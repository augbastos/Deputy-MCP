"""Dual-format (markdown / JSON) rendering helpers for the MCP layer.

Tools, resources and the CLI share these helpers so the same shift/timesheet/
employee is rendered identically everywhere. Every tool exposes a
``response_format`` argument; :func:`render` switches between a human markdown
view and a machine JSON view of the same data.

Times are rendered in the install's local timezone. Deputy stores
``StartTime``/``EndTime`` as unix UTC seconds; :func:`resolve_timezone` derives a
:class:`~datetime.tzinfo` from the company/location record (falling back to UTC,
and degrading gracefully when the local tz database is unavailable — e.g. a
Windows host without ``tzdata``), and :func:`fmt_ts` formats a timestamp in it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, tzinfo
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel

from deputy_mcp.client.models import (
    Company,
    Employee,
    OperationalUnit,
    Roster,
    Timesheet,
)
from deputy_mcp.client.reads import EMPLOYEE_JOIN

__all__ = [
    "ResponseFormat",
    "areas_by_id",
    "employee_display",
    "fmt_ts",
    "render",
    "render_areas",
    "render_calendar_url",
    "render_calendar_url_ical",
    "render_employee_list",
    "render_next_shift",
    "render_roster_list",
    "render_timesheet_list",
    "render_who_is_working",
    "render_whoami",
    "render_whoami_ical",
    "resolve_timezone",
    "to_json",
]

#: Accepted values for a tool's ``response_format`` argument.
ResponseFormat = Literal["markdown", "json"]

#: Company/location keys that may carry an IANA timezone name (probed in order).
_TZ_KEYS = ("Timezone", "TimeZone", "Time_zone", "TimezoneName", "TimeZoneName")


def resolve_timezone(company: Company | None) -> tuple[tzinfo, str]:
    """Return the install timezone and a human label for it.

    The exact timezone field on Deputy's ``Company`` object is not documented, so
    a small set of plausible keys is probed in the model's extra attributes. When
    a name is found but the local tz database cannot resolve it (common on
    Windows without the ``tzdata`` package), the clock falls back to UTC while the
    label preserves the intended zone so the output stays honest.
    """
    if company is not None:
        extra = company.model_extra or {}
        for key in _TZ_KEYS:
            raw = extra.get(key)
            if isinstance(raw, str) and raw.strip():
                name = raw.strip()
                try:
                    return ZoneInfo(name), name
                except (ZoneInfoNotFoundError, ValueError, OSError):
                    return UTC, f"{name} (shown in UTC)"
    return UTC, "UTC"


def fmt_ts(unix: int | None, tz: tzinfo) -> str:
    """Format a unix-seconds timestamp in ``tz`` as ``YYYY-MM-DD HH:MM``."""
    if unix is None:
        return "unknown"
    return datetime.fromtimestamp(unix, tz=tz).strftime("%Y-%m-%d %H:%M")


def areas_by_id(units: list[OperationalUnit]) -> dict[int, str]:
    """Build an ``{OperationalUnit.Id: name}`` lookup for area rendering."""
    mapping: dict[int, str] = {}
    for unit in units:
        if unit.Id is not None:
            mapping[unit.Id] = unit.OperationalUnitName or f"Area #{unit.Id}"
    return mapping


# --------------------------------------------------------------------------- #
# JSON output
# --------------------------------------------------------------------------- #
def _jsonable(value: Any) -> Any:
    """Recursively convert pydantic models/containers to JSON-safe values."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def to_json(data: Any) -> str:
    """Serialize ``data`` (models, dicts, lists) to indented JSON text."""
    return json.dumps(_jsonable(data), indent=2, default=str)


def render(
    data: Any,
    markdown_fn: Callable[[], str],
    response_format: ResponseFormat,
) -> str:
    """Return JSON for ``data`` or the markdown produced by ``markdown_fn``."""
    if response_format == "json":
        return to_json(data)
    return markdown_fn()


# --------------------------------------------------------------------------- #
# Shared field helpers
# --------------------------------------------------------------------------- #
def employee_display(emp: Employee | None) -> str:
    """Best-effort display name for an employee record."""
    if emp is None:
        return "Unassigned"
    if emp.DisplayName:
        return emp.DisplayName
    name = " ".join(part for part in (emp.FirstName, emp.LastName) if part)
    return name or (f"Employee #{emp.Id}" if emp.Id is not None else "Unknown")


def _joined_employee(obj: Roster | Timesheet) -> Employee | None:
    """Extract the eager-loaded employee join (see :data:`EMPLOYEE_JOIN`), if present."""
    raw: Any = (obj.model_extra or {}).get(EMPLOYEE_JOIN)
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if isinstance(raw, dict):
        return Employee.model_validate(raw)
    return None


