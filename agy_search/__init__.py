"""agy-search: minimal, search-only MCP wrapper around the Antigravity CLI."""

from .core import DEFAULT_MODEL, AgySearchError, search

__all__ = ["DEFAULT_MODEL", "AgySearchError", "search"]
