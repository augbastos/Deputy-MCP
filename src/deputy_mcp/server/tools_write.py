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

Tool annotations (MCP hints): ``readOnlyHint=false`` (they change state),
``destructiveHint=false`` (they create/assign, never delete), ``idempotentHint=false``
(calling twice is not a no-op -- e.g. two clock-ins), and ``openWorldHint=true`` (they
reach an external system, the Deputy install).

Only ``deputy_claim_open_shift`` asks the user to confirm through MCP elicitation
(:mod:`deputy_mcp.server._confirm`), because it is the one action that skips a Deputy
approval step. Clock in/out and unavailability act on the user's own record and are
routine, and a swap request only *submits* something a manager still approves, so an
extra prompt there would add friction without adding protection.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any

from fastmcp import Context
from mcp.types import InputRequiredResult
from pydantic import Field

from deputy_mcp.client.errors import DeputyError
from deputy_mcp.sanitize import redact
from deputy_mcp.server._confirm import confirm
from deputy_mcp.server.formatting import ResponseFormat, fmt_ts, render
from deputy_mcp.server.tools_read import _FORMAT_FIELD, resolve_client_timezone

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    from deputy_mcp.client import DeputyClient

__all__ = ["register"]

#: Zero-argument provider handed to :func:`register`; returns the shared, already-open
#: :class:`~deputy_mcp.client.DeputyClient` that ``app.py`` builds once in its lifespan.
ClientProvider = Callable[[], "DeputyClient"]

#: RosterSwap.Status integer -> human label (Deputy's documented RosterSwap status codes).
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
def _write_annotations(title: str) -> ToolAnnotations:
    """MCP behaviour hints shared by every write tool (see the module docstring)."""
    from mcp.types import ToolAnnotations

    return ToolAnnotations(
        title=title,
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    )


def register(mcp: FastMCP[Any], get_client: ClientProvider) -> None:
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
        annotations=_write_annotations("Claim open shift"),
    )
    async def deputy_claim_open_shift(
        shift_id: Annotated[
            int,
            Field(description="Roster.Id of the open (unassigned) shift to take.", gt=0),
        ],
        ctx: Context,
        response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
    ) -> str | InputRequiredResult:
        """Assign the signed-in user to an open shift, after the user confirms it.

        Deputy has **no** employee "accept open shift" API, so this fills the open
        roster by updating it (sets the employee, clears the open flag). It therefore
        needs a token whose Deputy user may edit that shift and it **bypasses** any
        "open shift with approval" request flow -- the assignment is applied directly.
        Because of that, the tool first shows the shift to the user and asks them to
        approve it through the MCP client (elicitation); nothing changes unless they
        accept, and clients without elicitation support cannot claim shifts.

        When NOT to use: to *offer* one of your own shifts to others (use
        deputy_request_shift_swap), or when a manager must approve pick-ups (that
        pathway is UI-only and not exposed here).

        Returns markdown (a confirmation, or why nothing was claimed; Deputy returns an
        empty body on success, so re-read the roster to verify it stuck) or, with
        response_format="json", the object ``{"shift_id", "claimed", "reason"}``.
        """
        try:
            client = get_client()
            shift = await client.get_open_shift(shift_id)
            tz, tz_label = await resolve_client_timezone(client)
            decision = await confirm(
                ctx,
                _claim_prompt(
                    shift_id, fmt_ts(shift.StartTime, tz), fmt_ts(shift.EndTime, tz), tz_label
                ),
                action_id=f"claim-open-shift:{shift_id}",
            )
            if not isinstance(decision, str):
                return decision  # 2026-07-28: the client asks the user and calls again
            if decision != "approved":
                data: dict[str, Any] = {
                    "shift_id": shift_id,
                    "claimed": False,
                    "reason": decision,
                }
                return render(data, lambda: _md_not_claimed(data), response_format)
            await client.claim_open_shift(shift_id)
        except DeputyError as exc:
            return _format_error(exc)
        data = {"shift_id": shift_id, "claimed": True, "reason": None}
        return render(data, lambda: _md_claim(data), response_format)

    @mcp.tool(
        name="deputy_request_shift_swap",
        annotations=_write_annotations("Request shift swap"),
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
        response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
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
        annotations=_write_annotations("Set unavailability"),
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
        response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
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
        annotations=_write_annotations("Clock in"),
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
        response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
    ) -> str:
        """Clock the signed-in user in, starting a live timesheet.

        Starts an unscheduled timesheet against the given area, now. If ``area_id`` is
        omitted it is auto-resolved **only** when exactly one rosterable area exists;
        otherwise the tool returns an error asking for one (clocking into the wrong
        location is a real hazard). No roster is required. Calling it twice starts two
        timesheets, so check deputy_whoami ("Clocked in now") first when unsure.

        When NOT to use: to record a past shift after the fact (use a full-timesheet
        edit in Deputy), or to end a shift (use deputy_clock_out).

        Returns markdown (a confirmation with the timesheet id and start time) or, with
        response_format="json", the object ``{"timesheet_id", "area_id", "in_progress",
        "start_time", "timezone"}``.

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
        annotations=_write_annotations("Clock out"),
    )
    async def deputy_clock_out(
        mealbreak_minutes: Annotated[
            int | None,
            Field(description="Optional unpaid meal-break length, in minutes, to record.", ge=0),
        ] = None,
        response_format: Annotated[ResponseFormat, _FORMAT_FIELD] = "markdown",
    ) -> str:
        """Clock the signed-in user out, ending their live timesheet.

        Ends the user's own in-progress timesheet, which Deputy reports on /me, so no
        timesheet id is needed. A clear error is returned if the user is not clocked in.
        Optionally records an unpaid meal break.

        When NOT to use: to end someone else's timesheet or to edit a past one -- this
        only closes your currently running timesheet.

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


def _claim_prompt(shift_id: int, start: str, end: str, tz_label: str) -> str:
    """The question the user approves before an open shift is assigned to them."""
    return (
        f"Claim open shift #{shift_id} ({start} to {end}, {tz_label})? Deputy will assign "
        "it to you directly, skipping any manager approval step for open shifts."
    )


def _md_not_claimed(data: dict[str, Any]) -> str:
    """Explanation when the user did not approve, or could not be asked."""
    if data.get("reason") == "unsupported":
        return (
            f"**Open shift {data['shift_id']} was not claimed.**\n\n"
            "Claiming needs your confirmation, but this MCP client does not support "
            "elicitation, so it cannot ask you. Claim the shift in Deputy instead."
        )
    return f"**Open shift {data['shift_id']} was not claimed** -- the request was declined."


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
    return redact("\n".join(lines))


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