def _person(obj: Roster | Timesheet) -> str:
    """Resolve who a shift/timesheet belongs to (name, id, or 'Open shift')."""
    emp = _joined_employee(obj)
    if emp is not None:
        return employee_display(emp)
    if obj.Employee:
        return f"Employee #{obj.Employee}"
    # Reached only when a record has no assigned employee AND was not flagged open
    # (open shifts are handled by the caller's ``Open`` check). "Unassigned" is honest
    # here — notably for an iCal-derived shift, which carries no employee field.
    return "Unassigned"


def _area(opunit_id: int | None, areas: Mapping[int, str] | None) -> str:
    """Resolve an operational-unit id to a name via ``areas`` (or a placeholder)."""
    if opunit_id is None:
        return "no area"
    if areas and opunit_id in areas:
        return areas[opunit_id]
    return f"Area #{opunit_id}"


def _area_label(obj: Roster | Timesheet, areas: Mapping[int, str] | None) -> str:
    """Resolve a shift/timesheet's area name from the best source available.

    Prefers the id -> name map (populated only when the admin ``OperationalUnit`` list is
    reachable), then the area embedded on the record itself as ``OperationalUnitObject``.
    Both ``/api/v1/my/roster`` and the iCal feed carry that embedded object, so a plain
    employee (who cannot list areas) and an iCal-mode caller both see the real area name
    rather than a bare id — and the output is identical whichever source the roster came
    from. Falls back to :func:`_area`.
    """
    opunit_id = obj.OperationalUnit
    if opunit_id is not None and areas and opunit_id in areas:
        return areas[opunit_id]
    embedded = (obj.model_extra or {}).get("OperationalUnitObject")
    if isinstance(embedded, dict):
        name = embedded.get("OperationalUnitName")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return _area(opunit_id, areas)


def _hours(value: float | None) -> str:
    """Format a shift length in hours, or ``?`` when unknown."""
    return f"{value:.2f}h" if value is not None else "?"


# --------------------------------------------------------------------------- #
# Markdown renderers
# --------------------------------------------------------------------------- #
def _roster_line(r: Roster, tz: tzinfo, areas: Mapping[int, str] | None) -> str:
    who = "Open shift" if r.Open else _person(r)
    flag = " **[OPEN]**" if r.Open else ""
    times = f"{fmt_ts(r.StartTime, tz)} - {fmt_ts(r.EndTime, tz)}"
    return f"- {times} | {who} | {_area_label(r, areas)} | {_hours(r.TotalTime)}{flag}"


def render_roster_list(
    rosters: list[Roster],
    tz: tzinfo,
    tz_label: str,
    *,
    title: str = "Roster",
    areas: Mapping[int, str] | None = None,
) -> str:
    """Render a list of shifts as a markdown section (times in ``tz_label``)."""
    if not rosters:
        return f"### {title}\n\n_No shifts found._"
    lines = [f"### {title}", "", f"_Times shown in {tz_label}._", ""]
    lines += [_roster_line(r, tz, areas) for r in rosters]
    return "\n".join(lines)


def _timesheet_line(t: Timesheet, tz: tzinfo, areas: Mapping[int, str] | None) -> str:
    end = "in progress" if t.EndTime is None else fmt_ts(t.EndTime, tz)
    live = " **[on the clock]**" if t.IsInProgress else ""
    times = f"{fmt_ts(t.StartTime, tz)} - {end}"
    area = _area_label(t, areas)
    return f"- {times} | {_person(t)} | {area} | {_hours(t.TotalTime)}{live}"


def render_timesheet_list(
    timesheets: list[Timesheet],
    tz: tzinfo,
    tz_label: str,
    *,
    title: str = "Timesheets",
    areas: Mapping[int, str] | None = None,
) -> str:
    """Render worked-time records as a markdown section."""
    if not timesheets:
        return f"### {title}\n\n_No timesheets found._"
    worked = sum(t.TotalTime for t in timesheets if t.TotalTime is not None)
    lines = [f"### {title}", "", f"_Times shown in {tz_label}. Total worked: {worked:.2f}h._", ""]
    lines += [_timesheet_line(t, tz, areas) for t in timesheets]
    return "\n".join(lines)


def render_who_is_working(
    data: Mapping[str, Any],
    tz: tzinfo,
    tz_label: str,
    *,
    areas: Mapping[int, str] | None = None,
) -> str:
    """Render the reconciled 'who is working now' view."""
    clocked: list[Timesheet] = list(data.get("clocked_in", []))
    rostered: list[Roster] = list(data.get("rostered_now", []))
    at = data.get("at", "now")
    lines = [f"### Who is working ({at})", "", f"_Times shown in {tz_label}._", ""]
    lines.append(f"**On the clock ({len(clocked)})** — actual clock-ins:")
    lines += [_timesheet_line(t, tz, areas) for t in clocked] or ["- _Nobody clocked in._"]
    lines += ["", f"**Rostered now ({len(rostered)})** — scheduled to be on:"]
    lines += [_roster_line(r, tz, areas) for r in rostered] or ["- _Nobody scheduled._"]
    return "\n".join(lines)


