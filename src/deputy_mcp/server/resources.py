"""MCP resources exposing the signed-in user's weekly roster.

Resources are read-only, addressable documents an MCP client can fetch without a
tool call. Two are published:

* ``deputy://my/roster/this-week`` — the current Monday-to-Sunday week.
* ``deputy://my/roster/next-week`` — the following Monday-to-Sunday week.

Both render the same markdown a tool would, using the install timezone. They are
registered by :func:`register`, which receives the shared ``get_client`` provider
so they reuse the process-wide Deputy client.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from deputy_mcp.client import DeputyClient, DeputyError
from deputy_mcp.server.formatting import render_roster_list
from deputy_mcp.server.tools_read import resolve_client_timezone

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ["register"]

#: A zero-argument provider returning the process-wide Deputy client.
ClientProvider = Callable[[], DeputyClient]


def _week_bounds(offset_weeks: int) -> tuple[date, date]:
    """Return the Monday-Sunday bounds of the week ``offset_weeks`` from now."""
    today = datetime.now(UTC).date()
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset_weeks)
    return monday, monday + timedelta(days=6)


async def _render_week(client: DeputyClient, offset_weeks: int, label: str) -> str:
    """Fetch and render the caller's roster for a given week as markdown."""
    start, end = _week_bounds(offset_weeks)
    try:
        rosters = await client.get_my_roster(start, end)
        tz, tz_label = await resolve_client_timezone(client)
    except DeputyError as exc:
        hint = f"\nHint: {exc.hint}" if exc.hint else ""
        return f"### {label}\n\nCould not load roster.\nError: {exc.message}{hint}"
    title = f"{label} ({start.isoformat()} to {end.isoformat()})"
    return render_roster_list(rosters, tz, tz_label, title=title)


def register(mcp: FastMCP[Any], get_client: ClientProvider) -> None:
    """Register the weekly roster resources onto ``mcp``."""

    @mcp.resource(
        "deputy://my/roster/this-week",
        name="my_roster_this_week",
        description="Your own shifts for the current week (Monday-Sunday), rendered as markdown.",
        mime_type="text/markdown",
    )
    async def this_week() -> str:
        return await _render_week(get_client(), 0, "My roster - this week")

    @mcp.resource(
        "deputy://my/roster/next-week",
        name="my_roster_next_week",
        description="Your own shifts for next week (Monday-Sunday), rendered as markdown.",
        mime_type="text/markdown",
    )
    async def next_week() -> str:
        return await _render_week(get_client(), 1, "My roster - next week")
