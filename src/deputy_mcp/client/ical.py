"""Read a Deputy employee roster from their personal iCal feed (``CalendarURL``).

Deputy exposes a per-user iCal subscription URL (the ``CalendarURL`` field of
``GET /api/v1/me``). It is a tokenised, read-only, roster-scoped feed meant to be
subscribed to from a calendar app. Crucially it needs **no API token** — which makes
it the one way an ordinary employee, who cannot mint a Deputy API token, can let the
MCP server read their real roster over the protocol.

This module is intentionally dependency-free: it unfolds and parses just the RFC 5545
subset Deputy emits (``VEVENT`` with ``DTSTART``/``DTEND``/``SUMMARY``/``LOCATION``/
``UID``). It never handles the feed URL as anything but an opaque secret held by the
caller; the URL is never logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo

__all__ = ["RosterEvent", "parse_ical"]


@dataclass(frozen=True, slots=True)
class RosterEvent:
    """A single shift parsed from the iCal feed.

    Attributes:
        start: Shift start (timezone-aware; assumed UTC if the feed omits a zone).
        end: Shift end, or ``None`` when the event carries no ``DTEND``.
        title: The event ``SUMMARY`` (Deputy puts the area/role here), or ``""``.
        location: The event ``LOCATION`` if present.
        uid: The event ``UID`` (stable id), or ``""``.
        all_day: True when the event was a ``VALUE=DATE`` (date-only) event.
    """

    start: datetime
    end: datetime | None
    title: str
    location: str | None
    uid: str
    all_day: bool

    @property
    def day(self) -> date:
        """The local calendar day the shift falls on."""
        return self.start.date()


def _unfold(text: str) -> list[str]:
    """Undo RFC 5545 line folding: a line starting with space/tab continues the prior one."""
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[str] = []
    for line in raw:
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _split_prop(line: str) -> tuple[str, dict[str, str], str]:
    """Split a content line into (NAME, params, value).

    ``DTSTART;TZID=Europe/Dublin:20260722T090000`` -> ("DTSTART",
    {"TZID": "Europe/Dublin"}, "20260722T090000"). The value is everything after the
    first unescaped colon; params are ``;KEY=VALUE`` pairs before it.
    """
    colon = line.find(":")
    if colon == -1:
        return line.upper(), {}, ""
    name_and_params = line[:colon]
    value = line[colon + 1 :]
    parts = name_and_params.split(";")
    name = parts[0].upper()
    params: dict[str, str] = {}
    for part in parts[1:]:
        if "=" in part:
            key, _, val = part.partition("=")
            params[key.upper()] = val
    return name, params, value


def _unescape(value: str) -> str:
    """Unescape RFC 5545 TEXT values (\\n, \\,, \\;, \\\\)."""
    out: list[str] = []
    i = 0
    while i < len(value):
        char = value[i]
        if char == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in ("n", "N") else nxt)
            i += 2
        else:
            out.append(char)
            i += 1
    return "".join(out)


def _parse_dt(value: str, params: dict[str, str]) -> tuple[datetime, bool]:
    """Parse a DTSTART/DTEND value into (aware datetime, all_day).

    Handles ``VALUE=DATE`` (date-only, all-day), UTC (``...Z``), and floating/TZID
    local times. Without a usable zone the time is assumed to be UTC so the value stays
    comparable; the feed's own localized fields are not available here.
    """
    if params.get("VALUE") == "DATE" or (len(value) == 8 and value.isdigit()):
        day = datetime.strptime(value[:8], "%Y%m%d").replace(tzinfo=UTC)
        return day, True
    if value.endswith("Z"):
        naive = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
        return naive.replace(tzinfo=UTC), False
    naive = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    tz = _resolve_tz(params.get("TZID"))
    return naive.replace(tzinfo=tz), False


def _resolve_tz(tzid: str | None) -> tzinfo:
    """Resolve a TZID to a tzinfo, falling back to UTC when unknown/unavailable."""
    if not tzid:
        return UTC
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(tzid)
    except Exception:
        return UTC


def parse_ical(text: str) -> list[RosterEvent]:
    """Parse a Deputy iCal feed body into a sorted list of :class:`RosterEvent`.

    Only ``VEVENT`` blocks are read; malformed events are skipped rather than raising,
    so one bad entry never hides the rest of the roster. Events are returned sorted by
    start time.
    """
    events: list[RosterEvent] = []
    in_event = False
    cur: dict[str, tuple[str, dict[str, str]]] = {}
    for line in _unfold(text):
        upper = line.strip().upper()
        if upper == "BEGIN:VEVENT":
            in_event, cur = True, {}
            continue
        if upper == "END:VEVENT":
            event = _build_event(cur)
            if event is not None:
                events.append(event)
            in_event = False
            continue
        if not in_event:
            continue
        name, params, value = _split_prop(line)
        cur[name] = (value, params)
    events.sort(key=lambda item: item.start)
    return events


def _build_event(props: dict[str, tuple[str, dict[str, str]]]) -> RosterEvent | None:
    """Build a RosterEvent from collected VEVENT properties, or None if unusable."""
    dtstart = props.get("DTSTART")
    if dtstart is None:
        return None
    try:
        start, all_day = _parse_dt(dtstart[0], dtstart[1])
    except (ValueError, KeyError):
        return None
    end: datetime | None = None
    dtend = props.get("DTEND")
    if dtend is not None:
        try:
            end, _ = _parse_dt(dtend[0], dtend[1])
        except (ValueError, KeyError):
            end = None
    if all_day and end is None:
        end = start + timedelta(days=1)
    title = _unescape(props["SUMMARY"][0]) if "SUMMARY" in props else ""
    location = _unescape(props["LOCATION"][0]) if "LOCATION" in props else None
    uid = props["UID"][0] if "UID" in props else ""
    return RosterEvent(
        start=start, end=end, title=title, location=location or None, uid=uid, all_day=all_day
    )
