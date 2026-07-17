"""Thin command-line interface for deputy-mcp.

Two roles share one entry point:

* With no subcommand (or ``serve``) it launches the MCP server on stdio. The
  ``create_server`` factory is imported *lazily* inside the serve path so that
  client-only CLI use never pulls in ``fastmcp``. In serve mode stdout belongs to
  the MCP protocol, so this module writes only to stderr there.
* The read subcommands (``whoami``, ``roster``, ``timesheets``, ``who``,
  ``areas``, ``next``) reuse :class:`~deputy_mcp.client.DeputyClient` directly via
  :func:`asyncio.run`. ``--json`` emits the raw API objects; otherwise a compact
  human rendering is printed.

Every :class:`~deputy_mcp.client.errors.DeputyError` is turned into a single
actionable stderr line and exit code 1 — callers never see a traceback.

Times are rendered as UTC ``HH:MM`` alongside Deputy's local business-day
``Date``. The richer, company-timezone-aware markdown renderers live in
``server/formatting.py``; that module sits under the ``deputy_mcp.server``
package whose import pulls in ``fastmcp``, so the CLI inlines its own minimal
renderers to stay dependency-light for client-only use.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from deputy_mcp.client import DeputyClient
from deputy_mcp.client.errors import DeputyError, DeputyNotFoundError
from deputy_mcp.client.models import Roster, Timesheet
from deputy_mcp.client.reads import EMPLOYEE_JOIN

__all__ = ["main"]

#: Default look-ahead window (days) for the roster subcommand.
_ROSTER_LOOKAHEAD_DAYS = 7
#: Default look-back window (days) for the timesheets subcommand.
_TIMESHEET_LOOKBACK_DAYS = 7


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #
def _date_arg(value: str) -> date:
    """Argparse type for an ISO ``YYYY-MM-DD`` date."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"'{value}' is not a valid date (expected YYYY-MM-DD)."
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    """Build the ``deputy-mcp`` argument parser (serve + read subcommands)."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit raw Deputy objects as JSON instead of human-readable text.",
    )

    parser = argparse.ArgumentParser(
        prog="deputy-mcp",
        description=(
            "Deputy MCP server and companion CLI. With no subcommand, runs the MCP server on stdio."
        ),
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    sub.add_parser(
        "serve",
        parents=[common],
        help="Run the MCP server on stdio (the default when no command is given).",
    )
    sub.add_parser("whoami", parents=[common], help="Show the authenticated user and location.")

    p_roster = sub.add_parser("roster", parents=[common], help="Show a roster (yours by default).")
    p_roster.add_argument(
        "--start", type=_date_arg, help="Range start (YYYY-MM-DD; default today)."
    )
    p_roster.add_argument(
        "--end", type=_date_arg, help="Range end (YYYY-MM-DD; default start + 7 days)."
    )
    p_roster.add_argument(
        "--team", action="store_true", help="Show the whole team's roster, not just yours."
    )
    p_roster.add_argument(
        "--area", type=int, metavar="ID", help="Restrict --team to one area (OperationalUnit id)."
    )

    p_ts = sub.add_parser("timesheets", parents=[common], help="Show your own timesheets.")
    p_ts.add_argument("--start", type=_date_arg, help="Range start (YYYY-MM-DD; default end - 7d).")
    p_ts.add_argument("--end", type=_date_arg, help="Range end (YYYY-MM-DD; default today).")

    sub.add_parser("who", parents=[common], help="Show who is working right now.")
    sub.add_parser("areas", parents=[common], help="List areas (operational units).")

    p_next = sub.add_parser("next", parents=[common], help="Show the next upcoming shift.")
    p_next.add_argument(
        "--employee",
        metavar="NAME_OR_ID",
        help="Employee name or id (defaults to you).",
    )
    return parser


# --------------------------------------------------------------------------- #
# JSON output
# --------------------------------------------------------------------------- #
def _json_default(obj: Any) -> Any:
    """``json.dumps`` fallback for pydantic models and datetimes."""
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _emit_json(payload: Any) -> None:
    """Print ``payload`` as indented JSON to stdout."""
    print(json.dumps(payload, indent=2, default=_json_default))


# --------------------------------------------------------------------------- #
# Human rendering (minimal; see module docstring for why it is inlined)
# --------------------------------------------------------------------------- #
def _fmt_time(moment: datetime | None) -> str:
    """Render a UTC time as ``HH:MM`` (``--:--`` when unset)."""
    return moment.strftime("%H:%M") if moment is not None else "--:--"


def _fmt_window(shift: Roster | Timesheet) -> str:
    """Render a shift/timesheet window as ``<date> HH:MM-HH:MM UTC``."""
    if shift.Date:
        day = shift.Date
    elif shift.start_dt is not None:
        day = shift.start_dt.date().isoformat()
    else:
        day = "????-??-??"
    return f"{day} {_fmt_time(shift.start_dt)}-{_fmt_time(shift.end_dt)} UTC"


def _employee_name(shift: Roster | Timesheet) -> str:
    """Best-effort display name for a shift's employee (joined or by id)."""
    extra = shift.model_extra or {}
    joined = extra.get(EMPLOYEE_JOIN)
    if isinstance(joined, dict):
        for key in ("DisplayName", "Name", "FirstName"):
            value = joined.get(key)
            if isinstance(value, str) and value.strip():
                return value
    if shift.Employee is not None:
        return f"Employee #{shift.Employee}"
    return "(unassigned)"


