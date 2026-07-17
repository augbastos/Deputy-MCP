"""Pure accessors over a Deputy ``/api/v1/me`` (WhoAmI) response.

The live smoke run (``smoke-findings.md``) confirmed that ``GET /api/v1/me`` is the
"who am I" call that works for ANY access level, and that its useful values live at the
top level or in embedded objects: ``EmployeeId`` (int), ``CompanyObject`` (with
``CompanyName``/timezone), ``InProgressTS`` (the caller's running timesheet),
``CalendarURL`` (a ready-made iCal feed) and ``Name``/``FirstName``/``LastName``.

:class:`~deputy_mcp.client.models.WhoAmI` declares no fields on purpose (the schema is
install-dependent), so these functions read through ``model_extra`` and defensively
coerce the value shapes Deputy has been observed to use. They never raise on a shape
they do not recognise except :func:`employee_id_from_whoami`, which must succeed for the
"act as me" tools and so raises an actionable error when no id can be found.
"""

from __future__ import annotations

from deputy_mcp.client.errors import DeputyError
from deputy_mcp.client.models import WhoAmI

__all__ = [
    "employee_id_from_whoami",
    "in_progress_timesheet_id",
    "whoami_calendar_url",
    "whoami_company_name",
    "whoami_display_name",
    "whoami_is_clocked_in",
]

#: WhoAmI keys, in priority order, that may carry the caller's Employee.Id.
_WHOAMI_EMPLOYEE_KEYS = ("EmployeeId", "Employee", "employeeId", "EmployeeID")


def employee_id_from_whoami(who: WhoAmI) -> int:
    """Extract the caller's ``Employee.Id`` from a ``/me`` response.

    ``/api/v1/me`` returns ``EmployeeId`` as a top-level int; the other spellings are
    probed for install variance. Raises when none is present (the token's user may not
    be linked to an employee record).
    """
    extra = who.model_extra or {}
    for key in _WHOAMI_EMPLOYEE_KEYS:
        value = extra.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value)
    raise DeputyError(
        "Could not determine your employee id from the Deputy WhoAmI response.",
        hint=(
            "The token's user may not be linked to an employee record. "
            "Pass an explicit employee id, or check the account in Deputy."
        ),
    )


def whoami_display_name(who: WhoAmI) -> str | None:
    """Return the caller's display name (``Name``/``DisplayName``, else first+last)."""
    extra = who.model_extra or {}
    for key in ("Name", "DisplayName"):
        value = extra.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    parts = [
        part.strip()
        for part in (extra.get("FirstName"), extra.get("LastName"))
        if isinstance(part, str) and part.strip()
    ]
    return " ".join(parts) if parts else None


def whoami_company_name(who: WhoAmI) -> str | None:
    """Return the company/location name from ``CompanyObject.CompanyName`` (or flat)."""
    extra = who.model_extra or {}
    obj = extra.get("CompanyObject")
    if isinstance(obj, dict):
        name = obj.get("CompanyName")
        if isinstance(name, str) and name.strip():
            return name.strip()
    name = extra.get("CompanyName")
    return name.strip() if isinstance(name, str) and name.strip() else None


def whoami_calendar_url(who: WhoAmI) -> str | None:
    """Return the caller's ``CalendarURL`` (a ready-made iCal feed), or ``None``."""
    value = (who.model_extra or {}).get("CalendarURL")
    return value.strip() if isinstance(value, str) and value.strip() else None


def in_progress_timesheet_id(who: WhoAmI) -> int | None:
    """Return the id of the caller's currently-running timesheet from ``InProgressTS``.

    ``/api/v1/me`` carries ``InProgressTS`` — the caller's single in-progress timesheet
    (a falsy value when not clocked in). Deputy may render it as a bare id or a nested
    timesheet object, so both are probed. Returns ``None`` (never raises) when the caller
    is not clocked in or the shape is unrecognised.
    """
    value = (who.model_extra or {}).get("InProgressTS")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    if isinstance(value, dict):
        for key in ("Id", "TimesheetId", "id"):
            nested = value.get(key)
            if isinstance(nested, bool):
                continue
            if isinstance(nested, int) and nested > 0:
                return nested
            if isinstance(nested, str) and nested.strip().isdigit():
                parsed = int(nested)
                if parsed > 0:
                    return parsed
    return None


def whoami_is_clocked_in(who: WhoAmI) -> bool:
    """Return whether the caller currently has an in-progress (running) timesheet."""
    return in_progress_timesheet_id(who) is not None