def render_employee_list(
    employees: list[Employee],
    *,
    title: str = "Employees",
    areas: Mapping[int, str] | None = None,
) -> str:
    """Render employee records with their documented fields."""
    if not employees:
        return f"### {title}\n\n_No matching employees._"
    lines = [f"### {title}", ""]
    for emp in employees:
        status = "active" if emp.Active else "archived"
        location = _area(emp.Company, areas) if emp.Company is not None else "no location"
        role = f", role #{emp.Role}" if emp.Role is not None else ""
        lines.append(f"- **{employee_display(emp)}** (id {emp.Id}) — {status}, {location}{role}")
    return "\n".join(lines)


def render_areas(units: list[OperationalUnit]) -> str:
    """Render operational units (areas)."""
    if not units:
        return "### Areas\n\n_No areas found._"
    lines = ["### Areas", ""]
    for unit in units:
        state = "active" if unit.Active else "inactive"
        lines.append(f"- **{unit.OperationalUnitName or 'Unnamed'}** (id {unit.Id}) — {state}")
    return "\n".join(lines)


def render_next_shift(
    roster: Roster | None,
    tz: tzinfo,
    tz_label: str,
    *,
    areas: Mapping[int, str] | None = None,
) -> str:
    """Render a single upcoming shift, or a 'none scheduled' note."""
    if roster is None:
        return "### Next shift\n\n_No upcoming shift scheduled._"
    return "\n".join(
        ["### Next shift", "", f"_Times shown in {tz_label}._", "", _roster_line(roster, tz, areas)]
    )


def render_whoami(
    who: Any,
    company: Company | None,
    tz_label: str,
    *,
    clocked_in: bool | None = None,
    calendar_url: str | None = None,
) -> str:
    """Render the WhoAmI + company sanity-check summary.

    ``clocked_in`` adds a live "on the clock" line (from ``/me``'s in-progress timesheet)
    when provided; ``calendar_url`` adds the personal iCal subscription link when present.
    """
    extra = getattr(who, "model_extra", None) or {}
    name = extra.get("Name") or extra.get("DisplayName") or extra.get("FirstName") or "unknown"
    company_name = None
    if company is not None:
        company_name = company.CompanyName or company.TradingName
    lines = [
        "### Deputy connection OK",
        "",
        f"- Signed in as: **{name}**",
        f"- Company/location: {company_name or 'unknown'}",
        f"- Timezone: {tz_label}",
    ]
    if clocked_in is not None:
        lines.append(f"- Clocked in now: {'yes' if clocked_in else 'no'}")
    if calendar_url:
        lines.append(f"- Calendar feed (iCal subscription): {calendar_url}")
    return "\n".join(lines)


def render_calendar_url(url: str | None) -> str:
    """Render the personal iCal subscription link (or a graceful 'none' note)."""
    if not url:
        return (
            "### My calendar feed\n\n_This Deputy install does not expose a personal iCal "
            "calendar URL for your account._"
        )
    return (
        "### My calendar feed\n\n"
        "Add this iCal subscription link to your calendar app (Google Calendar, Apple "
        "Calendar, Outlook) to see your shifts:\n\n"
        f"- {url}"
    )


def render_whoami_ical() -> str:
    """Render the identity/status summary for iCal mode (no API token).

    iCal mode has no authenticated API identity to report — it reads only the caller's own
    roster from their personal calendar feed. This states the mode and the honest scope
    without pretending to know the signed-in name, company or clock state (all API-only).
    """
    return "\n".join(
        [
            "### Deputy connection OK (iCal mode)",
            "",
            "- Mode: **iCal** — reading your personal Deputy calendar feed (no API token).",
            "- Available: your own roster only — `deputy_get_my_roster`, `deputy_next_shift`.",
            (
                "- Not available here: team roster, timesheets, who is working, employee "
                "lookup and areas. Those need a Deputy API token (set DEPUTY_API_TOKEN and "
                "DEPUTY_BASE_URL)."
            ),
        ]
    )


def render_calendar_url_ical() -> str:
    """Render the calendar-feed note for iCal mode without revealing the secret URL.

    In iCal mode the feed URL *is* the configured secret (``DEPUTY_CALENDAR_URL``); it
    carries a private token and is never printed. This confirms it is configured and is
    the source of the roster here, and points callers at the roster tools for the shifts.
    """
    return (
        "### My calendar feed\n\n"
        "You are running in iCal mode: your personal Deputy calendar feed is already "
        "configured (DEPUTY_CALENDAR_URL) and is the source of your roster here. For your "
        "safety the feed URL carries a private token and is not shown. Use "
        "`deputy_get_my_roster` or `deputy_next_shift` to see your shifts."
    )
