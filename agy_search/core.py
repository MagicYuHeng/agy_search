"""Search-only wrapper around the Antigravity CLI (`agy`).

The invocation contract follows the two reference projects this was built from:

- ``tphakala/agy-mcp``: drive ``agy --output-format stream-json``, decode the
  newline-delimited event stream (init / step_update / result), close stdin
  explicitly, and give agy its own ``--print-timeout`` budget below ours.
- ``andyleimc-source/agy-mcp``: keep the surface tiny — one prompt through
  ``agy -p`` in a scratch directory — and retry once when the CLI hard-hangs.

Only the answer text is returned. Nothing here can write to the user's project:
each call runs in a throwaway temp directory, and print mode without
``--dangerously-skip-permissions`` cannot touch files or run commands without a
permission prompt that nobody in a non-interactive run can approve.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

AGY_BIN = os.environ.get("AGY_BIN", "agy")

# Model ids are what `agy models` prints in its first column; the level is part
# of the id on agy 1.4.x. Override with AGY_SEARCH_MODEL.
DEFAULT_MODEL = os.environ.get("AGY_SEARCH_MODEL", "gemini-3.8-flash-high").strip()

# Optional --effort override. Empty means "whatever the model id encodes"; not
# every id accepts an explicit effort, so it stays unset by default.
DEFAULT_EFFORT = os.environ.get("AGY_SEARCH_EFFORT", "").strip()

# Healthy searches took ~15 s on gemini-3.8-flash-high. A hung agy gets killed
# and retried once; worst case stays bounded at timeout x (retries + 1).
DEFAULT_TIMEOUT = int(os.environ.get("AGY_SEARCH_TIMEOUT", "120"))
RETRIES = int(os.environ.get("AGY_SEARCH_RETRIES", "1"))

# Answers longer than this are cut and annotated. Keeps the tool result cheap in
# the calling model's context; raise it if you want the full text.
DEFAULT_MAX_CHARS = int(os.environ.get("AGY_SEARCH_MAX_CHARS", "8000"))

# Google's search grounding returns opaque redirect URLs, which are both long and
# useless to a reader. Resolve them (server-side, never by asking the model to run
# curl) to the publisher URL. On by default; AGY_SEARCH_RESOLVE_SOURCES=0 disables.
RESOLVE_SOURCES = os.environ.get("AGY_SEARCH_RESOLVE_SOURCES", "1") != "0"
RESOLVE_TIMEOUT = float(os.environ.get("AGY_SEARCH_RESOLVE_TIMEOUT", "4"))
MAX_SOURCES = 5

SEARCH_PROMPT = """\
Use the web search tool to research and answer the query below with current information.

Rules:
- Search the web before answering; do not answer time-sensitive questions from memory.
- Use only the web search tool. Do not call run_command, shell, browser, file, or subagent tools; they are disabled in this environment.
- Do not write or edit code, do not delegate to subagents, do not modify files.
- Answer in the same language as the query.
- Be concise: a direct synthesis, then up to 5 source links.
- List the source links exactly as the search results give them; do not shorten, resolve, or verify them.
- Never invent links.

