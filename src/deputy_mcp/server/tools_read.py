"""Read-only MCP tools for Deputy.

Each tool is a thin wrapper over a :class:`~deputy_mcp.client.DeputyClient` read
method: it validates/coerces arguments, calls the client, then renders the result
either as markdown (default) or JSON via :mod:`deputy_mcp.server.formatting`. All
tools are annotated ``readOnlyHint`` and ``openWorldHint`` (they talk to a remote
Deputy install and never mutate it). Any :class:`DeputyError` is turned into a
short, actionable message string — tools never leak a raw traceback.

Tools are registered onto a shared FastMCP instance by :func:`register`, which is
given a ``get_client`` provider so every call reuses the one client built for the
process lifetime (see :mod:`deputy_mcp.server.app`).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from deputy_mcp.client import DeputyClient, DeputyError
from deputy_mcp.server.formatting import (
    ResponseFormat,
    areas_by_id,
    render,
    render_areas,
    render_employee_list,
    render_next_shift,
    render_roster_list,
    render_timesheet_list,
    render_who_is_working,
    render_whoami,
    resolve_timezone,
)

__all__ = ["register", "resolve_client_timezone"]

#: A zero-argument provider returning the process-wide Deputy client.
ClientProvider = Callable[[], DeputyClient]

#: Reused Field description for the dual-format switch on every tool.
_FORMAT_FIELD = Field(
    description="Output format: 'markdown' (human-readable, default) or 'json' (raw records)."
)


def _error(exc: DeputyError) -> str:
    """Format a client error as an actionable, traceback-free message."""
    text = f"Error: {exc.message}"
    if exc.hint:
        text += f"\nHint: {exc.hint}"
    return text


def _today() -> date:
    """Return today's date in UTC (used as a range default)."""
    return datetime.now(UTC).date()


def _parse_date(value: str | None, default: date) -> date:
    """Parse an ISO ``YYYY-MM-DD`` string, or return ``default`` when blank."""
    if value is None or not value.strip():
        return default
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise DeputyError(
            f"Invalid date '{value}'.", hint="Use ISO format YYYY-MM-DD, e.g. 2026-07-18."
        ) from exc


