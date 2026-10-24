"""Unit tests for stream-json decoding, argv building, and source resolution.

Run with:  .venv/Scripts/python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from agy_search.core import DEFAULT_MODEL, _build_argv, parse_stream, resolve_sources


def ndjson(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


class BuildArgvTests(unittest.TestCase):
    def test_default_model_is_forwarded_and_budget_stays_below_timeout(self) -> None:
        argv = _build_argv(DEFAULT_MODEL, 120, "hello")
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-3.8-flash-high")
        self.assertEqual(argv[argv.index("--print-timeout") + 1], "110s")
        self.assertEqual(argv[-2:], ["-p", "hello"])
        self.assertNotIn("--dangerously-skip-permissions", argv)

    def test_stream_json_is_always_requested(self) -> None:
        argv = _build_argv("some-model", 60, "q")
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")


class ParseStreamTests(unittest.TestCase):
    def test_result_wins_and_deltas_are_kept(self) -> None:
        stream = ndjson(
            {"event": "init", "conversation_id": "c1", "init": {"model": "m"}},
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "state": "ACTIVE",
                    "text_delta": "Hello ",
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "state": "DONE",
                    "text_delta": "world",
                },
            },
            {
                "event": "result",
                "result": {"status": "SUCCESS", "response": "Hello world\n"},
            },
        )
        outcome = parse_stream(stream)
        self.assertEqual(outcome.result["status"], "SUCCESS")
        self.assertEqual(outcome.streamed_text, "Hello world")
        self.assertEqual(outcome.malformed_lines, 0)

    def test_only_agent_response_deltas_accumulate(self) -> None:
        stream = ndjson(
            {
                "event": "step_update",
                "step_update": {"step_type": "tool", "tool_name": "search_web"},
            },
            {
                "event": "step_update",
                "step_update": {"step_type": "agent_response", "text_delta": "answer"},
            },
        )
        outcome = parse_stream(stream)
        self.assertIsNone(outcome.result)
        self.assertEqual(outcome.streamed_text, "answer")

    def test_malformed_lines_are_counted_not_fatal(self) -> None:
        stream = '{"event":"step_update","step_update":{"step_type":"agent_response","text_delta":"ok"}}\nnot json\n'
        outcome = parse_stream(stream)
        self.assertEqual(outcome.streamed_text, "ok")
        self.assertEqual(outcome.malformed_lines, 1)

    def test_error_result_payload_is_exposed(self) -> None:
        stream = ndjson(
            {
                "event": "result",
                "result": {"status": "ERROR", "response": "", "error": "bad model"},
            }
        )
        outcome = parse_stream(stream)
        self.assertEqual(outcome.result["status"], "ERROR")
        self.assertEqual(outcome.result["error"], "bad model")


class ResolveSourcesTests(unittest.TestCase):
    def test_redirect_links_are_replaced_with_publisher_urls(self) -> None:
        text = (
            "Answer.\n\nSources:\n"
            "1. https://vertexaisearch.cloud.google.com/grounding-api-redirect/AAA111\n"
            "2. https://vertexaisearch.cloud.google.com/grounding-api-redirect/BBB222\n"
        )
        with patch(
            "agy_search.core._resolve_redirect",
            side_effect=lambda url: "https://example.com/"
            + ("a" if url.endswith("AAA111") else "b"),
        ):
            out = resolve_sources(text)
        self.assertIn("https://example.com/a", out)
        self.assertIn("https://example.com/b", out)
        self.assertNotIn("grounding-api-redirect", out)

    def test_duplicate_links_are_resolved_once(self) -> None:
        url = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AAA111"
        calls: list[str] = []

        def fake(url: str) -> str:
            calls.append(url)
            return "https://example.com/real"

        with patch("agy_search.core._resolve_redirect", side_effect=fake):
            out = resolve_sources(f"{url}\n{url}\n")
        self.assertEqual(calls, [url])
        self.assertEqual(out.count("https://example.com/real"), 2)

    def test_non_redirect_text_is_untouched(self) -> None:
        text = "Plain answer with https://example.com/a"
        self.assertEqual(resolve_sources(text), text)


if __name__ == "__main__":
    unittest.main()
