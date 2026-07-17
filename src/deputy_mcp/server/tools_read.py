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
from datetime import timedelta, tzinfo
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from deputy_mcp.client import DeputyClient, DeputyError
from deputy_mcp.client.whoami import (
    whoami_calendar_url,
    whoami_company_name,
    whoami_is_clocked_in,
)
from deputy_mcp.server._read_helpers import (
    format_error,
    opt_date,
    parse_date,
    parse_dt,
    today,
)
from deputy_mcp.server.formatting import (
    ResponseFormat,
    areas_by_id,
    employee_display,
    render,
    render_areas,
    render_calendar_url,
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
    """Resolve an employee reference (numeric id or name) to a single id, or ``None``.

    A blank reference means "me" (``None`` — the caller resolves self). A numeric
    string is used directly. A name is matched against active employees: exactly one
    match resolves to that id; zero or several raise an actionable :class:`DeputyError`.
    The ambiguous case lists every match with its id so the caller can retry by id,
    instead of silently picking the first (which could act on the wrong person).
    """
    if ref is None or not ref.strip():
        return None
    text = ref.strip()
    if text.isdigit():
        return int(text)
    # get_employees already filters to active employees; keep those with a usable id.
    matches = [emp for emp in await client.get_employees(search=text) if emp.Id is not None]
    if not matches:
        raise DeputyError(
            f"No employee found matching '{text}'.",
            hint="Try a numeric employee id or a different part of the name.",
        )
    if len(matches) > 1:
        listing = "; ".join(f"{employee_display(emp)} (id {emp.Id})" for emp in matches)
        raise DeputyError(
            f"Multiple employees match '{text}': {listing}.",
            hint="Re-run with the numeric employee id of the person you mean.",
        )
    return matches[0].Id


def register(mcp: FastMCP[Any], get_client: ClientProvider) -> None:
    """Register all read tools onto ``mcp`` (uses ``deputy_`` name prefix)."""
    read_only = {"readOnlyHint": True, "openWorldHint": True}

    @mcp.tool(name="deputy_whoami", annotations=read_only)
    async def deputy_whoami(
        response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
    ) -> str:
        """Verify the Deputy connection and confirm which identity the token uses.

        Returns who the API token authenticates as, the company/location and its timezone,
        whether that user is clocked in right now (from their in-progress timesheet), and
        their personal iCal calendar subscription URL when the install exposes one. Run
        this first to confirm setup before other tools.

        When NOT to use: to read schedules or people (use deputy_get_my_roster or
        deputy_get_employee_info) — this only checks the connection and identity.

        Returns markdown (a connection summary: signed-in name, company, timezone, whether
        clocked in, and the calendar feed) or, with response_format="json", an object
        ``{"whoami", "company", "timezone", "company_name", "clocked_in", "calendar_url"}``
        where ``clocked_in`` is a bool and ``calendar_url`` is a string or null.
        """
        client = get_client()
        try:
            who = await client.whoami()
            try:
                company = await client.get_company()
            except DeputyError:
                company = None
            _, tz_label = resolve_timezone(company)
            clocked_in = whoami_is_clocked_in(who)
            calendar_url = whoami_calendar_url(who)
            company_name = (
                company.CompanyName or company.TradingName if company is not None else None
            ) or whoami_company_name(who)
            data = {
                "whoami": who,
                "company": company,
                "timezone": tz_label,
                "company_name": company_name,
                "clocked_in": clocked_in,
                "calendar_url": calendar_url,
            }
            return render(
                data,
                lambda: render_whoami(
                    who, company, tz_label, clocked_in=clocked_in, calendar_url=calendar_url
                ),
                response_format,
            )
        except DeputyError as exc:
            return format_error(exc)

    @mcp.tool(name="deputy_get_my_calendar_url", annotations=read_only)
    async def deputy_get_my_calendar_url(
        response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
    ) -> str:
        """Return the signed-in user's personal Deputy calendar (iCal) subscription URL.

        Deputy publishes a per-user, read-only iCal feed of your roster (the CalendarURL
        on /api/v1/me). Add the returned link to any calendar app (Google Calendar, Apple
        Calendar, Outlook) to see your shifts there; it stays in sync as your roster
        changes. Works at any access level — it reads only your own /me record.

        When NOT to use: to read the shifts themselves here (use deputy_get_my_roster or
        deputy_next_shift) — this only returns the subscription link.

        Returns markdown (the subscription link, or a note that this install exposes none)
        or, with response_format="json", an object ``{"calendar_url"}`` whose value is a
        string or null.
        """
        client = get_client()
        try:
            url = whoami_calendar_url(await client.whoami())
            return render({"calendar_url": url}, lambda: render_calendar_url(url), response_format)
        except DeputyError as exc:
            return format_error(exc)

    @mcp.tool(name="deputy_get_my_roster", annotations=read_only)
    async def deputy_get_my_roster(
        start_date: Annotated[str | None, Field(description="Start date (ISO).")] = None,
        end_date: Annotated[str | None, Field(description="End date (ISO).")] = None,
        response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
    ) -> str:
        """List the signed-in user's own upcoming shifts in a date range.

        Defaults to today through the next 7 days, computed in UTC; dates are ISO
        YYYY-MM-DD.

        When NOT to use: for other people's shifts (use deputy_get_team_roster) or for
        worked time (use deputy_get_my_timesheets) — this returns your scheduled shifts.

        Returns markdown (a shift list, times in the install timezone) or, with
        response_format="json", a list of Roster records.
        """
        client = get_client()
        try:
            start = parse_date(start_date, today())
            end = parse_date(end_date, start + timedelta(days=7))
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
            return format_error(exc)

    @mcp.tool(name="deputy_get_team_roster", annotations=read_only)
    async def deputy_get_team_roster(
        date: Annotated[str | None, Field(description="Single day (ISO); overrides range.")] = None,
        start_date: Annotated[str | None, Field(description="Range start (ISO).")] = None,
        end_date: Annotated[str | None, Field(description="Range end (ISO).")] = None,
        area_id: Annotated[int | None, Field(description="OperationalUnit id filter.")] = None,
        response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
    ) -> str:
        """List every scheduled shift in a date range, optionally scoped to one area.

        Pass a single 'date' for one day, or 'start_date'/'end_date' for a range
        (defaults to today through +7 days, computed in UTC). 'area_id' filters to one
        operational unit.

        When NOT to use: to see who is actually clocked in right now (use
        deputy_who_is_working) — this lists the schedule, not live attendance.

        Returns markdown (a shift list, times in the install timezone) or, with
        response_format="json", a list of Roster records.
        """
        client = get_client()
        try:
            if date is not None and date.strip():
                start = end = parse_date(date, today())
            else:
                start = parse_date(start_date, today())
                end = parse_date(end_date, start + timedelta(days=7))
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
            return format_error(exc)

    @mcp.tool(name="deputy_who_is_working", annotations=read_only)
    async def deputy_who_is_working(
        at: Annotated[str | None, Field(description="Instant to evaluate (ISO datetime).")] = None,
        response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
    ) -> str:
        """Show who is working at an instant: physically clocked in vs scheduled on.

        Reconciles two signals — who is clocked in (actual timesheets) and who is
        rostered on (schedule window). 'at' is an optional ISO datetime; it defaults to
        now (UTC).

        When NOT to use: for a whole day's or week's schedule (use
        deputy_get_team_roster) — this is a single-instant snapshot.

        Returns markdown (two lists: on the clock, and rostered now) or, with
        response_format="json", an object ``{"at", "clocked_in": [Timesheet],
        "rostered_now": [Roster]}``.
        """
        client = get_client()
        try:
            moment = parse_dt(at)
            data = await client.who_is_working(moment)
            tz, label = await resolve_client_timezone(client)
            areas = await _area_map(client)
            return render(
                data,
                lambda: render_who_is_working(data, tz, label, areas=areas),
                response_format,
            )
        except DeputyError as exc:
            return format_error(exc)

    @mcp.tool(name="deputy_get_employee_info", annotations=read_only)
    async def deputy_get_employee_info(
        name_or_id: Annotated[str, Field(description="Employee name (substring) or numeric id.")],
        response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
    ) -> str:
        """Look up one or more employees by name (substring) or numeric id.

        Returns each match's documented profile fields (status, location, role id). A
        name may match several people; every match is listed with its id so you can
        follow up by id.

        When NOT to use: to find someone's shifts (use deputy_search_shifts or
        deputy_next_shift) — this returns profiles, not schedules. Use deputy_get_areas
        to translate location/area ids into names.

        Returns markdown (an employee list with ids) or, with response_format="json", a
        list of Employee records.
        """
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
            return format_error(exc)

    @mcp.tool(name="deputy_search_shifts", annotations=read_only)
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
        """Search shifts by employee (name or id), area, date range, and open status.

        'open_only' finds shifts nobody is assigned to yet. 'limit'/'offset' page the
        results (max 500 per page). A name given as 'employee' must resolve to exactly
        one active person; otherwise the matches are listed for you to retry by id.

        When NOT to use: for just your own upcoming shifts (use deputy_get_my_roster) or
        only the single next one (use deputy_next_shift).

        Returns markdown (a shift list plus a paging hint) or, with
        response_format="json", an object ``{"shifts": [Roster], "pagination": {limit,
        offset, returned, has_more, next_offset}}``.
        """
        client = get_client()
        try:
            employee_id = None if open_only else await _resolve_employee_id(client, employee)
            start = opt_date(start_date)
            end = opt_date(end_date)
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
            # A full page (returned == limit) means more shifts may exist. Surface a
            # pagination cursor so the caller does not treat one page as the total.
            has_more = len(rosters) >= limit
            next_offset = offset + limit if has_more else None
            data = {
                "shifts": rosters,
                "pagination": {
                    "limit": limit,
                    "offset": offset,
                    "returned": len(rosters),
                    "has_more": has_more,
                    "next_offset": next_offset,
                },
            }

            def _markdown() -> str:
                body = render_roster_list(rosters, tz, label, title=title, areas=areas)
                if has_more:
                    body += (
                        f"\n\n_More shifts may exist. Call again with offset="
                        f"{next_offset} to see the next page._"
                    )
                return body

            return render(data, _markdown, response_format)
        except DeputyError as exc:
            return format_error(exc)

    @mcp.tool(name="deputy_get_areas", annotations=read_only)
    async def deputy_get_areas(
        response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
    ) -> str:
        """List all areas (operational units / work locations) with their ids.

        Use the ids to filter deputy_get_team_roster or deputy_search_shifts.

        When NOT to use: to list shifts or people — this only enumerates locations.

        Returns markdown (an area list with ids) or, with response_format="json", a
        list of OperationalUnit records.
        """
        client = get_client()
        try:
            units = await client.get_operational_units()
            return render(units, lambda: render_areas(units), response_format)
        except DeputyError as exc:
            return format_error(exc)

    @mcp.tool(name="deputy_next_shift", annotations=read_only)
    async def deputy_next_shift(
        employee: Annotated[str | None, Field(description="Employee name or id.")] = None,
        response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
    ) -> str:
        """Return the single next upcoming shift for an employee (name or id).

        Omit 'employee' to get your own next shift. A name must resolve to exactly one
        active person; otherwise the matches are listed for you to retry by id.

        When NOT to use: for a full range of upcoming shifts (use deputy_get_my_roster
        or deputy_search_shifts) — this returns only the earliest one.

        Returns markdown (one shift, or a 'none scheduled' note) or, with
        response_format="json", a single Roster record or null.
        """
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
            return format_error(exc)

    @mcp.tool(name="deputy_get_my_timesheets", annotations=read_only)
    async def deputy_get_my_timesheets(
        start_date: Annotated[str | None, Field(description="Start date (ISO).")] = None,
        end_date: Annotated[str | None, Field(description="End date (ISO).")] = None,
        response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
    ) -> str:
        """List the signed-in user's own timesheets (actual worked time) in a range.

        Defaults to the last 7 days through today, computed in UTC. Shows total hours
        worked and flags any timesheet still in progress.

        When NOT to use: for scheduled (not yet worked) time (use deputy_get_my_roster)
        — timesheets record actual attendance, not the plan.

        Returns markdown (a timesheet list with a worked-hours total) or, with
        response_format="json", a list of Timesheet records.
        """
        client = get_client()
        try:
            end = parse_date(end_date, today())
            start = parse_date(start_date, end - timedelta(days=7))
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
            return format_error(exc)
