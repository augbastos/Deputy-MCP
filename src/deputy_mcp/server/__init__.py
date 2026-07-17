"""MCP server package for Deputy.

Exposes :func:`create_server`, the FastMCP application factory that wires the
Deputy client to the read tools, write tools (opt-in), resources and prompts.
This package depends on :mod:`deputy_mcp.client`, never the other way around.
"""

from __future__ import annotations

from deputy_mcp.server.app import create_server

__all__ = ["create_server"]