def _opt_date(value: str | None) -> date | None:
    """Parse an optional ISO date, returning ``None`` when blank."""
    if value is None or not value.strip():
        return None
    return _parse_date(value, _today())


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO date/datetime string into an aware UTC datetime, or ``None``."""
    if value is None or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise DeputyError(
            f"Invalid datetime '{value}'.",
            hint="Use ISO format, e.g. 2026-07-18T14:30 (UTC assumed if no offset).",
        ) from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


async def resolve_client_timezone(client: DeputyClient) -> tuple[tzinfo, str]:
    """Return the install timezone and label, defaulting to UTC on any failure.

    The company/location lookup is best-effort: if it fails (permissions, network)
    time rendering degrades to UTC rather than failing the whole tool call.
    """
    try:
        company = await client.get_company()
    except DeputyError:
        company = None
    return resolve_timezone(company)


async def _area_map(client: DeputyClient) -> dict[int, str]:
    """Best-effort ``{area_id: name}`` map; empty when areas cannot be listed."""
    try:
        return areas_by_id(await client.get_operational_units())
    except DeputyError:
        return {}


async def _resolve_employee_id(client: DeputyClient, ref: str | None) -> int | None:
    """Resolve an employee reference (numeric id or name) to an id, or ``None``.

    A blank reference means "me" (``None`` — the caller resolves self). A numeric
    string is used directly; otherwise the first name match wins.
    """
    if ref is None or not ref.strip():
        return None
    text = ref.strip()
    if text.isdigit():
        return int(text)
    matches = await client.get_employees(search=text)
    if not matches or matches[0].Id is None:
        raise DeputyError(
            f"No employee found matching '{text}'.",
            hint="Try a numeric employee id or a different part of the name.",
        )
    return matches[0].Id


def register(mcp: FastMCP[Any], get_client: ClientProvider) -> None:
    """Register all read tools onto ``mcp`` (uses ``deputy_`` name prefix)."""
    read_only = {"readOnlyHint": True, "openWorldHint": True}

    @mcp.tool(
        name="deputy_whoami",
        annotations=read_only,
        description=(
            "Verify the Deputy connection and identity. Returns who the API token "
            "authenticates as plus the company/location and its timezone. Use this "
            "first to confirm setup before other tools."
        ),
    )
    async def deputy_whoami(
        response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
    ) -> str:
        client = get_client()
        try:
            who = await client.whoami()
            try:
                company = await client.get_company()
            except DeputyError:
                company = None
            _, tz_label = resolve_timezone(company)
            data = {"whoami": who, "company": company, "timezone": tz_label}
            return render(data, lambda: render_whoami(who, company, tz_label), response_format)
        except DeputyError as exc:
            return _error(exc)

    @mcp.tool(
        name="deputy_get_my_roster",
        annotations=read_only,
        description=(
            "List the signed-in user's own upcoming shifts in a date range. "
            "Defaults to today through the next 7 days. Dates are ISO YYYY-MM-DD. "
            "Use deputy_get_team_roster for other people's shifts."
        ),
    )
    async def deputy_get_my_roster(
        start_date: Annotated[str | None, Field(description="Start date (ISO).")] = None,
        end_date: Annotated[str | None, Field(description="End date (ISO).")] = None,
        response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
    ) -> str:
        client = get_client()
        try:
            start = _parse_date(start_date, _today())
            end = _parse_date(end_date, start + timedelta(days=7))
            rosters = await client.get_my_roster(start, end)
            tz, label = await resolve_client_timezone(client)
            areas = await _area_map(client)
            title = f"My roster ({start.isoformat()} to {end.isoformat()})"
            return render(
                rosters,
                lambda: render_roster_list(rosters, tz, label, title=title, areas=areas),
                response_format,
            )
        except DeputyError as exc:
            return _error(exc)

    @mcp.tool(
        name="deputy_get_team_roster",
        annotations=read_only,
        description=(
            "List every scheduled shift in a date range, optionally scoped to one "
            "area. Pass a single 'date' for one day, or 'start_date'/'end_date' for "
            "a range (defaults to today -> +7 days). 'area_id' filters to an area."
        ),
    )
    async def deputy_get_team_roster(
        date: Annotated[str | None, Field(description="Single day (ISO); overrides range.")] = None,
        start_date: Annotated[str | None, Field(description="Range start (ISO).")] = None,
        end_date: Annotated[str | None, Field(description="Range end (ISO).")] = None,
        area_id: Annotated[int | None, Field(description="OperationalUnit id filter.")] = None,
        response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
    ) -> str:
        client = get_client()
        try:
            if date is not None and date.strip():
                start = end = _parse_date(date, _today())
            else:
                start = _parse_date(start_date, _today())
                end = _parse_date(end_date, start + timedelta(days=7))
            rosters = await client.get_team_roster(start, end, area_id)
            tz, label = await resolve_client_timezone(client)
            areas = await _area_map(client)
            title = f"Team roster ({start.isoformat()} to {end.isoformat()})"
            return render(
                rosters,
                lambda: render_roster_list(rosters, tz, label, title=title, areas=areas),
                response_format,
            )
        except DeputyError as exc:
            return _error(exc)

    @mcp.tool(
        name="deputy_who_is_working",
        annotations=read_only,
        description=(
            "Show who is working now (or at a given instant): both who is physically "
            "clocked in (actual timesheets) and who is scheduled to be on (roster "
            "window). 'at' is an optional ISO datetime; defaults to now."
        ),
    )
    async def deputy_who_is_working(
        at: Annotated[str | None, Field(description="Instant to evaluate (ISO datetime).")] = None,
        response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
    ) -> str:
        client = get_client()
        try:
            moment = _parse_dt(at)
            data = await client.who_is_working(moment)
            tz, label = await resolve_client_timezone(client)
            areas = await _area_map(client)
            return render(
                data,
                lambda: render_who_is_working(data, tz, label, areas=areas),
                response_format,
            )
        except DeputyError as exc:
            return _error(exc)

    @mcp.tool(
        name="deputy_get_employee_info",
        annotations=read_only,
        description=(
            "Look up one or more employees by name (substring) or numeric id. "
            "Returns their documented profile fields (status, location, role id). "
            "Use deputy_get_areas to translate location/area ids into names."
        ),
    )
    async def deputy_get_employee_info(
        name_or_id: Annotated[str, Field(description="Employee name (substring) or numeric id.")],
        response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
    ) -> str:
        client = get_client()
        try:
            ref = name_or_id.strip()
            if ref.isdigit():
                employees = [await client.get_employee(int(ref))]
            else:
                employees = await client.get_employees(search=ref)
            areas = await _area_map(client)
            title = f"Employee info for '{name_or_id}'"
            return render(
                employees,
                lambda: render_employee_list(employees, title=title, areas=areas),
                response_format,
            )
        except DeputyError as exc:
            return _error(exc)

    @mcp.tool(
        name="deputy_search_shifts",
        annotations=read_only,
        description=(
            "Search shifts by any combination of employee (name or id), area, date "
            "range, and open/unassigned status. 'open_only' finds shifts nobody is "
            "assigned to yet. Use 'limit'/'offset' to page (max 500 per page)."
        ),
    )
    async def deputy_search_shifts(
        employee: Annotated[str | None, Field(description="Employee name or id.")] = None,
        area_id: Annotated[int | None, Field(description="OperationalUnit id filter.")] = None,
        start_date: Annotated[str | None, Field(description="Range start (ISO).")] = None,
        end_date: Annotated[str | None, Field(description="Range end (ISO).")] = None,
        open_only: Annotated[bool, Field(description="Only unassigned open shifts.")] = False,
        limit: Annotated[int, Field(ge=1, le=500, description="Max shifts (<=500).")] = 50,
        offset: Annotated[int, Field(ge=0, description="Records to skip (pagination).")] = 0,
        response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
    ) -> str:
        client = get_client()
        try:
            employee_id = None if open_only else await _resolve_employee_id(client, employee)
            start = _opt_date(start_date)
            end = _opt_date(end_date)
            rosters = await client.search_shifts(
                employee_id=employee_id,
                opunit_id=area_id,
                start=start,
                end=end,
                open_only=open_only,
                limit=limit,
                offset=offset,
            )
            tz, label = await resolve_client_timezone(client)
            areas = await _area_map(client)
            title = "Open shifts" if open_only else "Matching shifts"
            return render(
                rosters,
                lambda: render_roster_list(rosters, tz, label, title=title, areas=areas),
                response_format,
            )
        except DeputyError as exc:
            return _error(exc)

    @mcp.tool(
        name="deputy_get_areas",
        annotations=read_only,
        description=(
            "List all areas (operational units / work locations) with their ids. "
            "Use the ids to filter deputy_get_team_roster or deputy_search_shifts."
        ),
    )
    async def deputy_get_areas(
        response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
    ) -> str:
        client = get_client()
        try:
            units = await client.get_operational_units()
            return render(units, lambda: render_areas(units), response_format)
        except DeputyError as exc:
            return _error(exc)

    @mcp.tool(
        name="deputy_next_shift",
        annotations=read_only,
        description=(
            "Return the single next upcoming shift for an employee (name or id). "
            "Omit 'employee' to get your own next shift."
        ),
    )
    async def deputy_next_shift(
        employee: Annotated[str | None, Field(description="Employee name or id.")] = None,
        response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
    ) -> str:
        client = get_client()
        try:
            employee_id = await _resolve_employee_id(client, employee)
            roster = await client.next_shift(employee_id)
            tz, label = await resolve_client_timezone(client)
            areas = await _area_map(client)
            return render(
                roster,
                lambda: render_next_shift(roster, tz, label, areas=areas),
                response_format,
            )
        except DeputyError as exc:
            return _error(exc)

    @mcp.tool(
        name="deputy_get_my_timesheets",
        annotations=read_only,
        description=(
            "List the signed-in user's own timesheets (actual worked time) in a "
            "date range. Defaults to the last 7 days through today. Shows total "
            "hours worked and flags any timesheet still in progress."
        ),
    )
    async def deputy_get_my_timesheets(
        start_date: Annotated[str | None, Field(description="Start date (ISO).")] = None,
        end_date: Annotated[str | None, Field(description="End date (ISO).")] = None,
        response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
    ) -> str:
        client = get_client()
        try:
            end = _parse_date(end_date, date.today())
            start = _parse_date(start_date, end - timedelta(days=7))
            timesheets = await client.get_my_timesheets(start, end)
            tz, label = await resolve_client_timezone(client)
            areas = await _area_map(client)
            title = f"My timesheets ({start.isoformat()} to {end.isoformat()})"
            return render(
                timesheets,
                lambda: render_timesheet_list(timesheets, tz, label, title=title, areas=areas),
                response_format,
            )
        except DeputyError as exc:
            return _error(exc)
