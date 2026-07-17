"""Tests for the dependency-free iCal roster parser (:mod:`deputy_mcp.client.ical`)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from deputy_mcp.client.ical import parse_ical

# A fictional Deputy-style feed: folded line, TZID, UTC, all-day, and a malformed event.
SAMPLE = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//Deputy//Roster//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:shift-1@deputy\r\n"
    "DTSTART;TZID=Europe/Dublin:20260722T090000\r\n"
    "DTEND;TZID=Europe/Dublin:20260722T180000\r\n"
    "SUMMARY:Management shift at Cloud Nine\r\n"
    " Cafe\r\n"  # folded continuation of SUMMARY
    "LOCATION:Cloud Nine Cafe Harbourside\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:shift-2@deputy\r\n"
    "DTSTART:20260723T090000Z\r\n"
    "DTEND:20260723T173000Z\r\n"
    "SUMMARY:Early\\, opening\r\n"  # escaped comma
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:allday@deputy\r\n"
    "DTSTART;VALUE=DATE:20260725\r\n"
    "SUMMARY:Leave\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:broken@deputy\r\n"  # no DTSTART -> skipped, must not break the rest
    "SUMMARY:Broken\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def test_parses_all_valid_events_and_skips_broken() -> None:
    events = parse_ical(SAMPLE)
    assert len(events) == 3  # broken (no DTSTART) dropped, others kept
    assert [e.uid for e in events] == ["shift-1@deputy", "shift-2@deputy", "allday@deputy"]


def test_line_unfolding_joins_summary() -> None:
    events = parse_ical(SAMPLE)
    assert events[0].title == "Management shift at Cloud NineCafe"


def test_tzid_is_resolved() -> None:
    events = parse_ical(SAMPLE)
    assert events[0].start == datetime(2026, 7, 22, 9, 0, tzinfo=ZoneInfo("Europe/Dublin"))
    assert events[0].end == datetime(2026, 7, 22, 18, 0, tzinfo=ZoneInfo("Europe/Dublin"))
    assert events[0].location == "Cloud Nine Cafe Harbourside"


def test_utc_z_suffix() -> None:
    events = parse_ical(SAMPLE)
    assert events[1].start == datetime(2026, 7, 23, 9, 0, tzinfo=UTC)


def test_escaped_text_is_unescaped() -> None:
    events = parse_ical(SAMPLE)
    assert events[1].title == "Early, opening"


def test_all_day_event() -> None:
    events = parse_ical(SAMPLE)
    allday = events[2]
    assert allday.all_day is True
    assert allday.day == date(2026, 7, 25)
    assert allday.end is not None  # synthesized +1 day


def test_events_sorted_by_start() -> None:
    events = parse_ical(SAMPLE)
    starts = [e.start for e in events]
    assert starts == sorted(starts)


def test_empty_feed_is_empty_list() -> None:
    assert parse_ical("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n") == []
