"""Write operations mixin for the Deputy client.

:class:`WritesMixin` supplies the mutating half of ``DeputyClient`` (the read half
lives in :mod:`deputy_mcp.client.reads`; the two are composed in
:mod:`deputy_mcp.client`). Every method here is opt-in: it first checks
``config.allow_writes`` and raises :class:`DeputyWritesDisabledError` when writes are
disabled, so a misconfigured deployment can never mutate a live install by accident.
After any successful write the shared read cache is invalidated (Deputy is real-time;
a stale roster/timesheet would mislead the next read).

Permission reality (see ``deputy-api-write.md`` §5): these are ``/supervise`` manager
endpoints plus generic Resource writes. The token inherits the permissions of the
Deputy user who created it. When that user cannot perform an action Deputy returns
HTTP 403, which the transport maps to :class:`DeputyPermissionError` -- every method's
docstring calls this out rather than pretending the call always succeeds.

Deputy write conventions used below:

* ``/supervise/*`` bodies use imperative ``int*``/``bln*``/``str*`` keys and Unix
  timestamps in seconds.
* Resource writes (``/resource/{Object}``) use the object's PascalCase field names.
* There is **no** employee-facing "claim open shift" endpoint; claiming is modelled as
  a roster update (see :meth:`claim_open_shift`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar

from deputy_mcp.client.errors import DeputyError, DeputyWritesDisabledError
from deputy_mcp.client.models import DeputyModel, Roster, RosterSwap, Timesheet, Unavailability
from deputy_mcp.client.query import QueryBuilder, query_all

if TYPE_CHECKING:
    from deputy_mcp.client.http import DeputyHTTP
    from deputy_mcp.client.models import OperationalUnit

__all__ = ["WritesMixin"]

_ModelT = TypeVar("_ModelT", bound=DeputyModel)

#: RosterSwap.Status = 4 (Pending Approval): submitted and awaiting a manager, who
#: advances it to 5 (Approved). See deputy-api-write.md §2 for the full code table.
_SWAP_STATUS_PENDING_APPROVAL = 4

#: Valid iCal RRULE FREQ values Deputy accepts for recurring unavailability.
_RRULE_FREQS = frozenset({"WEEKLY", "MONTHLY"})

#: Valid RRULE BYDAY day tokens.
_RRULE_DAYS = frozenset({"MO", "TU", "WE", "TH", "FR", "SA", "SU"})


class WritesMixin:
    """Mutating operations mixed into ``DeputyClient``.

    This class is never instantiated on its own; it relies on members provided by the
    composed :class:`~deputy_mcp.client.DeputyClient`:

    * ``_http`` -- the :class:`~deputy_mcp.client.http.DeputyHTTP` transport (also the
      source of ``config`` and the cache ``invalidate`` hook).
    * ``own_employee_id()`` -- async accessor returning the authenticated user's
      ``Employee.Id`` (the client caches it after the first ``whoami``).
    * ``get_operational_units()`` -- the read mixin's area lookup, used only to
      auto-resolve a sole clock-in area.

    Those members are declared below under ``TYPE_CHECKING`` so static analysis sees
    them without this file importing the composition layer at runtime.
    """

    if TYPE_CHECKING:  # provided by the composed DeputyClient / ReadsMixin
        _http: DeputyHTTP

        async def own_employee_id(self) -> int: ...

        async def get_operational_units(self) -> list[OperationalUnit]: ...

    # -- public write operations -------------------------------------------

    async def claim_open_shift(self, roster_id: int) -> None:
        """Assign the current user to an open (unassigned) shift.

        Deputy exposes **no** employee-facing "claim/accept open shift" API endpoint.
        The documented way to fill an open roster is to update it: ``POST
        /supervise/roster`` with ``intRosterId`` set, ``intRosterEmployee`` set to the
        claimant, and ``blnOpen: 0`` to clear the open flag (deputy-api-write.md §1b).
        This bypasses the UI's employee-request workflow, so it needs a token whose
        Deputy user may edit that roster.

        The roster is read first, for two safety reasons:

        * ``/supervise/roster`` is the same add-or-update endpoint, and its only
          worked example always carries the shift's ``intStartTimestamp`` /
          ``intEndTimestamp`` / ``intOpunitId``. Those are echoed back on the update so
          an upsert cannot blank the shift's time/area.
        * If the shift is *not* open (a wrong id, or it was filled between listing and
          claiming), the method refuses rather than silently overwriting whoever is
          currently assigned -- which keeps ``destructiveHint=false`` truthful.

        Args:
            roster_id: ``Roster.Id`` of the open shift to take.

        Returns:
            ``None`` -- Deputy answers a successful roster update with HTTP 200 and an
            empty body, so there is nothing to parse. Re-read the roster if you need to
            confirm the assignment.

        Raises:
            DeputyWritesDisabledError: When ``DEPUTY_ALLOW_WRITES`` is not enabled.
            DeputyError: The shift is not an open shift (refusing to reassign it), or
                any other API/transport failure.
            DeputyPermissionError: HTTP 403 -- the user cannot edit this roster (for
                example a plain employee against an "open shift with approval" area,
                whose pick-up must be manager-approved via the UI).
            DeputyNotFoundError: HTTP 404 -- no roster with that id.
        """
        self._require_writes()
        employee_id = await self.own_employee_id()
        roster = await self._get_roster(roster_id)
        if not roster.Open:
            raise DeputyError(
                f"Shift {roster_id} is not an open shift; refusing to reassign it.",
                hint=(
                    "Only unassigned open shifts can be claimed. Check the shift id, or "
                    "use a shift-swap request to take over an already-assigned shift."
                ),
            )
        body: dict[str, Any] = {
            "intRosterId": roster_id,
            "intRosterEmployee": employee_id,
            "blnOpen": 0,
        }
        # Preserve the shift's existing time/area so a full upsert cannot blank them.
        if roster.StartTime is not None:
            body["intStartTimestamp"] = roster.StartTime
        if roster.EndTime is not None:
            body["intEndTimestamp"] = roster.EndTime
        if roster.OperationalUnit is not None:
            body["intOpunitId"] = roster.OperationalUnit
        await self._http.request("POST", "/supervise/roster", json_body=body)
        self._http.invalidate()

    async def request_shift_swap(self, roster_id: int, note: str | None = None) -> RosterSwap:
        """Create a shift-swap request for one of the current user's shifts.

        Modelled by the ``RosterSwap`` resource (deputy-api-write.md §2). There is no
        dedicated swap "submit" action endpoint, so the record is created via the
        generic Resource API with ``Status = 4`` (Pending Approval). A manager drives
        later transitions (``5`` Approved / ``7`` Declined); the exact accept/approve
        semantics are not fully documented by Deputy and should be validated live.

        This simplified tool models "offer my shift up for swap, pending approval"
        (``SourceRoster`` = your shift). It does **not** target a specific replacement
        shift/employee: ``TargetRoster`` is sent as ``0`` (no specific target). See the
        deviation note if your install rejects an untargeted swap.

        Args:
            roster_id: ``Roster.Id`` of the shift you want to give up (SourceRoster).
            note: Optional message stored as ``RequestMessage``.

        Returns:
            The created :class:`~deputy_mcp.client.models.RosterSwap`.

        Raises:
            DeputyWritesDisabledError: When writes are disabled.
            DeputyPermissionError: HTTP 403 -- the user may not create this swap.
            DeputyError: Any other API/transport failure (including a rejected body).
        """
        self._require_writes()
        employee_id = await self.own_employee_id()
        body: dict[str, Any] = {
            "SourceRoster": roster_id,
            "TargetRoster": 0,
            "Employee": employee_id,
            "Status": _SWAP_STATUS_PENDING_APPROVAL,
        }
        if note:
            body["RequestMessage"] = note
        data = await self._http.request("POST", "/resource/RosterSwap", json_body=body)
        self._http.invalidate()
        return _as_model(data, RosterSwap)

    async def set_unavailability(
        self,
        start: datetime,
        end: datetime,
        reason: str | None = None,
        repeat: str | None = None,
    ) -> Unavailability:
        """Record an unavailability window for the current user.

        ``POST /supervise/unavail`` with the documented shape (deputy-api-write.md §3):
        ``start``/``end`` are **objects** wrapping a Unix-seconds ``timestamp`` string,
        ``intAssignedEmployeeId`` is the target employee, and
        ``blnSubmitSuperUnavail: true`` submits it as an approved unavailability.

        Args:
            start: Window start. A naive datetime is interpreted as UTC.
            end: Window end (must be after ``start``).
            reason: Optional comment stored as ``strComment``.
            repeat: Optional iCal RRULE-style string for a recurring block, e.g.
                ``"FREQ=WEEKLY;INTERVAL=1;BYDAY=MO"`` (weekly on Monday) or
                ``"FREQ=MONTHLY;BYMONTHDAY=6"``. ``FREQ`` must be ``WEEKLY`` or
                ``MONTHLY``; omit for a one-off block.

        Returns:
            The created :class:`~deputy_mcp.client.models.Unavailability`
            (``Type`` 0 = one-off, 1 = recurring).

        Raises:
            DeputyWritesDisabledError: When writes are disabled.
            DeputyError: ``end`` not after ``start``, or a malformed ``repeat`` string.
            DeputyPermissionError: HTTP 403 -- the user may not set this unavailability.
        """
        self._require_writes()
        if end <= start:
            raise DeputyError(
                "Unavailability end must be after start.",
                hint="Pass an end datetime later than the start datetime.",
            )
        employee_id = await self.own_employee_id()
        body: dict[str, Any] = {
            "blnSubmitSuperUnavail": True,
            "intAssignedEmployeeId": employee_id,
            "start": {"timestamp": _to_unix_str(start)},
            "end": {"timestamp": _to_unix_str(end)},
        }
        if reason:
            body["strComment"] = reason
        if repeat:
            body["recurrence"] = _parse_recurrence(repeat)
        data = await self._http.request("POST", "/supervise/unavail", json_body=body)
        self._http.invalidate()
        return _as_model(data, Unavailability)

    async def clock_in(
        self, opunit_id: int | None = None, roster_id: int | None = None
    ) -> Timesheet:
        """Clock the current user in, starting a live timesheet.

        ``POST /supervise/timesheet/start`` needs only ``intEmployeeId`` and
        ``intOpunitId`` -- a roster is optional (deputy-api-write.md §4). If
        ``opunit_id`` is omitted this resolves the area automatically **only** when the
        install has exactly one rosterable area; otherwise it raises so the caller must
        name the area (clocking into the wrong location is a real-world hazard).

        Args:
            opunit_id: ``OperationalUnit.Id`` (the area/location) to clock into. When
                ``None``, auto-resolved iff there is a single rosterable area.
            roster_id: Optional ``Roster.Id`` to link the timesheet to a scheduled
                shift. A bad/foreign roster id is rejected by Deputy.

        Returns:
            The started :class:`~deputy_mcp.client.models.Timesheet`
            (``IsInProgress`` true). **Keep its ``Id`` -- it is required to clock out.**

        Raises:
            DeputyWritesDisabledError: When writes are disabled.
            DeputyError: When no ``opunit_id`` is given and the area cannot be resolved
                unambiguously.
            DeputyPermissionError: HTTP 403 -- the token's user is not a supervisor of
                this employee (an employee token can generally act on self only).
        """
        self._require_writes()
        employee_id = await self.own_employee_id()
        resolved_opunit = await self._resolve_clock_in_opunit(opunit_id)
        body: dict[str, Any] = {
            "intEmployeeId": employee_id,
            "intOpunitId": resolved_opunit,
        }
        if roster_id is not None:
            body["intRosterId"] = roster_id
        data = await self._http.request("POST", "/supervise/timesheet/start", json_body=body)
        self._http.invalidate()
        return _as_model(data, Timesheet)

    async def clock_out(
        self, timesheet_id: int | None = None, mealbreak_minutes: int | None = None
    ) -> Timesheet:
        """Clock out, ending a live timesheet.

        ``POST /supervise/timesheet/end`` keyed by ``intTimesheetId``, with optional
        ``intMealbreakMinute`` (deputy-api-write.md §4). When ``timesheet_id`` is
        omitted, the current user's single in-progress timesheet is looked up first
        (``Timesheet`` QUERY on ``Employee`` + ``IsInProgress`` true); a clear error is
        raised when none exists.

        Args:
            timesheet_id: ``Timesheet.Id`` to end. When ``None``, resolved from the
                user's in-progress timesheet.
            mealbreak_minutes: Optional unpaid meal-break length in minutes to record.

        Returns:
            The ended :class:`~deputy_mcp.client.models.Timesheet`
            (``IsInProgress`` false, ``EndTime`` populated).

        Raises:
            DeputyWritesDisabledError: When writes are disabled.
            DeputyError: No in-progress timesheet found (and none supplied), or a
                negative ``mealbreak_minutes``.
            DeputyPermissionError: HTTP 403 -- the user may not edit this timesheet.
        """
        self._require_writes()
        resolved_id = (
            timesheet_id if timesheet_id is not None else await self._find_in_progress_timesheet()
        )
        body: dict[str, Any] = {"intTimesheetId": resolved_id}
        if mealbreak_minutes is not None:
            if mealbreak_minutes < 0:
                raise DeputyError(
                    "mealbreak_minutes cannot be negative.",
                    hint="Pass 0 or a positive number of minutes, or omit it.",
                )
            body["intMealbreakMinute"] = mealbreak_minutes
        data = await self._http.request("POST", "/supervise/timesheet/end", json_body=body)
        self._http.invalidate()
        return _as_model(data, Timesheet)

    # -- internal helpers ---------------------------------------------------

    def _require_writes(self) -> None:
        """Gate: raise unless ``DEPUTY_ALLOW_WRITES`` is enabled."""
        if not self._http.config.allow_writes:
            raise DeputyWritesDisabledError()

    async def _resolve_clock_in_opunit(self, opunit_id: int | None) -> int:
        """Return an area id to clock into, auto-selecting a sole rosterable area.

        Auto-resolution is intentionally conservative: it succeeds only when exactly
        one candidate area exists, otherwise it raises so the caller picks explicitly.
        """
        if opunit_id is not None:
            return opunit_id
        units = await self.get_operational_units()
        candidates = [
            unit.Id
            for unit in units
            if unit.Id is not None and unit.Active is not False and unit.RosterActive is not False
        ]
        if len(candidates) == 1:
            return candidates[0]
        raise DeputyError(
            "Could not determine which area to clock in to "
            f"({len(candidates)} rosterable areas found).",
            hint="Pass an explicit area id (opunit_id); list areas with get_operational_units().",
        )

    async def _get_roster(self, roster_id: int) -> Roster:
        """Read a single roster by id (``GET /resource/Roster/{id}``)."""
        data = await self._http.request("GET", f"/resource/Roster/{roster_id}", cacheable=True)
        return _as_model(data, Roster)

    async def _find_in_progress_timesheet(self) -> int:
        """Find the current user's single open (in-progress) timesheet id.

        Raises when there is none *or* when more than one is open: clocking out the
        newest and silently leaving the others running would be a payroll hazard, and
        the tool promises to error when the situation is ambiguous.
        """
        employee_id = await self.own_employee_id()
        builder = (
            QueryBuilder()
            .where("Employee", "eq", employee_id)
            .where("IsInProgress", "eq", True)
            .sort("StartTime", desc=True)
            .max(50)
        )
        records = await query_all(self._http, "Timesheet", builder, hard_limit=50)
        if not records:
            raise DeputyError(
                "No in-progress timesheet found to clock out of.",
                hint="Clock in first, or pass an explicit timesheet_id.",
            )
        if len(records) > 1:
            open_ids = [r.get("Id") for r in records]
            raise DeputyError(
                f"Found {len(records)} in-progress timesheets ({open_ids}); "
                "cannot choose one unambiguously.",
                hint="Pass an explicit timesheet_id to clock out the intended timesheet.",
            )
        timesheet_id = records[0].get("Id")
        if not isinstance(timesheet_id, int):
            raise DeputyError(
                "The in-progress timesheet is missing a usable Id.",
                hint="Pass an explicit timesheet_id to clock out.",
            )
        return timesheet_id


def _as_model(data: Any, model_cls: type[_ModelT]) -> _ModelT:
    """Validate a Deputy write response into ``model_cls``.

    Deputy write endpoints return the affected record as a JSON object; a few wrap it
    in a single-element list. Anything else (empty/None/non-dict) is surfaced as a
    clear error rather than silently producing an empty model.
    """
    record = data[0] if isinstance(data, list) and data else data
    if not isinstance(record, dict):
        raise DeputyError(
            f"Deputy returned an unexpected response for {model_cls.__name__}.",
            hint="The write may still have applied; re-read to confirm.",
        )
    return model_cls.model_validate(record)


def _to_unix_str(moment: datetime) -> str:
    """Convert a datetime to Unix seconds as a string (Deputy's documented form).

    A naive datetime (no tzinfo) is treated as UTC so the result is deterministic and
    independent of the host clock's timezone.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return str(int(moment.timestamp()))


def _parse_recurrence(repeat: str) -> dict[str, Any]:
    """Parse an iCal RRULE-style string into Deputy's ``recurrence`` object.

    Accepts ``KEY=VALUE`` pairs separated by ``;`` (case-insensitive keys), e.g.
    ``"FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE"``. Supported keys: ``FREQ`` (required,
    ``WEEKLY``/``MONTHLY``), ``INTERVAL`` (positive int, default 1), ``BYDAY``
    (comma-separated MO..SU), ``BYMONTHDAY`` (int). Raises :class:`DeputyError` on any
    malformed input so the failure is actionable rather than a silent bad payload.
    """
    fields: dict[str, str] = {}
    for token in repeat.split(";"):
        token = token.strip()
        if not token:
            continue
        if "=" not in token:
            raise DeputyError(
                f"Malformed recurrence token '{token}'.",
                hint="Use KEY=VALUE pairs, e.g. FREQ=WEEKLY;INTERVAL=1;BYDAY=MO.",
            )
        key, value = token.split("=", 1)
        fields[key.strip().upper()] = value.strip()

    freq = fields.get("FREQ", "").upper()
    if freq not in _RRULE_FREQS:
        raise DeputyError(
            f"Recurrence FREQ must be one of {sorted(_RRULE_FREQS)}.",
            hint="Example: FREQ=WEEKLY;INTERVAL=1;BYDAY=MO",
        )
    recurrence: dict[str, Any] = {"FREQ": freq, "INTERVAL": _parse_interval(fields)}

    byday = fields.get("BYDAY")
    if byday:
        days = [day.strip().upper() for day in byday.split(",") if day.strip()]
        invalid = [day for day in days if day not in _RRULE_DAYS]
        if invalid:
            raise DeputyError(
                f"Invalid recurrence BYDAY value(s): {invalid}.",
                hint=f"Use two-letter days from {sorted(_RRULE_DAYS)}.",
            )
        recurrence["BYDAY"] = ",".join(days)

    bymonthday = fields.get("BYMONTHDAY")
    if bymonthday is not None:
        recurrence["BYMONTHDAY"] = _parse_monthday(bymonthday)

    return recurrence


def _parse_interval(fields: dict[str, str]) -> int:
    """Parse ``INTERVAL`` (default 1); must be a positive integer."""
    raw = fields.get("INTERVAL")
    if raw is None:
        return 1
    try:
        interval = int(raw)
    except ValueError as exc:
        raise DeputyError(
            f"Recurrence INTERVAL must be an integer, got '{raw}'.",
            hint="Example: INTERVAL=2 for fortnightly.",
        ) from exc
    if interval < 1:
        raise DeputyError(
            "Recurrence INTERVAL must be >= 1.",
            hint="Use 1 for weekly, 2 for fortnightly, etc.",
        )
    return interval


def _parse_monthday(raw: str) -> int:
    """Parse ``BYMONTHDAY`` as a day-of-month (1..31)."""
    try:
        day = int(raw)
    except ValueError as exc:
        raise DeputyError(
            f"Recurrence BYMONTHDAY must be an integer, got '{raw}'.",
            hint="Use a day of the month, e.g. BYMONTHDAY=6.",
        ) from exc
    if not 1 <= day <= 31:
        raise DeputyError(
            f"Recurrence BYMONTHDAY must be between 1 and 31, got {day}.",
            hint="Use a valid day of the month.",
        )
    return day
