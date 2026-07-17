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

from deputy_mcp.client.errors import DeputyError, DeputyNotFoundError
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

__all__ = ["EMPLOYEE_JOIN", "ReadsMixin"]

#: Association name used to eager-load the assigned employee onto a Roster/Timesheet
#: (both the QUERY ``join`` sent here and the key the response is read back under, in
#: :mod:`deputy_mcp.server.formatting` and :mod:`deputy_mcp.cli`). "EmployeeObject" is
#: the documented example name but the join/assoc naming is a smoke-test gap in the API
#: notes (deputy-api-read.md §1.1) and MUST be confirmed against a live install; keeping
#: it in one place means a correction propagates to every send/parse site at once.
EMPLOYEE_JOIN = "EmployeeObject"

#: WhoAmI keys, in priority order, that may carry the caller's Employee.Id.
_WHOAMI_EMPLOYEE_KEYS = ("EmployeeId", "Employee", "employeeId", "EmployeeID")

_ModelT = TypeVar("_ModelT", bound=BaseModel)


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


def _employee_id_from_whoami(who: WhoAmI) -> int:
    """Extract the caller's ``Employee.Id`` from a WhoAmI response.

    WhoAmI has no documented schema, so the employee id lives in the model's
    extra fields. Probe the known key spellings and coerce a digit string.
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


class ReadsMixin:
    """Read methods mixed into :class:`DeputyClient` (never used standalone)."""

    # Supplied by DeputyClient; declared for type checking only.
    _http: DeputyHTTP
    _own_employee_id: int | None

    async def whoami(self) -> WhoAmI:
        """Return the authenticated user's account/employee info.

        Uses ``GET /resource/Account/WhoAmI`` (Deputy's canonical "Hello World"
        call), falling back to ``GET /me`` only if WhoAmI is not found. The
        response schema is install-dependent, so all values live under the
        model's extra attributes.
        """
        try:
            data = await self._http.request("GET", "/resource/Account/WhoAmI", cacheable=True)
        except DeputyNotFoundError:
            data = await self._http.request("GET", "/me", cacheable=True)
        if isinstance(data, list) and data:
            data = data[0]
        return WhoAmI.model_validate(data)

    async def own_employee_id(self) -> int:
        """Return (and cache) the caller's ``Employee.Id`` via :meth:`whoami`."""
        if self._own_employee_id is None:
            self._own_employee_id = _employee_id_from_whoami(await self.whoami())
        return self._own_employee_id

    async def get_my_roster(self, start: date, end: date) -> list[Roster]:
        """Return the caller's own shifts within ``[start, end]`` (inclusive).

        Uses ``Roster/QUERY`` filtered by the caller's own ``Employee.Id`` and a
        ``Date`` range (the fallback the design mandates). The documented
        ``GET /my/roster`` endpoint is the future-only "when am I next working" view:
        it has no range parameters and cannot answer a past-or-arbitrary range, and
        its 200 response shape is unverified (an object-wrapped array would be
        silently coerced to "no shifts"). Querying the Roster resource by employee id
        avoids both traps and matches :meth:`get_team_roster`.
        """
        employee_id = await self.own_employee_id()
        builder = (
            QueryBuilder()
            .where("Employee", "eq", employee_id)
            .where("Date", "ge", start.isoformat())
            .where("Date", "le", end.isoformat())
            .join(EMPLOYEE_JOIN)
            .sort("StartTime")
        )
        records = await query_all(self._http, "Roster", builder)
        return _as_models(records, Roster)

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
        records = await query_all(self._http, "Roster", builder)
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
        records = await query_all(self._http, "Employee", builder)
        return _as_models(records, Employee)

    async def get_employee(self, employee_id: int) -> Employee:
        """Fetch a single employee by id (``GET /resource/Employee/{id}``)."""
        data = await self._http.request("GET", f"/resource/Employee/{employee_id}", cacheable=True)
        return Employee.model_validate(data)

    async def get_operational_units(self) -> list[OperationalUnit]:
        """List all areas (operational units), sorted by name."""
        builder = QueryBuilder().sort("OperationalUnitName")
        records = await query_all(self._http, "OperationalUnit", builder)
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
        records = await query_all(
            self._http, "Roster", builder, hard_limit=page_size, warn_on_truncate=False
        )
        return _as_models(records, Roster)

    async def next_shift(self, employee_id: int | None = None) -> Roster | None:
        """Return the next upcoming shift for an employee (self if ``None``).

        Finds the earliest ``Roster`` whose ``StartTime`` is after now for the
        target employee; returns ``None`` when there is no future shift.
        """
        target = employee_id if employee_id is not None else await self.own_employee_id()
        builder = (
            QueryBuilder()
            .where("StartTime", "gt", _now_unix(None))
            .where("Employee", "eq", target)
            .join(EMPLOYEE_JOIN)
            .sort("StartTime")
            .max(1)
        )
        records = await query_all(
            self._http, "Roster", builder, hard_limit=1, warn_on_truncate=False
        )
        rosters = _as_models(records, Roster)
        return rosters[0] if rosters else None

    async def get_my_timesheets(self, start: date, end: date) -> list[Timesheet]:
        """Return the caller's own timesheets within ``[start, end]`` (inclusive).

        Uses ``Timesheet/QUERY`` filtered by the caller's own ``Employee.Id`` and a
        ``Date`` range (the design's documented alternative to ``GET /my/timesheets``).
        The ``/my/timesheets`` endpoint's 200 response shape is unverified, so an
        object-wrapped array would be silently coerced to "no timesheets"; querying the
        Timesheet resource by employee id is both range-correct and shape-safe.
        """
        employee_id = await self.own_employee_id()
        builder = (
            QueryBuilder()
            .where("Employee", "eq", employee_id)
            .where("Date", "ge", start.isoformat())
            .where("Date", "le", end.isoformat())
            .join(EMPLOYEE_JOIN)
            .sort("StartTime")
        )
        records = await query_all(self._http, "Timesheet", builder)
        return _as_models(records, Timesheet)

    async def get_company(self) -> Company:
        """Return the install's primary company/location (timezone source).

        Deputy overloads "Company" to mean a location; the first record is used
        as the install's primary location for timezone-aware rendering.
        """
        records = await query_all(self._http, "Company", QueryBuilder().sort("Id"))
        if not records:
            raise DeputyNotFoundError("No company/location was found for this Deputy install.")
        return Company.model_validate(records[0])
