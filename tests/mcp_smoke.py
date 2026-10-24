"""End-to-end MCP smoke test: spawn the server over stdio and call its tool.

Run with:  .venv/Scripts/python.exe tests/mcp_smoke.py "your query"
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else "What are the latest stable Python release notes?"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agy_search.server"],
        cwd=ROOT,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])
            print("schema:", tools.tools[0].input_schema)
            result = await session.call_tool("search", {"query": query})
            text = "\n".join(
                getattr(block, "text", "") for block in result.content if getattr(block, "text", None)
            )
            (ROOT / "tests" / "last_answer.txt").write_text(text, encoding="utf-8")
            print("is_error:", result.is_error)
            print("answer_chars:", len(text))
            print("ascii_head:", ascii(text[:160]))
            print("redirect_links_left:", text.count("grounding-api-redirect"))
            print("urls:", [line.strip() for line in text.splitlines() if line.strip().startswith("http")][:8])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
