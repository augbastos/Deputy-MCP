"""Tests for :class:`deputy_mcp.client.ical.IcalRosterSource`.

The feed is mocked at the HTTP layer with respx against a FICTIONAL calendar URL and a
hand-written VCALENDAR of fictional shifts (Alex Rivera). These tests prove the source:

* fetches + window-filters the feed by local calendar day;
* returns the soonest future event from ``next()``;
* raises the actionable :class:`DeputyFeedError` on a 5xx / timeout, WITHOUT ever
  putting the tokenised feed URL into the exception;
* honours the TTL cache (hit within TTL, refetch after expiry, disabled at ttl=0).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from deputy_mcp.client import ical as ical_module
from deputy_mcp.client.errors import DeputyFeedError
from deputy_mcp.client.ical import IcalRosterSource

# A fictional feed URL. NOT a real credential; the token segment is a marker string that
# must never leak into an exception.
FAKE_CAL_URL = "https://cloud-nine-cafe.eu.deputy.com/api/v1/my/ical/FAKE-NOT-A-SECRET.ics"
_TOKEN_MARKER = "FAKE-NOT-A-SECRET"

# Three fictional shifts across a week (Dublin TZID, UTC-Z, and a later UTC shift).
SAMPLE_FEED = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//Deputy//Roster//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:shift-1@deputy\r\n"
    "DTSTART;TZID=Europe/Dublin:20260722T090000\r\n"
    "DTEND;TZID=Europe/Dublin:20260722T180000\r\n"
    "SUMMARY:Front of House\r\n"
    "LOCATION:Cloud Nine Cafe\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:shift-2@deputy\r\n"
    "DTSTART:20260724T083000Z\r\n"
    "DTEND:20260724T163000Z\r\n"
    "SUMMARY:Kitchen\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:shift-3@deputy\r\n"
    "DTSTART:20260728T120000Z\r\n"
    "DTEND:20260728T200000Z\r\n"
    "SUMMARY:Management\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)

# A feed whose only shift is far in the future, so ``next()`` with the default "now" is
# deterministic regardless of the wall clock when the suite runs.
FUTURE_FEED = (
    "BEGIN:VCALENDAR\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:future@deputy\r\n"
    "DTSTART:20990101T090000Z\r\n"
    "DTEND:20990101T170000Z\r\n"
    "SUMMARY:Far future shift\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def _cal(text: str) -> httpx.Response:
    """A 200 text/calendar response carrying ``text``."""
    return httpx.Response(200, text=text, headers={"Content-Type": "text/calendar"})


@pytest.fixture
async def source() -> AsyncIterator[IcalRosterSource]:
    """A cache-disabled source (ttl=0) so each test fetches deterministically."""
    src = IcalRosterSource(FAKE_CAL_URL, timeout=5.0, max_retries=0, cache_ttl=0)
    try:
        yield src
    finally:
        await src.aclose()


# -- window filtering --------------------------------------------------------


async def test_get_roster_filters_to_window(source: IcalRosterSource) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(FAKE_CAL_URL).mock(return_value=_cal(SAMPLE_FEED))
        events = await source.get_roster(date(2026, 7, 22), date(2026, 7, 24))
    assert [e.uid for e in events] == ["shift-1@deputy", "shift-2@deputy"]


async def test_get_roster_excludes_out_of_window(source: IcalRosterSource) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(FAKE_CAL_URL).mock(return_value=_cal(SAMPLE_FEED))
        events = await source.get_roster(date(2026, 7, 25), date(2026, 7, 30))
    assert [e.uid for e in events] == ["shift-3@deputy"]


async def test_get_roster_empty_window_is_empty(source: IcalRosterSource) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(FAKE_CAL_URL).mock(return_value=_cal(SAMPLE_FEED))
        events = await source.get_roster(date(2026, 1, 1), date(2026, 1, 2))
    assert events == []


# -- next() ------------------------------------------------------------------


async def test_next_returns_soonest_future_event(source: IcalRosterSource) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(FAKE_CAL_URL).mock(return_value=_cal(SAMPLE_FEED))
        event = await source.next(after=datetime(2026, 7, 1, tzinfo=UTC))
    assert event is not None
    assert event.uid == "shift-1@deputy"


async def test_next_skips_past_events(source: IcalRosterSource) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(FAKE_CAL_URL).mock(return_value=_cal(SAMPLE_FEED))
        event = await source.next(after=datetime(2026, 7, 23, tzinfo=UTC))
    assert event is not None
    assert event.uid == "shift-2@deputy"


async def test_next_none_when_nothing_ahead(source: IcalRosterSource) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(FAKE_CAL_URL).mock(return_value=_cal(SAMPLE_FEED))
        event = await source.next(after=datetime(2026, 8, 1, tzinfo=UTC))
    assert event is None


async def test_next_naive_after_is_treated_as_utc(source: IcalRosterSource) -> None:
    """A naive ``after`` must not crash the aware/naive comparison; it is read as UTC."""
    with respx.mock(assert_all_called=False) as router:
        router.get(FAKE_CAL_URL).mock(return_value=_cal(SAMPLE_FEED))
        event = await source.next(after=datetime(2026, 7, 23, 0, 0))  # naive
    assert event is not None
    assert event.uid == "shift-2@deputy"


async def test_next_default_uses_now(source: IcalRosterSource) -> None:
    """With no ``after`` the current time is used; a year-2099 shift is always ahead."""
    with respx.mock(assert_all_called=False) as router:
        router.get(FAKE_CAL_URL).mock(return_value=_cal(FUTURE_FEED))
        event = await source.next()
    assert event is not None
    assert event.uid == "future@deputy"


# -- error handling: actionable, secret-free --------------------------------


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_5xx_raises_feed_error(source: IcalRosterSource, status: int) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(FAKE_CAL_URL).mock(return_value=httpx.Response(status))
        with pytest.raises(DeputyFeedError) as exc:
            await source.get_roster(date(2026, 7, 1), date(2026, 8, 1))
    assert exc.value.status_code == status


async def test_timeout_raises_feed_error(source: IcalRosterSource) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(FAKE_CAL_URL).mock(side_effect=httpx.ReadTimeout("slow feed"))
        with pytest.raises(DeputyFeedError):
            await source.next(after=datetime(2026, 7, 1, tzinfo=UTC))


async def test_feed_url_never_appears_in_exception() -> None:
    """The tokenised feed URL must never surface in the error message, hint or repr."""
    src = IcalRosterSource(FAKE_CAL_URL, timeout=5.0, max_retries=0, cache_ttl=0)
    try:
        with respx.mock(assert_all_called=False) as router:
            router.get(FAKE_CAL_URL).mock(
                return_value=httpx.Response(500, text=f"boom {FAKE_CAL_URL}")
            )
            with pytest.raises(DeputyFeedError) as exc:
                await src.get_roster(date(2026, 7, 1), date(2026, 8, 1))
        rendered = f"{exc.value!r} {exc.value} {exc.value.hint or ''}"
        assert FAKE_CAL_URL not in rendered
        assert _TOKEN_MARKER not in rendered
        # The source's own repr is redacted too.
        assert _TOKEN_MARKER not in repr(src)
    finally:
        await src.aclose()


async def test_transport_error_message_omits_url() -> None:
    """A transport failure carries the request URL in str(exc); we must not echo it."""
    src = IcalRosterSource(FAKE_CAL_URL, timeout=5.0, max_retries=0, cache_ttl=0)
    try:
        with respx.mock(assert_all_called=False) as router:
            router.get(FAKE_CAL_URL).mock(side_effect=httpx.ConnectError("dns boom"))
            with pytest.raises(DeputyFeedError) as exc:
                await src.next(after=datetime(2026, 7, 1, tzinfo=UTC))
        rendered = f"{exc.value!r} {exc.value} {exc.value.hint or ''}"
        assert _TOKEN_MARKER not in rendered
    finally:
        await src.aclose()


# -- retry -------------------------------------------------------------------


async def test_retryable_status_is_retried_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 503 then 200 is retried (GET is idempotent) with sleep patched out."""
    src = IcalRosterSource(FAKE_CAL_URL, timeout=5.0, max_retries=3, cache_ttl=0)
    monkeypatch.setattr(src, "_sleep", AsyncMock())
    try:
        with respx.mock(assert_all_called=False) as router:
            route = router.get(FAKE_CAL_URL).mock(
                side_effect=[httpx.Response(503), _cal(SAMPLE_FEED)]
            )
            events = await src.get_roster(date(2026, 7, 22), date(2026, 7, 24))
        assert route.call_count == 2
        assert [e.uid for e in events] == ["shift-1@deputy", "shift-2@deputy"]
    finally:
        await src.aclose()