Query: {query}"""

_REDIRECT_RE = re.compile(
    r"https://vertexaisearch\.cloud\.google\.com/grounding-api-redirect/[^\s<>\"')\]}。，；：！？、]+"
)


class AgySearchError(RuntimeError):
    """Raised when agy is missing, fails, or returns no usable answer."""


class EmptyAnswerError(AgySearchError):
    """agy finished with SUCCESS but produced no answer text (retryable)."""


@dataclass(frozen=True)
class StreamOutcome:
    """What a stream-json run produced."""

    result: Optional[dict]
    streamed_text: str
    malformed_lines: int


def _build_argv(model: str, timeout: int, prompt: str) -> list[str]:
    budget = max(timeout - 10, 30)
    argv = [
        AGY_BIN,
        "--print-timeout",
        f"{budget}s",
        "--output-format",
        "stream-json",
        "--model",
        model,
    ]
    if DEFAULT_EFFORT:
        argv += ["--effort", DEFAULT_EFFORT]
    argv += ["-p", prompt]
    return argv


def parse_stream(stdout: str) -> StreamOutcome:
    """Decode agy's newline-delimited stream-json output.

    Event shapes (measured against agy 1.4.7):

    - ``{"event":"init","conversation_id":...,"init":{...}}``
    - ``{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"..."}}``
    - ``{"event":"result","result":{"status":"SUCCESS","response":"..."}}``

    The terminal result is authoritative. Streamed deltas are kept as a fallback
    for a run that ends without one (hard kill, partial print-timeout).
    """
    result: Optional[dict] = None
    chunks: list[str] = []
    malformed = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if not isinstance(event, dict):
            malformed += 1
            continue
        kind = event.get("event")
        if kind == "result":
            payload = event.get("result")
            if isinstance(payload, dict):
                result = payload
        elif kind == "step_update":
            step = event.get("step_update")
            if not isinstance(step, dict):
                continue
            if step.get("step_type") == "agent_response":
                delta = step.get("text_delta")
                if isinstance(delta, str) and delta:
                    chunks.append(delta)
    return StreamOutcome(result=result, streamed_text="".join(chunks), malformed_lines=malformed)


def _trim(text: str) -> str:
    text = text.strip()
    if len(text) <= DEFAULT_MAX_CHARS:
        return text
    omitted = len(text) - DEFAULT_MAX_CHARS
    return (
        text[:DEFAULT_MAX_CHARS].rstrip()
        + f"\n\n[truncated {omitted} chars; raise AGY_SEARCH_MAX_CHARS for the full answer]"
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface the 3xx Location instead of following it to the publisher."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _resolve_redirect(url: str) -> str:
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"}
    )
    try:
        opener.open(request, timeout=RESOLVE_TIMEOUT)
        return url
    except urllib.error.HTTPError as exc:
        location = (exc.headers or {}).get("Location") if exc.headers else None
        if isinstance(location, str) and location.startswith("http"):
            return location
        return url
    except (urllib.error.URLError, OSError, ValueError):
        return url


def resolve_sources(text: str) -> str:
    """Replace Google grounding-redirect links with the real publisher URL.

    Done in parallel and bounded by MAX_SOURCES / RESOLVE_TIMEOUT; any link that
    cannot be resolved is left untouched.
    """
    if not RESOLVE_SOURCES:
        return text
    found = list(dict.fromkeys(_REDIRECT_RE.findall(text)))[:MAX_SOURCES]
    if not found:
        return text
    with ThreadPoolExecutor(max_workers=len(found)) as pool:
        resolved = dict(zip(found, pool.map(_resolve_redirect, found)))
    for old, new in resolved.items():
        if new != old:
            text = text.replace(old, new)
    return text


def _run_once(prompt: str, model: str, timeout: int) -> str:
    """One agy attempt in a disposable workspace. Raises AgySearchError or
    subprocess.TimeoutExpired."""
    workdir = tempfile.mkdtemp(prefix="agy-search-")
    try:
        argv = _build_argv(model, timeout, prompt)
        proc = subprocess.run(
            argv,
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        outcome = parse_stream(proc.stdout or "")

        if outcome.result is not None:
            status = str(outcome.result.get("status") or "").upper()
            response = outcome.result.get("response")
            response = response.strip() if isinstance(response, str) else ""
            if status == "SUCCESS":
                if response:
                    return response
                if outcome.streamed_text.strip():
                    return outcome.streamed_text.strip()
                stderr = (proc.stderr or "").strip()
                raise EmptyAnswerError(
                    "agy reported SUCCESS but returned no answer text"
                    + (f" (stderr: {stderr[:500]})" if stderr else "")
                    + ". The model may have tried a disabled tool such as run_command."
                )
            detail = outcome.result.get("error") or response or "no detail"
            raise AgySearchError(f"agy reported {status or 'an error'}: {str(detail).strip()}")

        if outcome.streamed_text.strip():
            # Partial stream: agy was killed or hit its print-timeout before the
            # terminal event. Better than nothing, and clearly incomplete.
            return outcome.streamed_text.strip()

        stderr = (proc.stderr or "").strip()
        raise AgySearchError(
            f"agy exited {proc.returncode} without a result event. "
            f"stderr: {stderr[:1000] or '(empty)'}"
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def search(query: str, model: Optional[str] = None, timeout: Optional[int] = None) -> str:
    """Run one web search through agy and return the answer text.

    Raises AgySearchError when agy is unavailable or returns nothing usable.
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("query must not be empty")
    if not shutil.which(AGY_BIN):
        raise AgySearchError(
            f"`{AGY_BIN}` not found on PATH. Install the Antigravity CLI and log in, "
            "or set AGY_BIN to its full path."
        )

    chosen_model = (model or DEFAULT_MODEL).strip()
    budget = timeout or DEFAULT_TIMEOUT
    prompt = SEARCH_PROMPT.format(query=query)

    last_error: Optional[BaseException] = None
    for _ in range(RETRIES + 1):
        try:
            answer = _run_once(prompt, chosen_model, budget)
            return _trim(resolve_sources(answer))
        except (subprocess.TimeoutExpired, EmptyAnswerError) as exc:
            # A wedged process or a model that ended without an answer both
            # usually clear on a fresh attempt.
            last_error = exc

    if isinstance(last_error, EmptyAnswerError):
        raise AgySearchError(
            f"{last_error} Retried {RETRIES + 1} time(s) without success."
        ) from last_error
    raise AgySearchError(
        f"agy did not finish within {budget}s on {RETRIES + 1} attempt(s). "
        "Run `agy -p 'search: ...'` once interactively to check the login state."
    ) from last_error
