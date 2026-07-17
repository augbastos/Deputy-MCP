"""Read operations for the Deputy client, as a mixin on :class:`DeputyClient`.

Every method here is a typed, async read. Filtered searches go through the
Resource ``/QUERY`` DSL (:mod:`deputy_mcp.client.query`) and paginate past the
500-record cap; the ``/my/*`` convenience endpoints and single-object GETs use
the transport directly. All reads are marked ``cacheable`` so repeated tool
calls within the short cache TTL do not re-hit Deputy.

Time semantics follow the read notes: ``StartTime``/``EndTime`` are unix UTC
seconds (compared as strings in QUERY bodies), while ``Date`` is the local
business-day string (``YYYY-MM-DD``) used for calendar-range filtering.

The mixin relies on two members supplied by :class:`DeputyClient`:
``_http`` (the transport) and ``_own_employee_id`` (the whoami-derived cache
backing :meth:`own_employee_id`). They are declared here for type checking.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, TypeVar

from pydantic import BaseModel

from deputy_mcp.client.errors import DeputyNotFoundError, DeputyPermissionError
from deputy_mcp.client.http import DeputyHTTP
from deputy_mcp.client.models import (
    Company,
    Employee,
    OperationalUnit,
    Roster,
    Timesheet,
    WhoAmI,
)
from deputy_mcp.client.query import QueryBuilder, query_all
from deputy_mcp.client.whoami import (
    employee_id_from_whoami,
    in_progress_timesheet_id,
    whoami_calendar_url,
    whoami_company_name,
    whoami_display_name,
    whoami_is_clocked_in,
)

__all__ = [
    "EMPLOYEE_JOIN",
    "ReadsMixin",
    "in_progress_timesheet_id",
    "whoami_calendar_url",
    "whoami_company_name",
    "whoami_display_name",
    "whoami_is_clocked_in",
]

#: Association name used to eager-load the assigned employee onto a Roster/Timesheet
#: (both the QUERY ``join`` sent here and the key the response is read back under, in
#: :mod:`deputy_mcp.server.formatting` and :mod:`deputy_mcp.cli`). "EmployeeObject" is
#: the documented example name but the join/assoc naming is a smoke-test gap in the API
#: notes (deputy-api-read.md §1.1) and MUST be confirmed against a live install; keeping
#: it in one place means a correction propagates to every send/parse site at once.
EMPLOYEE_JOIN = "EmployeeObject"

_ModelT = TypeVar("_ModelT", bound=BaseModel)

#: Constrained to the two shift-like models so the client-side date filter stays typed
#: while working for both Roster (``/my/roster``) and Timesheet (``/my/timesheets``).
_ShiftT = TypeVar("_ShiftT", Roster, Timesheet)

#: The self-service tools that work for a plain employee token, named in every
#: manager-only permission error so the model is redirected to something that works.
_SELF_SERVICE_TOOLS = "get_my_roster, next_shift (your own), get_my_timesheets"


def _now_unix(at: datetime | None) -> str:
    """Return the epoch-seconds string for ``at`` (or now) for QUERY comparisons.

    Deputy compares unix timestamps sent as strings. A naive ``at`` is treated
    as UTC; ``None`` means the current instant.
    """
    moment = at if at is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return str(int(moment.timestamp()))


def _as_models(records: list[dict[str, Any]], model: type[_ModelT]) -> list[_ModelT]:
    """Validate a list of raw Deputy records into typed models."""
    return [model.model_validate(record) for record in records]


def _utc_today() -> date:
    """Return today's date in UTC (used to decide whether a range reaches the past)."""
    return datetime.now(UTC).date()


def _filter_by_date(items: list[_ShiftT], start: date, end: date) -> list[_ShiftT]:
    """Keep records whose local business ``Date`` is within ``[start, end]`` and sort them.

    ``/my/roster`` and ``/my/timesheets`` return every record the caller can see with no
    range parameters, so the window is applied client-side. ``Date`` is a ``YYYY-MM-DD``
    string whose lexicographic order matches calendar order, so string comparison is
    exact. When ``Date`` is missing the UTC date of ``StartTime`` is used as a fallback.
    Records with neither are dropped rather than guessed at.
    """
    lo, hi = start.isoformat(), end.isoformat()
    kept: list[_ShiftT] = []
    for item in items:
        day = item.Date
        if day is None and item.start_dt is not None:
            day = item.start_dt.date().isoformat()
        if day is not None and lo <= day <= hi:
            kept.append(item)
    kept.sort(key=lambda item: item.StartTime if item.StartTime is not None else 0)
    return kept