# -- TTL cache ---------------------------------------------------------------


async def test_cache_serves_second_read_without_refetch() -> None:
    src = IcalRosterSource(FAKE_CAL_URL, timeout=5.0, max_retries=0, cache_ttl=30)
    try:
        with respx.mock(assert_all_called=False) as router:
            route = router.get(FAKE_CAL_URL).mock(return_value=_cal(SAMPLE_FEED))
            first = await src.get_roster(date(2026, 7, 1), date(2026, 8, 1))
            second = await src.get_roster(date(2026, 7, 1), date(2026, 8, 1))
        assert first == second
        assert route.call_count == 1  # second read served from cache
    finally:
        await src.aclose()


async def test_cache_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"t": 0.0}
    monkeypatch.setattr(ical_module, "time", SimpleNamespace(monotonic=lambda: clock["t"]))
    src = IcalRosterSource(FAKE_CAL_URL, timeout=5.0, max_retries=0, cache_ttl=30)
    try:
        with respx.mock(assert_all_called=False) as router:
            route = router.get(FAKE_CAL_URL).mock(return_value=_cal(SAMPLE_FEED))
            await src.get_roster(date(2026, 7, 1), date(2026, 8, 1))
            clock["t"] = 10.0  # within TTL -> cache hit
            await src.get_roster(date(2026, 7, 1), date(2026, 8, 1))
            assert route.call_count == 1
            clock["t"] = 40.0  # past TTL -> refetch
            await src.get_roster(date(2026, 7, 1), date(2026, 8, 1))
            assert route.call_count == 2
    finally:
        await src.aclose()


async def test_cache_disabled_when_ttl_zero() -> None:
    src = IcalRosterSource(FAKE_CAL_URL, timeout=5.0, max_retries=0, cache_ttl=0)
    try:
        with respx.mock(assert_all_called=False) as router:
            route = router.get(FAKE_CAL_URL).mock(return_value=_cal(SAMPLE_FEED))
            await src.get_roster(date(2026, 7, 1), date(2026, 8, 1))
            await src.get_roster(date(2026, 7, 1), date(2026, 8, 1))
        assert route.call_count == 2  # no caching -> every read refetches
    finally:
        await src.aclose()
