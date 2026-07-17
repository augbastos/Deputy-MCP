"""FastMCP application factory for the Deputy MCP server.

:func:`create_server` builds the one FastMCP instance the process serves. It:

* loads config and constructs a single :class:`~deputy_mcp.client.DeputyClient`
  from the environment (failing closed at startup on bad config),
* wires a ``get_client`` provider so every tool/resource reuses that client,
* registers the read tools, resources and prompts, and — only when
  ``DEPUTY_ALLOW_WRITES`` is enabled — the write tools (they are invisible
  otherwise, so a read-only deployment cannot mutate Deputy by accident), and
* closes the client on shutdown via the server lifespan.

The write-tool module is imported lazily inside the ``allow_writes`` branch so a
read-only install never even imports the mutation code path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP

from deputy_mcp import __version__
from deputy_mcp.client import DeputyClient
from deputy_mcp.server import prompts, resources, tools_read

__all__ = ["create_server"]


def _instructions(*, allow_writes: bool) -> str:
    """Build the server instructions string shown to MCP clients."""
    mode = "enabled" if allow_writes else "disabled (read-only)"
    base = (
        "Query and manage Deputy (deputy.com) workforce data: rosters, shifts, "
        "timesheets, employees and areas. Read tools cover your own and the team's "
        "roster, who is working now, employee lookup, shift search, areas and "
        "timesheets. Every tool accepts response_format='markdown' (default) or "
        "'json'. Dates are ISO YYYY-MM-DD; times are shown in the install timezone."
    )
    writes = (
        " Write tools (claim open shift, request swap, set unavailability, clock "
        "in/out) are available."
        if allow_writes
        else " Write actions are disabled; set DEPUTY_ALLOW_WRITES=true to enable them."
    )
    return f"{base}{writes} Write actions are currently {mode}."


def create_server() -> FastMCP[dict[str, Any]]:
    """Create and configure the Deputy FastMCP server (built once per process)."""
    client = DeputyClient.from_env()

    def get_client() -> DeputyClient:
        return client

    @asynccontextmanager
    async def lifespan(_server: FastMCP[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
        try:
            yield {}
        finally:
            await client.aclose()

    allow_writes = client.config.allow_writes
    mcp: FastMCP[dict[str, Any]] = FastMCP(
        name="deputy_mcp",
        version=__version__,
        instructions=_instructions(allow_writes=allow_writes),
        lifespan=lifespan,
    )

    provider: Callable[[], DeputyClient] = get_client
    tools_read.register(mcp, provider)
    resources.register(mcp, provider)
    prompts.register(mcp)

    if allow_writes:
        from deputy_mcp.server import tools_write

        tools_write.register(mcp, provider)

    return mcp
