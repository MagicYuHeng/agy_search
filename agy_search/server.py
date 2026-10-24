"""agy-search MCP server: one read-only web-search tool backed by the local agy CLI."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .core import DEFAULT_MODEL, AgySearchError, search as _search

server = MCPServer(
    "agy-search",
    title="agy-search",
    description=f"Read-only web search through the local Antigravity CLI (default model: {DEFAULT_MODEL}).",
    instructions=(
        "Use agy-search when you need current web information. It only searches: "
        "it cannot write, edit, or run code."
    ),
)


@server.tool(
    annotations=ToolAnnotations(
        title="Web search via Antigravity",
        readOnlyHint=True,
        openWorldHint=True,
        idempotentHint=False,
    )
)
def search(query: str) -> str:
    """Search the web and return a concise answer with source URLs.

    Args:
        query: Natural-language search query.
    """
    try:
        return _search(query)
    except (AgySearchError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
