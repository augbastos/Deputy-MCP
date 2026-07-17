"""Shared helpers for the MCP server tests (in-memory FastMCP client).

The server tests drive the real :class:`fastmcp.Client` against a real
:func:`deputy_mcp.server.create_server` instance, with Deputy's HTTP API mocked by
respx at the transport layer. These helpers wire a full set of read/write routes so
any tool, resource or prompt can be exercised, and pull the plain-text payload out of
a tool call result (our tools all return ``str``, so ``CallToolResult.data`` carries
the rendered string).

Everything here operates on the fictional install and people defined in the suite's
``conftest.py`` -- no real names, tokens or installs.
"""

from __future__ import annotations

from typing import Any

import httpx
import respx
from fastmcp.client.client import CallToolResult

__all__ = [
    "READ_TOOL_NAMES",
    "WRITE_TOOL_NAMES",
    "tool_text",
    "wire_read_api",
    "wire_write_api",
]

#: The nine read tools every server build must expose.
READ_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "deputy_whoami",
        "deputy_get_my_roster",
        "deputy_get_team_roster",
        "deputy_who_is_working",
        "deputy_get_employee_info",
        "deputy_search_shifts",
        "deputy_get_areas",
        "deputy_next_shift",
        "deputy_get_my_timesheets",
    }
)

#: The five write tools that appear ONLY when DEPUTY_ALLOW_WRITES is enabled.
WRITE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "deputy_claim_open_shift",
        "deputy_request_shift_swap",
        "deputy_set_unavailability",
        "deputy_clock_in",
        "deputy_clock_out",
    }
)


def tool_text(result: CallToolResult) -> str:
    """Return the plain-text payload of a tool call.

    Our tools return ``str``, so FastMCP parses it back into ``result.data``. Fall
    back to the first text content block if a build ever omits structured output.
    """
    if isinstance(result.data, str):
        return result.data
    block = result.content[0]
    text = getattr(block, "text", None)
    if isinstance(text, str):
        return text
    raise AssertionError(f"tool result carried no text payload: {result!r}")


def _ok(payload: Any) -> httpx.Response:
    """A 200 JSON response."""
    return httpx.Response(200, json=payload)


def wire_read_api(
    router: respx.MockRouter,
    *,
    whoami: dict[str, Any],
    company: dict[str, Any],
    employees: list[dict[str, Any]],
    operational_units: list[dict[str, Any]],
    rosters: list[dict[str, Any]],
    timesheets: list[dict[str, Any]],
) -> None:
    """Register every read endpoint the read tools / resources may touch.

    The same Roster/QUERY route backs team roster, shift search, next-shift and the
    rostered-now half of who-is-working; the same Timesheet/QUERY route backs the
    clocked-in half. That is fine for smoke coverage -- the tool wiring, argument
    handling and rendering are what these tests assert.
    """
    router.get("/resource/Account/WhoAmI").mock(return_value=_ok(whoami))
    router.get("/my/roster").mock(return_value=_ok(rosters))
    router.get("/my/timesheets").mock(return_value=_ok(timesheets))
    router.post("/resource/Company/QUERY").mock(return_value=_ok([company]))
    router.post("/resource/OperationalUnit/QUERY").mock(return_value=_ok(operational_units))
    router.post("/resource/Employee/QUERY").mock(return_value=_ok(employees))
    router.post("/resource/Roster/QUERY").mock(return_value=_ok(rosters))
    router.post("/resource/Timesheet/QUERY").mock(return_value=_ok(timesheets))
    # Single-employee GET (used when a tool is given a numeric id).
    router.get(path__regex=r"/resource/Employee/\d+$").mock(
        return_value=_ok(employees[0] if employees else {})
    )


def wire_write_api(
    router: respx.MockRouter,
    *,
    whoami: dict[str, Any],
    company: dict[str, Any],
    swap: dict[str, Any],
    unavailability: dict[str, Any],
    timesheet_started: dict[str, Any],
    timesheet_ended: dict[str, Any],
    in_progress_timesheet: dict[str, Any],
    operational_units: list[dict[str, Any]] | None = None,
    open_roster: dict[str, Any] | None = None,
) -> None:
    """Register every write endpoint (plus the reads a write path needs)."""
    router.get("/resource/Account/WhoAmI").mock(return_value=_ok(whoami))
    router.post("/resource/Company/QUERY").mock(return_value=_ok([company]))
    router.post("/resource/OperationalUnit/QUERY").mock(return_value=_ok(operational_units or []))
    # claim_open_shift reads the target roster first (to confirm it is open and to
    # preserve its time/area), then updates it. Deputy answers the update with 200 +
    # empty body.
    router.get(path__regex=r"/resource/Roster/\d+$").mock(
        return_value=_ok(
            open_roster
            or {
                "Id": 9001,
                "StartTime": 1609459200,
                "EndTime": 1609488000,
                "OperationalUnit": 11,
                "Open": True,
                "Employee": 0,
            }
        )
    )
    router.post("/supervise/roster").mock(return_value=httpx.Response(200, text=""))
    router.post("/resource/RosterSwap").mock(return_value=_ok(swap))
    router.post("/supervise/unavail").mock(return_value=_ok(unavailability))
    router.post("/supervise/timesheet/start").mock(return_value=_ok(timesheet_started))
    router.post("/supervise/timesheet/end").mock(return_value=_ok(timesheet_ended))
    # clock_out looks up the caller's in-progress timesheet first.
    router.post("/resource/Timesheet/QUERY").mock(return_value=_ok([in_progress_timesheet]))
