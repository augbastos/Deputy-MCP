"""MCP server package for Deputy.

Exposes :func:`create_server`, the FastMCP application factory that wires the
Deputy client to the read tools, write tools (opt-in), resources and prompts.
This package depends on :mod:`deputy_mcp.client`, never the other way around.

``create_server`` is resolved lazily via a module ``__getattr__`` (PEP 562) so
that merely importing :mod:`deputy_mcp.server` — as the CLI does to monkeypatch
it in tests, or to reach a submodule — does NOT import :mod:`fastmcp`. The factory
module itself also defers its ``fastmcp`` import into ``create_server``'s body, so
even ``from deputy_mcp.server import create_server`` (the CLI's serve path) stays
fastmcp-free; the heavy ``fastmcp`` import happens only when the server is actually
built. This is what lets the client-only CLI stay dependency-light without inlining
its own copy of the server's renderers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = ["create_server"]

if TYPE_CHECKING:
    from deputy_mcp.server.app import create_server


def __getattr__(name: str) -> Any:
    """Lazily import ``create_server`` on first access (keeps ``fastmcp`` out of import)."""
    if name == "create_server":
        from deputy_mcp.server.app import create_server

        return create_server
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