def _manager_only_error(action: str) -> DeputyPermissionError:
    """Build the actionable 403 for a manager/admin-only read hit by an employee token.

    The self-service tools that DO work at an employee access level are named so the
    caller (or the model driving it) is redirected instead of shown a raw 403.
    """
    return DeputyPermissionError(
        f"To {action} you need a manager or administrator access level on this Deputy "
        "install, but this API token's Deputy user has a standard employee access level "
        "(Deputy returned HTTP 403 'Access to object-type denied').",
        hint=(
            "Use the self-service tools that work at your access level: "
            f"{_SELF_SERVICE_TOOLS}. To use the team/manager tools, an administrator must "
            "raise the token user's access level, or supply a manager/admin API token."
        ),
    )


class ReadsMixin:
    """Read methods mixed into :class:`DeputyClient` (never used standalone)."""

    # Supplied by DeputyClient; declared for type checking only.
    _http: DeputyHTTP
    _own_employee_id: int | None

    async def whoami(self) -> WhoAmI:
        """Return the authenticated user's account/employee info.

        Uses ``GET /api/v1/me`` as the primary call: it returns the caller's identity
        (top-level ``EmployeeId``, ``CompanyObject``, ``InProgressTS``, ``CalendarURL``,
        ``Permissions``) and works for ANY access level. The documented
        ``GET /resource/Account/WhoAmI`` 404s on installs that lack it, so it is only a
        fallback here (the reverse of the previous ordering). The schema is
        install-dependent; read derived values through the ``deputy_mcp.client.whoami``
        accessor functions rather than declared fields.
        """
        try:
            data = await self._http.request("GET", "/me", cacheable=True)
        except DeputyNotFoundError:
            data = await self._http.request("GET", "/resource/Account/WhoAmI", cacheable=True)
        if isinstance(data, list) and data:
            data = data[0]
        return WhoAmI.model_validate(data)

    async def own_employee_id(self) -> int:
        """Return (and cache) the caller's ``Employee.Id`` via :meth:`whoami`."""
        if self._own_employee_id is None:
            self._own_employee_id = employee_id_from_whoami(await self.whoami())
        return self._own_employee_id

    async def _my_roster(self) -> list[Roster]:
        """Fetch the caller's own upcoming shifts from ``GET /api/v1/my/roster``.

        This self-service endpoint works for ANY employee token and returns a bare JSON
        array of full Roster records with the area embedded as ``OperationalUnitObject``
        (no join needed). It is future-only and takes no range parameters, so callers
        filter client-side. A non-array response degrades to an empty list.
        """
        data = await self._http.request("GET", "/my/roster", cacheable=True)
        records = [rec for rec in data if isinstance(rec, dict)] if isinstance(data, list) else []
        return _as_models(records, Roster)

    async def get_my_roster(self, start: date, end: date) -> list[Roster]:
        """Return the caller's own shifts within ``[start, end]`` (inclusive).

        PRIMARY source is ``GET /api/v1/my/roster`` — the only roster endpoint a plain
        employee token can reach. It is future-only, so it fully answers the common
        "my week" case (``start`` today or later). When the requested window reaches into
        the PAST (``start`` before today) an admin-only ``Roster/QUERY`` is attempted for
        the historical part; a 403 there (a non-admin employee) is NOT surfaced — it
        degrades to ``/my/roster`` filtered to the window, so "my week" never fails with a
        raw permission error.
        """
        if start < _utc_today():
            employee_id = await self.own_employee_id()
            builder = (
                QueryBuilder()
                .where("Employee", "eq", employee_id)
                .where("Date", "ge", start.isoformat())
                .where("Date", "le", end.isoformat())
                .join(EMPLOYEE_JOIN)
                .sort("StartTime")
            )
            try:
                records = await query_all(self._http, "Roster", builder)
            except DeputyPermissionError:
                return _filter_by_date(await self._my_roster(), start, end)
            return _as_models(records, Roster)
        return _filter_by_date(await self._my_roster(), start, end)

    async def get_team_roster(
        self, start: date, end: date, opunit_id: int | None = None
    ) -> list[Roster]:
        """Return all shifts in a date range, optionally scoped to one area.

        Runs ``Roster/QUERY`` with a ``Date`` range (inclusive) and an optional
        ``OperationalUnit`` filter, eager-loading the assigned employee.
        """
        builder = (
            QueryBuilder()
            .where("Date", "ge", start.isoformat())
            .where("Date", "le", end.isoformat())
        )
        if opunit_id is not None:
            builder.where("OperationalUnit", "eq", opunit_id)
        builder.join(EMPLOYEE_JOIN).sort("StartTime")
        try:
            records = await query_all(self._http, "Roster", builder)
        except DeputyPermissionError as exc:
            raise _manager_only_error("read the whole team's roster") from exc
        return _as_models(records, Roster)

    async def who_is_working(self, at: datetime | None = None) -> dict[str, Any]:
        """Reconcile who is working "now" from two independent signals.

        Returns a dict with:

        * ``at`` — the ISO instant evaluated (UTC).
        * ``clocked_in`` — ``list[Timesheet]`` with ``IsInProgress`` true whose
          ``StartTime`` is at or before ``at`` (physically on the clock as of
          ``at``, not a clock-in that only started afterwards).
        * ``rostered_now`` — ``list[Roster]`` whose window contains ``at``, are
          published (drafts excluded), and are not open/unassigned (who is
          *scheduled* to be on).

        Both queries honor ``at`` so the two lists describe the same instant.
        Scheduled and actual diverge (late clock-in, no-shows); callers decide how
        to combine them.
        """
        moment = at if at is not None else datetime.now(UTC)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        now = str(int(moment.timestamp()))

        # Bound the clock-in signal by ``at`` too: an in-progress timesheet that
        # started after the requested instant was not on the clock then.
        clocked_builder = (
            QueryBuilder()
            .where("IsInProgress", "eq", True)
            .where("StartTime", "le", now)
            .join(EMPLOYEE_JOIN)
        )
        # Both signals read every employee's timesheets/rosters, so both need a
        # manager/admin access level. A non-admin employee 403s here -> surface the
        # actionable manager-only error instead of a raw permission failure.
        try:
            clocked_records = await query_all(self._http, "Timesheet", clocked_builder)

            # Published filter excludes draft shifts (deputy-api-read.md §5 Strategy B):
            # a drafted-but-unpublished shift is not really "scheduled on" right now.
            rostered_builder = (
                QueryBuilder()
                .where("StartTime", "le", now)
                .where("EndTime", "gt", now)
                .where("Published", "eq", True)
                .where("Open", "ne", True)
                .join(EMPLOYEE_JOIN)
            )
            rostered_records = await query_all(self._http, "Roster", rostered_builder)
        except DeputyPermissionError as exc:
            raise _manager_only_error("see who across the team is working now") from exc

        return {
            "at": moment.isoformat(),
            "clocked_in": _as_models(clocked_records, Timesheet),
            "rostered_now": _as_models(rostered_records, Roster),
        }

    async def get_employees(
        self, search: str | None = None, active_only: bool = True
    ) -> list[Employee]:
        """List employees, optionally filtered by a name substring.

        ``search`` matches ``DisplayName`` with a ``LIKE`` (substring) filter;
        the raw QUERY API cannot OR across name fields, and DisplayName holds the
        full name. ``active_only`` excludes archived employees.
        """
        builder = QueryBuilder()
        if search:
            builder.where("DisplayName", "lk", f"%{search}%")
        if active_only:
            builder.where("Active", "eq", True)
        builder.sort("DisplayName")
        try:
            records = await query_all(self._http, "Employee", builder)
        except DeputyPermissionError as exc:
            raise _manager_only_error("list employees") from exc
        return _as_models(records, Employee)

    async def get_employee(self, employee_id: int) -> Employee:
        """Fetch a single employee by id (``GET /resource/Employee/{id}``).

        Reading an arbitrary employee's profile is a manager/admin capability; a plain
        employee token 403s, which is turned into the actionable manager-only error.
        """
        try:
            data = await self._http.request(
                "GET", f"/resource/Employee/{employee_id}", cacheable=True
            )
        except DeputyPermissionError as exc:
            raise _manager_only_error("look up another employee's profile") from exc
        return Employee.model_validate(data)

    async def _areas_from_my_roster(self) -> list[OperationalUnit]:
        """Derive the distinct areas the caller works from ``/my/roster`` (self-service).

        Each ``/my/roster`` record embeds its area as ``OperationalUnitObject`` (with the
        name), so the areas an employee actually works can be listed without the
        admin-only ``OperationalUnit/QUERY``. Falls back to a name-less unit built from the
        bare ``OperationalUnit`` id when the embedded object is absent.
        """
        seen: dict[int, OperationalUnit] = {}
        for roster in await self._my_roster():
            obj = (roster.model_extra or {}).get("OperationalUnitObject")
            if isinstance(obj, dict):
                unit = OperationalUnit.model_validate(obj)
                if unit.Id is not None and unit.Id not in seen:
                    seen[unit.Id] = unit
            elif roster.OperationalUnit is not None and roster.OperationalUnit not in seen:
                seen[roster.OperationalUnit] = OperationalUnit(Id=roster.OperationalUnit)
        return sorted(seen.values(), key=lambda unit: (unit.OperationalUnitName or "").lower())

    async def get_operational_units(self) -> list[OperationalUnit]:
        """List areas (operational units), sorted by name.

        Admin path is ``OperationalUnit/QUERY`` (every area on the install). A non-admin
        employee 403s there, so this degrades to the areas the caller actually works,
        derived from the ``OperationalUnitObject`` embedded in ``/my/roster`` — a useful
        self-service answer instead of a permission error.
        """
        builder = QueryBuilder().sort("OperationalUnitName")
        try:
            records = await query_all(self._http, "OperationalUnit", builder)
        except DeputyPermissionError:
            return await self._areas_from_my_roster()
        return _as_models(records, OperationalUnit)

    async def search_shifts(
        self,
        employee_id: int | None = None,
        opunit_id: int | None = None,
        start: date | None = None,
        end: date | None = None,
        open_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Roster]:
        """Search shifts (``Roster/QUERY``) by any combination of filters.

        ``open_only`` restricts to unassigned open shifts (which have no
        employee, so it is mutually exclusive with an employee filter).
        ``limit`` is clamped to Deputy's 500-record page cap; ``offset`` skips
        records for pagination.
        """
        page_size = max(1, min(limit, 500))
        builder = QueryBuilder()
        if open_only:
            builder.where("Open", "eq", True)
        elif employee_id is not None:
            builder.where("Employee", "eq", employee_id)
        if opunit_id is not None:
            builder.where("OperationalUnit", "eq", opunit_id)
        if start is not None:
            builder.where("Date", "ge", start.isoformat())
        if end is not None:
            builder.where("Date", "le", end.isoformat())
        builder.join(EMPLOYEE_JOIN).sort("StartTime")
        builder.max(page_size).start(max(0, offset))
        try:
            records = await query_all(
                self._http, "Roster", builder, hard_limit=page_size, warn_on_truncate=False
            )
        except DeputyPermissionError as exc:
            raise _manager_only_error("search the whole install's shifts") from exc
        return _as_models(records, Roster)

    async def next_shift(self, employee_id: int | None = None) -> Roster | None:
        """Return the next upcoming shift for an employee (self if ``None``).

        For SELF (``employee_id`` is ``None``) this derives from ``GET /api/v1/my/roster``
        — the self-service, future-only feed — picking the earliest shift starting after
        now. That works for any employee token and needs no ``Employee`` lookup.

        For ANOTHER employee it must read across people via ``Roster/QUERY`` (a
        manager/admin capability); a non-admin employee 403s there, surfaced as the
        actionable manager-only error. Returns ``None`` when there is no future shift.
        """
        if employee_id is None:
            now_epoch = int(_now_unix(None))
            upcoming = [
                roster
                for roster in await self._my_roster()
                if roster.StartTime is not None and roster.StartTime > now_epoch
            ]
            upcoming.sort(key=lambda roster: roster.StartTime or 0)
            return upcoming[0] if upcoming else None

        builder = (
            QueryBuilder()
            .where("StartTime", "gt", _now_unix(None))
            .where("Employee", "eq", employee_id)
            .join(EMPLOYEE_JOIN)
            .sort("StartTime")
            .max(1)
        )
        try:
            records = await query_all(
                self._http, "Roster", builder, hard_limit=1, warn_on_truncate=False
            )
        except DeputyPermissionError as exc:
            raise _manager_only_error("look up another employee's next shift") from exc
        rosters = _as_models(records, Roster)
        return rosters[0] if rosters else None

    async def _my_timesheets(self) -> list[Timesheet]:
        """Fetch the caller's own timesheets from ``GET /api/v1/my/timesheets``.

        Self-service endpoint reachable by any employee token; returns a bare JSON array
        (empty when the caller has logged none). A non-array response degrades to empty.
        """
        data = await self._http.request("GET", "/my/timesheets", cacheable=True)
        records = [rec for rec in data if isinstance(rec, dict)] if isinstance(data, list) else []
        return _as_models(records, Timesheet)

    async def get_my_timesheets(self, start: date, end: date) -> list[Timesheet]:
        """Return the caller's own timesheets within ``[start, end]`` (inclusive).

        PRIMARY source is ``GET /api/v1/my/timesheets`` — the endpoint an employee token
        can reach — filtered client-side to the window. Only when that yields nothing for
        a window reaching into the PAST is an admin ``Timesheet/QUERY`` attempted for
        deeper history; a 403 there (a non-admin employee) is NOT surfaced — it degrades
        to the ``/my`` data, so the common "my recent timesheets" case never fails with a
        raw permission error.
        """
        filtered = _filter_by_date(await self._my_timesheets(), start, end)
        if filtered or start >= _utc_today():
            return filtered
        # Empty result over a past window: an admin token can pull deeper history than
        # /my exposes; a non-admin 403s -> degrade to the (empty) self-service data.
        employee_id = await self.own_employee_id()
        builder = (
            QueryBuilder()
            .where("Employee", "eq", employee_id)
            .where("Date", "ge", start.isoformat())
            .where("Date", "le", end.isoformat())
            .join(EMPLOYEE_JOIN)
            .sort("StartTime")
        )
        try:
            records = await query_all(self._http, "Timesheet", builder)
        except DeputyPermissionError:
            return filtered
        return _as_models(records, Timesheet)

    async def _company_from_me(self) -> Company | None:
        """Return the install's company/location from ``/me``'s ``CompanyObject``.

        ``/api/v1/me`` embeds ``CompanyObject`` (Id, CompanyName, timezone, ...) and is
        readable by any employee token, unlike the admin-only ``Company/QUERY``. Returns
        ``None`` when the install's ``/me`` does not embed it.
        """
        obj = ((await self.whoami()).model_extra or {}).get("CompanyObject")
        return Company.model_validate(obj) if isinstance(obj, dict) else None

    async def get_company(self) -> Company:
        """Return the install's primary company/location (timezone source).

        Deputy overloads "Company" to mean a location. PRIMARY source is the
        ``CompanyObject`` embedded in ``/api/v1/me`` — readable by any employee token, so
        an employee never hits the admin-only ``Company/QUERY`` (which 403s). Falls back
        to ``Company/QUERY`` (admins, or installs whose ``/me`` omits ``CompanyObject``),
        and only then raises when nothing is found.
        """
        company = await self._company_from_me()
        if company is not None:
            return company
        try:
            records = await query_all(self._http, "Company", QueryBuilder().sort("Id"))
        except DeputyPermissionError:
            records = []
        if not records:
            raise DeputyNotFoundError("No company/location was found for this Deputy install.")
        return Company.model_validate(records[0])
