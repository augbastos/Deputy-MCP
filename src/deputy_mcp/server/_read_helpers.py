"""Argument-parsing and error-formatting helpers for the read tools.

Kept separate from :mod:`deputy_mcp.server.tools_read` (which holds the tool
registration itself) so both files stay well under the module size budget. Everything
here is pure: date/datetime coercion for tool arguments, and turning a
:class:`~deputy_mcp.client.DeputyError` into a short, actionable, traceback-free string.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from deputy_mcp.client import DeputyError, DeputyPermissionError

__all__ = [
    "format_error",
    "opt_date",
    "parse_date",
    "parse_dt",
    "today",
]

#: Named in every manager/admin permission error so the model (or user) is redirected to
#: tools that DO work at a standard employee access level instead of seeing a raw 403.
_SELF_SERVICE_HINT = (
    "This tool needs a manager or administrator access level on the Deputy install. "
    "With a standard employee token, use the self-service tools instead: "
    "deputy_get_my_roster, deputy_next_shift, deputy_get_my_timesheets "
    "(and deputy_whoami / deputy_get_my_calendar_url)."
)


def format_error(exc: DeputyError) -> str:
    """Format a client error as an actionable, traceback-free message.

    A permission failure (HTTP 403) from a manager/admin-only tool is rewritten to point
    the caller at the self-service tools that work at an employee access level, so the
    model never sees a raw 403 and always has a working next step. Every other error keeps
    the client's own message/hint.
    """
    if isinstance(exc, DeputyPermissionError):
        return f"Error: {exc.message}\nHint: {_SELF_SERVICE_HINT}"
    text = f"Error: {exc.message}"
    if exc.hint:
        text += f"\nHint: {exc.hint}"
    return text


def today() -> date:
    """Return today's date in UTC (used as a range default)."""
    return datetime.now(UTC).date()


def parse_date(value: str | None, default: date) -> date:
    """Parse an ISO ``YYYY-MM-DD`` string, or return ``default`` when blank."""
    if value is None or not value.strip():
        return default
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise DeputyError(
            f"Invalid date '{value}'.", hint="Use ISO format YYYY-MM-DD, e.g. 2026-07-18."
        ) from exc


def opt_date(value: str | None) -> date | None:
    """Parse an optional ISO date, returning ``None`` when blank."""
    if value is None or not value.strip():
        return None
    return parse_date(value, today())


def parse_dt(value: str | None) -> datetime | None:
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
