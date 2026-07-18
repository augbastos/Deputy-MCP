"""Thin command-line interface for deputy-mcp.

Two roles share one entry point:

* With no subcommand (or ``serve``) it launches the MCP server on stdio. The
  ``create_server`` factory is imported *lazily* inside the serve path so that
  client-only CLI use never pulls in ``fastmcp``. In serve mode stdout belongs to
  the MCP protocol, so this module writes only to stderr there.
* The read subcommands (``whoami``, ``roster``, ``timesheets``, ``who``,
  ``areas``, ``next``) reuse :class:`~deputy_mcp.client.DeputyClient` directly via
  :func:`asyncio.run`. ``--json`` emits the raw API objects; otherwise a human
  rendering is printed.
* ``login`` runs the OAuth 2.0 loopback flow (for an employee who cannot mint a
  permanent API token), persisting the resulting access/refresh tokens to the
  token store; ``logout`` deletes that store. Neither ever prints a token value.

Every :class:`~deputy_mcp.client.errors.DeputyError` is turned into a single
actionable stderr line and exit code 1 — callers never see a traceback.

Human output and ``--json`` both go through the shared, fastmcp-free
:mod:`deputy_mcp.render` renderers — the SAME code the MCP server uses — so a
shift shows the same wall-clock time whichever surface you read it on (there is
no longer a separate CLI renderer with its own timezone rules). The renderers are
parameterised by a timezone; the CLI does not spend an extra API call just to
fetch the install's company timezone, so it renders through that shared path with
a UTC fallback (``resolve_timezone(None)``), and the output labels the zone
honestly. Given a company record the identical renderer would show local time.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from deputy_mcp.client import DeputyClient
from deputy_mcp.client.errors import DeputyConfigError, DeputyError, DeputyNotFoundError
from deputy_mcp.config import DeputyConfig
from deputy_mcp.render import (
    render_areas,
    render_next_shift,
    render_roster_list,
    render_timesheet_list,
    render_who_is_working,
    resolve_timezone,
    to_json,
)

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

    p_login = sub.add_parser(
        "login",
        help="Sign in via OAuth (browser) and store an access token for API use.",
    )
    p_login.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open a browser; print the authorize URL to open manually.",
    )
    sub.add_parser("logout", help="Delete the stored OAuth token (sign out).")
    return parser


# --------------------------------------------------------------------------- #
# JSON output
# --------------------------------------------------------------------------- #
def _emit_json(payload: Any) -> None:
    """Print ``payload`` as indented JSON to stdout via the shared serializer."""
    print(to_json(payload))


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
    tz, label = resolve_timezone(None)
    scope = "Team" if args.team else "My"
    title = f"{scope} roster ({start.isoformat()} to {end.isoformat()})"
    print(render_roster_list(rosters, tz, label, title=title))


async def _cmd_timesheets(client: DeputyClient, args: argparse.Namespace) -> None:
    end: date = args.end or date.today()
    start: date = args.start or (end - timedelta(days=_TIMESHEET_LOOKBACK_DAYS))
    sheets = await client.get_my_timesheets(start, end)
    if args.as_json:
        _emit_json(sheets)
        return
    tz, label = resolve_timezone(None)
    title = f"My timesheets ({start.isoformat()} to {end.isoformat()})"
    print(render_timesheet_list(sheets, tz, label, title=title))


async def _cmd_who(client: DeputyClient, as_json: bool) -> None:
    result = await client.who_is_working()
    if as_json:
        _emit_json(result)
        return
    tz, label = resolve_timezone(None)
    print(render_who_is_working(result, tz, label))


async def _cmd_areas(client: DeputyClient, as_json: bool) -> None:
    units = await client.get_operational_units()
    if as_json:
        _emit_json(units)
        return
    print(render_areas(units))


async def _cmd_next(client: DeputyClient, args: argparse.Namespace) -> None:
    employee_id = await _resolve_employee(client, args.employee) if args.employee else None
    shift = await client.next_shift(employee_id)
    if args.as_json:
        _emit_json(shift)
        return
    tz, label = resolve_timezone(None)
    print(render_next_shift(shift, tz, label))


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
# OAuth login / logout
# --------------------------------------------------------------------------- #
def _print_register_steps(port: int) -> None:
    """Print the exact steps to register an OAuth app (no secret is ever echoed)."""
    redirect = f"http://localhost:{port}/callback"
    print("deputy-mcp login needs a registered Deputy OAuth app.")
    print("To set one up (no admin access required):")
    print("  1. Open https://once.deputy.com/my/oauth_clients and create an app.")
    print(f"  2. Set its redirect URI to exactly: {redirect}")
    print("  3. Copy the client id and client secret, then set these env vars:")
    print("       DEPUTY_OAUTH_CLIENT_ID=<your client id>")
    print("       DEPUTY_OAUTH_CLIENT_SECRET=<your client secret>")
    print("  4. Run 'deputy-mcp login' again.")


def _cmd_login(*, open_browser: bool = True) -> int:
    """Run the OAuth loopback flow and persist the tokens; never print a token."""
    from deputy_mcp import oauth

    try:
        config = DeputyConfig.from_env()
    except DeputyConfigError:
        # Nothing usable is configured at all — guide the user to register an app.
        _print_register_steps(oauth.DEFAULT_REDIRECT_PORT)
        return 1

    if not (config.oauth_client_id and config.oauth_client_secret is not None):
        _print_register_steps(config.redirect_port)
        return 1

    try:
        tokens = asyncio.run(oauth.run_login_flow(config, open_browser=open_browser))
    except DeputyError as exc:
        _fail(exc)
        return 1

    store = oauth.TokenStore(config.token_store_path)
    store.save(tokens)
    expires = datetime.fromtimestamp(tokens.expires_at, tz=UTC)
    print(
        f"Logged in — API base {tokens.base_url}, token stored at {store.path}, "
        f"access token expires {expires:%Y-%m-%d %H:%M UTC}"
    )
    return 0


def _token_store_path() -> Path:
    """Resolve the token-store path, even when no credentials are configured."""
    try:
        return DeputyConfig.from_env().token_store_path
    except DeputyConfigError:
        raw = (os.environ.get("DEPUTY_TOKEN_STORE") or "").strip()
        if raw:
            return Path(raw)
        return Path.home() / ".deputy-mcp" / "token.json"


def _cmd_logout() -> int:
    """Delete the stored OAuth token, if any."""
    from deputy_mcp import oauth

    path = _token_store_path()
    store = oauth.TokenStore(path)
    if store.delete():
        print(f"Logged out — removed token store at {path}.")
    else:
        print(f"No Deputy token store to remove at {path}.")
    return 0


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
    if command == "login":
        return _cmd_login(open_browser=not args.no_browser)
    if command == "logout":
        return _cmd_logout()

    try:
        asyncio.run(_run_command(command, args))
    except DeputyError as exc:
        _fail(exc)
        return 1
    return 0