def _area_text(shift: Roster | Timesheet) -> str:
    """Area label for a shift/timesheet, resolving the name when only it is available.

    Uses the ``OperationalUnit`` id when present, else the area name embedded on the
    record as ``OperationalUnitObject`` — which both ``/api/v1/my/roster`` and the iCal
    feed carry — so an iCal-mode roster shows its real area/title instead of "no area".
    """
    if shift.OperationalUnit is not None:
        return f"area #{shift.OperationalUnit}"
    obj = (shift.model_extra or {}).get("OperationalUnitObject")
    if isinstance(obj, dict):
        name = obj.get("OperationalUnitName")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return "no area"


def _render_rosters(rosters: list[Roster]) -> str:
    """Render a list of rosters as indented bullet lines."""
    if not rosters:
        return "  (no shifts)"
    lines: list[str] = []
    for shift in rosters:
        who = "OPEN SHIFT" if shift.Open else _employee_name(shift)
        lines.append(f"  - {_fmt_window(shift)}  {who}  ({_area_text(shift)})")
    return "\n".join(lines)


def _render_timesheets(sheets: list[Timesheet]) -> str:
    """Render a list of timesheets as indented bullet lines."""
    if not sheets:
        return "  (no timesheets)"
    lines: list[str] = []
    for sheet in sheets:
        status = "in progress" if sheet.IsInProgress else "completed"
        hours = f"{sheet.TotalTime:.2f}h" if sheet.TotalTime is not None else "?h"
        lines.append(f"  - {_fmt_window(sheet)}  {_employee_name(sheet)}  {hours} ({status})")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Command handlers (async; each reuses DeputyClient)
# --------------------------------------------------------------------------- #
async def _resolve_employee(client: DeputyClient, value: str) -> int:
    """Resolve an employee name-or-id argument to an employee id."""
    text = value.strip()
    if text.isdigit():
        return int(text)
    matches = await client.get_employees(search=text)
    if not matches:
        raise DeputyNotFoundError(
            f"No active employee matches '{value}'.",
            hint="Try a different spelling, or pass a numeric employee id.",
        )
    ident = matches[0].Id
    if ident is None:
        raise DeputyNotFoundError(f"The employee matched for '{value}' has no id.")
    return ident


async def _cmd_whoami(client: DeputyClient, as_json: bool) -> None:
    who = await client.whoami()
    try:
        company = await client.get_company()
    except DeputyError:
        company = None
    if as_json:
        _emit_json({"whoami": who, "company": company})
        return
    extra = who.model_extra or {}
    name = extra.get("Name") or extra.get("DisplayName") or "(unknown user)"
    print(f"Authenticated as: {name}")
    for key in ("UserId", "EmployeeId", "Company", "CompanyName"):
        if key in extra:
            print(f"  {key}: {extra[key]}")
    if company is not None:
        label = company.CompanyName or company.TradingName or f"#{company.Id}"
        print(f"Primary location: {label}")


