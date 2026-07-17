"""MCP write tools for Deputy (opt-in, gated behind ``DEPUTY_ALLOW_WRITES``).

This module registers the five *mutating* tools. It is imported and wired by
:mod:`deputy_mcp.server.app` **only when** ``config.allow_writes`` is true, so the
write tools are entirely invisible to a client when writes are disabled -- the
safest default for a workforce system a language model can drive.

Every tool is a thin, honest wrapper over a :class:`~deputy_mcp.client.DeputyClient`
write method (see :mod:`deputy_mcp.client.writes`). The client layer owns the Deputy
API reality; the tool layer owns argument validation, actionable error text, and
dual markdown/JSON rendering via :mod:`deputy_mcp.server.formatting`. A raw traceback
is never returned to the model: any :class:`~deputy_mcp.client.errors.DeputyError`
becomes a short, actionable string.

Tool annotations (MCP hints, per the design): ``readOnlyHint=false`` (they change
state), ``destructiveHint=false`` (they create/assign, never delete),
``idempotentHint=false`` (calling twice is not a no-op -- e.g. two clock-ins), and
``openWorldHint=true`` (they reach an external system, the Deputy install).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from deputy_mcp.client.errors import DeputyError
from deputy_mcp.server.formatting import ResponseFormat, fmt_ts, render
from deputy_mcp.server.tools_read import resolve_client_timezone

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from deputy_mcp.client import DeputyClient

__all__ = ["register"]

#: Zero-argument provider handed to :func:`register`; returns the shared, already-open
#: :class:`~deputy_mcp.client.DeputyClient` that ``app.py`` builds once in its lifespan.
ClientProvider = Callable[[], "DeputyClient"]

#: MCP behaviour hints shared by all write tools (see module docstring).
_WRITE_ANNOTATIONS: dict[str, Any] = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": True,
}

#: RosterSwap.Status integer -> human label (see deputy-api-write.md §2).
_SWAP_STATUS_LABELS: dict[int | None, str] = {
    0: "Not required",
    1: "Pending Out",
    2: "Pending In",
    3: "Pending In Out",
    4: "Pending Approval",
    5: "Approved",
    6: "Cancelled",
    7: "Declined",
}


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
def register(mcp: FastMCP, get_client: ClientProvider) -> None:
    """Register the five Deputy write tools on ``mcp``.

    Called by :func:`deputy_mcp.server.app.create_server` **only** when
    ``DEPUTY_ALLOW_WRITES`` is enabled, so absence of these tools is itself the
    write-disabled signal to a connected client.

    Args:
        mcp: The :class:`fastmcp.FastMCP` server to attach the tools to.
        get_client: Zero-arg provider returning the shared, open ``DeputyClient``.
    """

    @mcp.tool(
        name="deputy_claim_open_shift",
        annotations={**_WRITE_ANNOTATIONS, "title": "Claim open shift"},
    )
    async def deputy_claim_open_shift(
        shift_id: Annotated[
            int,
            Field(description="Roster.Id of the open (unassigned) shift to take.", gt=0),
        ],
        response_format: ResponseFormat = "markdown",
    ) -> str:
        """Assign the signed-in user to an open shift.

        Deputy has **no** employee "accept open shift" API, so this fills the open
        roster by updating it (sets the employee, clears the open flag). It therefore
        needs a token whose Deputy user may edit that shift and it **bypasses** any
        "open shift with approval" request flow -- the assignment is applied directly.

        When NOT to use: to *offer* a shift to others, or when a manager must approve
        pick-ups (that pathway is UI-only and not exposed here).

        Returns markdown (a confirmation; Deputy returns an empty body on success, so
        re-read the roster to verify it stuck) or, with response_format="json", the
        object ``{"shift_id", "claimed"}``.

        Args:
            shift_id: The open shift's ``Roster.Id``.
            response_format: ``markdown`` (default) or ``json``.
        """
        try:
            client = get_client()
            await client.claim_open_shift(shift_id)
        except DeputyError as exc:
            return _format_error(exc)
        data: dict[str, Any] = {"shift_id": shift_id, "claimed": True}
        return render(data, lambda: _md_claim(data), response_format)

    @mcp.tool(
        name="deputy_request_shift_swap",
        annotations={**_WRITE_ANNOTATIONS, "title": "Request shift swap"},
    )
    async def deputy_request_shift_swap(
        shift_id: Annotated[
            int,
            Field(description="Roster.Id of your shift to offer up for swap.", gt=0),
        ],
        note: Annotated[
            str | None,
            Field(description="Optional message stored with the swap request.", max_length=500),
        ] = None,
        response_format: ResponseFormat = "markdown",
    ) -> str:
        """Offer one of the signed-in user's shifts up for swap, pending approval.

        Creates a ``RosterSwap`` record with status *Pending Approval* (4). A manager
        drives later transitions (approve -> 5, decline -> 7); this tool only submits
        the request, it does not approve anything. The swap is untargeted (no specific
        replacement shift) -- some installs may reject that; see the tool's error.

        When NOT to use: to approve/decline an existing swap (manager action, not
        exposed), or to claim an open shift (use ``deputy_claim_open_shift``).

        Returns markdown (a confirmation of the submitted request) or, with
        response_format="json", the object ``{"swap_id", "source_shift_id", "status",
        "status_label", "note"}``.

        Args:
            shift_id: ``Roster.Id`` of the shift you want to give up.
            note: Optional request message.
            response_format: ``markdown`` (default) or ``json``.
        """
        try:
            client = get_client()
            swap = await client.request_shift_swap(shift_id, note)
        except DeputyError as exc:
            return _format_error(exc)
        record = swap.model_dump(mode="json")
        data = {
            "swap_id": record.get("Id"),
            "source_shift_id": record.get("SourceRoster"),
            "status": record.get("Status"),
            "status_label": _SWAP_STATUS_LABELS.get(record.get("Status")),
            "note": record.get("RequestMessage"),
        }
        return render(data, lambda: _md_swap(data), response_format)

    @mcp.tool(
        name="deputy_set_unavailability",
        annotations={**_WRITE_ANNOTATIONS, "title": "Set unavailability"},
    )
    async def deputy_set_unavailability(
        start: Annotated[
            str,
            Field(description="Window start, ISO 8601, e.g. 2026-07-20T09:00:00 or with offset."),
        ],
        end: Annotated[
            str,
            Field(description="Window end, ISO 8601; must be after start."),
        ],
        reason: Annotated[
            str | None,
            Field(description="Optional comment stored with the record.", max_length=500),
        ] = None,
        repeat: Annotated[
            str | None,
            Field(
                description=(
                    "Optional iCal RRULE for a recurring block, e.g. "
                    "'FREQ=WEEKLY;INTERVAL=1;BYDAY=MO' or 'FREQ=MONTHLY;BYMONTHDAY=6'. "
                    "FREQ must be WEEKLY or MONTHLY. Omit for a one-off block."
                ),
            ),
        ] = None,
        response_format: ResponseFormat = "markdown",
    ) -> str:
        """Record an unavailability window for the signed-in user.

        Submits an approved unavailability (one-off, or recurring when ``repeat`` is
        given). Times are ISO 8601; a value without a timezone offset is treated as
        UTC. The end must be strictly after the start.

        When NOT to use: to request a single shift off (that is a leave request, not
        modelled here) -- this blocks availability for the whole window.

        Returns markdown (a confirmation of the recorded window) or, with
        response_format="json", the object ``{"unavailability_id", "recurring", "start",
        "end", "timezone", "reason"}``.

        Args:
            start: Window start (ISO 8601).
            end: Window end (ISO 8601), after ``start``.
            reason: Optional comment.
            repeat: Optional RRULE string; see the field description.
            response_format: ``markdown`` (default) or ``json``.
        """
        try:
            client = get_client()
            start_dt = _parse_iso(start, "start")
            end_dt = _parse_iso(end, "end")
            unavail = await client.set_unavailability(start_dt, end_dt, reason, repeat)
            tz, tz_label = await resolve_client_timezone(client)
        except DeputyError as exc:
            return _format_error(exc)
        record = unavail.model_dump(mode="json")
        record_type = record.get("Type")
        data = {
            "unavailability_id": record.get("Id"),
            "recurring": bool(record_type) if record_type is not None else bool(repeat),
            "start": fmt_ts(_to_unix(start_dt), tz),
            "end": fmt_ts(_to_unix(end_dt), tz),
            "timezone": tz_label,
            "reason": reason,
        }
        return render(data, lambda: _md_unavail(data), response_format)

    @mcp.tool(
        name="deputy_clock_in",
        annotations={**_WRITE_ANNOTATIONS, "title": "Clock in"},
    )
    async def deputy_clock_in(
        area_id: Annotated[
            int | None,
            Field(
                description=(
                    "OperationalUnit.Id (the area/location) to clock into. Omit only "
                    "if the install has a single rosterable area; otherwise required."
                ),
                gt=0,
            ),
        ] = None,
        response_format: ResponseFormat = "markdown",
    ) -> str:
        """Clock the signed-in user in, starting a live timesheet.

        Starts an unscheduled timesheet against the given area. If ``area_id`` is
        omitted it is auto-resolved **only** when exactly one rosterable area exists;
        otherwise the tool asks for one (clocking into the wrong location is a real
        hazard). No roster is required.

        When NOT to use: to record a past shift after the fact (use a full-timesheet
        edit in Deputy) -- this starts the clock *now*.

        Returns markdown (a confirmation with the timesheet id and start time; keep the
        id to clock out) or, with response_format="json", the object ``{"timesheet_id",
        "area_id", "in_progress", "start_time", "timezone"}``.

        Args:
            area_id: The area/location ``OperationalUnit.Id`` to clock into.
            response_format: ``markdown`` (default) or ``json``.
        """
        try:
            client = get_client()
            timesheet = await client.clock_in(area_id)
            tz, tz_label = await resolve_client_timezone(client)
        except DeputyError as exc:
            return _format_error(exc)
        record = timesheet.model_dump(mode="json")
        start_unix = record.get("StartTime")
        data = {
            "timesheet_id": record.get("Id"),
            "area_id": record.get("OperationalUnit"),
            "in_progress": record.get("IsInProgress"),
            "start_time": fmt_ts(start_unix, tz) if isinstance(start_unix, int) else None,
            "timezone": tz_label,
        }
        return render(data, lambda: _md_clock_in(data), response_format)

    @mcp.tool(
        name="deputy_clock_out",
        annotations={**_WRITE_ANNOTATIONS, "title": "Clock out"},
    )
    async def deputy_clock_out(
        mealbreak_minutes: Annotated[
            int | None,
            Field(description="Optional unpaid meal-break length, in minutes, to record.", ge=0),
        ] = None,
        response_format: ResponseFormat = "markdown",
    ) -> str:
        """Clock the signed-in user out, ending their live timesheet.

        Ends the user's single in-progress timesheet (looked up automatically). A
        clear error is returned if none is open. Optionally records a meal break.

        When NOT to use: when several timesheets are open at once -- this tool ends the
        one in-progress record and errors if the situation is ambiguous.

        Returns markdown (a confirmation with the ended timesheet id, end time and total
        hours) or, with response_format="json", the object ``{"timesheet_id",
        "in_progress", "end_time", "timezone", "total_hours", "mealbreak_minutes"}``.

        Args:
            mealbreak_minutes: Optional unpaid meal-break minutes.
            response_format: ``markdown`` (default) or ``json``.
        """
        try:
            client = get_client()
            timesheet = await client.clock_out(mealbreak_minutes=mealbreak_minutes)
            tz, tz_label = await resolve_client_timezone(client)
        except DeputyError as exc:
            return _format_error(exc)
        record = timesheet.model_dump(mode="json")
        end_unix = record.get("EndTime")
        data = {
            "timesheet_id": record.get("Id"),
            "in_progress": record.get("IsInProgress"),
            "end_time": fmt_ts(end_unix, tz) if isinstance(end_unix, int) else None,
            "timezone": tz_label,
            "total_hours": record.get("TotalTime"),
            "mealbreak_minutes": mealbreak_minutes,
        }
        return render(data, lambda: _md_clock_out(data), response_format)


# --------------------------------------------------------------------------- #
# Markdown renderers (each paired with its tool's summary ``data`` dict)
# --------------------------------------------------------------------------- #
def _md_claim(data: dict[str, Any]) -> str:
    """Confirmation for a claimed open shift."""
    return (
        f"**Open shift {data['shift_id']} claimed.**\n\n"
        "You are now assigned to this shift. Deputy returns no body on success, so "
        "re-read the roster if you need to confirm the change."
    )


def _md_swap(data: dict[str, Any]) -> str:
    """Confirmation for a submitted shift-swap request."""
    label = data.get("status_label") or "submitted"
    lines = [
        f"**Shift-swap request created (#{data.get('swap_id')}).**",
        "",
        f"- Shift offered: {data.get('source_shift_id')}",
        f"- Status: {label}",
    ]
    if data.get("note"):
        lines.append(f"- Note: {data['note']}")
    lines.append("")
    lines.append("A manager must approve or decline this request.")
    return "\n".join(lines)


def _md_unavail(data: dict[str, Any]) -> str:
    """Confirmation for a recorded unavailability window."""
    kind = "recurring" if data.get("recurring") else "one-off"
    tz = data.get("timezone")
    lines = [
        f"**Unavailability recorded (#{data.get('unavailability_id')}, {kind}).**",
        "",
        f"- From: {data.get('start')} ({tz})",
        f"- To: {data.get('end')} ({tz})",
    ]
    if data.get("reason"):
        lines.append(f"- Reason: {data['reason']}")
    return "\n".join(lines)


def _md_clock_in(data: dict[str, Any]) -> str:
    """Confirmation for a started timesheet."""
    lines = [
        f"**Clocked in.** Timesheet #{data.get('timesheet_id')} is now running.",
        "",
        f"- Area: {data.get('area_id')}",
    ]
    if data.get("start_time"):
        lines.append(f"- Started: {data['start_time']} ({data.get('timezone')})")
    lines.append("")
    lines.append("Keep the timesheet id above -- it is needed to clock out.")
    return "\n".join(lines)


def _md_clock_out(data: dict[str, Any]) -> str:
    """Confirmation for an ended timesheet."""
    lines = [f"**Clocked out.** Timesheet #{data.get('timesheet_id')} is closed.", ""]
    if data.get("end_time"):
        lines.append(f"- Ended: {data['end_time']} ({data.get('timezone')})")
    if data.get("total_hours") is not None:
        lines.append(f"- Total worked: {data['total_hours']} h")
    if data.get("mealbreak_minutes"):
        lines.append(f"- Meal break: {data['mealbreak_minutes']} min")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _format_error(exc: DeputyError) -> str:
    """Render a :class:`DeputyError` as a short, actionable string for the model."""
    lines = [f"Deputy write did not complete: {exc.message}"]
    if exc.hint:
        lines.append(f"Hint: {exc.hint}")
    return "\n".join(lines)


def _parse_iso(value: str, field: str) -> datetime:
    """Parse an ISO 8601 date-time argument, raising an actionable error on failure."""
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise DeputyError(
            f"Could not parse {field} as an ISO 8601 date-time: {value!r}.",
            hint="Use e.g. 2026-07-20T09:00:00 or 2026-07-20T09:00:00+01:00.",
        ) from exc


def _to_unix(moment: datetime) -> int:
    """Convert a datetime to unix seconds (a naive value is treated as UTC)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp())