async def _cmd_roster(client: DeputyClient, args: argparse.Namespace) -> None:
    start: date = args.start or date.today()
    end: date = args.end or (start + timedelta(days=_ROSTER_LOOKAHEAD_DAYS))
    if args.team:
        rosters = await client.get_team_roster(start, end, args.area)
    else:
        rosters = await client.get_my_roster(start, end)
    if args.as_json:
        _emit_json(rosters)
        return
    scope = "Team" if args.team else "My"
    print(f"{scope} roster {start.isoformat()} to {end.isoformat()}:")
    print(_render_rosters(rosters))


async def _cmd_timesheets(client: DeputyClient, args: argparse.Namespace) -> None:
    end: date = args.end or date.today()
    start: date = args.start or (end - timedelta(days=_TIMESHEET_LOOKBACK_DAYS))
    sheets = await client.get_my_timesheets(start, end)
    if args.as_json:
        _emit_json(sheets)
        return
    print(f"My timesheets {start.isoformat()} to {end.isoformat()}:")
    print(_render_timesheets(sheets))


async def _cmd_who(client: DeputyClient, as_json: bool) -> None:
    result = await client.who_is_working()
    if as_json:
        _emit_json(result)
        return
    clocked: list[Timesheet] = result["clocked_in"]
    rostered: list[Roster] = result["rostered_now"]
    print(f"As of {result['at']}:")
    print(f"\nClocked in ({len(clocked)}):")
    print(_render_timesheets(clocked))
    print(f"\nRostered now ({len(rostered)}):")
    print(_render_rosters(rostered))


async def _cmd_areas(client: DeputyClient, as_json: bool) -> None:
    units = await client.get_operational_units()
    if as_json:
        _emit_json(units)
        return
    if not units:
        print("No areas found.")
        return
    for unit in units:
        print(f"- #{unit.Id}  {unit.OperationalUnitName or '(unnamed)'}")


async def _cmd_next(client: DeputyClient, args: argparse.Namespace) -> None:
    employee_id = await _resolve_employee(client, args.employee) if args.employee else None
    shift = await client.next_shift(employee_id)
    if args.as_json:
        _emit_json(shift)
        return
    if shift is None:
        print("No upcoming shift found.")
        return
    print(f"Next shift: {_fmt_window(shift)}  {_employee_name(shift)}")


async def _run_command(command: str, args: argparse.Namespace) -> None:
    """Dispatch a read subcommand against a live client."""
    async with DeputyClient.from_env() as client:
        if command == "whoami":
            await _cmd_whoami(client, args.as_json)
        elif command == "roster":
            await _cmd_roster(client, args)
        elif command == "timesheets":
            await _cmd_timesheets(client, args)
        elif command == "who":
            await _cmd_who(client, args.as_json)
        elif command == "areas":
            await _cmd_areas(client, args.as_json)
        elif command == "next":
            await _cmd_next(client, args)
        else:  # pragma: no cover - argparse constrains the command set
            raise DeputyError(f"Unknown command: {command}")


# --------------------------------------------------------------------------- #
# Serve mode
# --------------------------------------------------------------------------- #
def _serve() -> int:
    """Run the MCP server on stdio; never writes to stdout (stderr only)."""
    # Lazy import: client-only CLI use must never import fastmcp.
    from deputy_mcp.server import create_server

    print("deputy-mcp: starting MCP server on stdio.", file=sys.stderr)
    try:
        server = create_server()
    except DeputyError as exc:
        _fail(exc)
        return 1
    server.run(transport="stdio")
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _fail(exc: DeputyError) -> None:
    """Print a single actionable error line to stderr."""
    print(f"deputy-mcp error: {exc}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    command: str = args.command or "serve"

    if command == "serve":
        return _serve()

    try:
        asyncio.run(_run_command(command, args))
    except DeputyError as exc:
        _fail(exc)
        return 1
    return 0
