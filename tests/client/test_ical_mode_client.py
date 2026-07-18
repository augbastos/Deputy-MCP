"""Client-level tests for :class:`deputy_mcp.client.DeputyClient` in iCal mode.

In iCal mode the client has NO API token and NO base URL: it reads the caller's own
roster from their personal Deputy calendar feed (mocked here with respx against a
fictional URL and a hand-written VCALENDAR). Only ``get_my_roster`` and ``next_shift``
(for self) work; every API-only method fails closed with the single actionable
"needs a Deputy API token" error. All values are fictional.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime

import httpx
import pytest
import respx

from deputy_mcp.client import DeputyClient, DeputyError
from deputy_mcp.config import DeputyConfig

from .test_ical_source import FAKE_CAL_URL, FUTURE_FEED, SAMPLE_FEED


def _ical_config() -> DeputyConfig:
    """A cache-disabled iCal-mode config for the fictional feed.

    Passes an explicit env mapping (hermetic — no cwd ``.env``), and forwards the
    isolated ``DEPUTY_TOKEN_STORE`` the autouse fixture pins so a real ``~/.deputy-mcp``
    token store can never flip this config into OAuth mode.
    """
    env = {"DEPUTY_CALENDAR_URL": FAKE_CAL_URL, "DEPUTY_CACHE_TTL": "0"}
    store = os.environ.get("DEPUTY_TOKEN_STORE")
    if store:
        env["DEPUTY_TOKEN_STORE"] = store
    return DeputyConfig.from_env(env)


def _cal(text: str) -> httpx.Response:
    return httpx.Response(200, text=text, headers={"Content-Type": "text/calendar"})


def test_client_resolves_ical_mode() -> None:
    client = DeputyClient(_ical_config())
    assert client.mode == "ical"


# -- roster reads work over the feed -----------------------------------------


async def test_get_my_roster_maps_feed_events_to_roster() -> None:
    client = DeputyClient(_ical_config())
    try:
        with respx.mock(assert_all_called=False) as router:
            router.get(FAKE_CAL_URL).mock(return_value=_cal(SAMPLE_FEED))
            rosters = await client.get_my_roster(date(2026, 7, 22), date(2026, 7, 24))
    finally:
        await client.aclose()
    assert [r.Date for r in rosters] == ["2026-07-22", "2026-07-24"]
    first = rosters[0]
    assert first.Comment == "Front of House"
    assert first.Open is False
    assert first.TotalTime == 9.0  # 09:00-18:00 Dublin
    # The area label survives as the embedded OperationalUnitObject the renderer reads.
    extra = first.model_extra or {}
    assert extra.get("OperationalUnitObject", {}).get("OperationalUnitName") == "Front of House"


async def test_next_shift_self_returns_soonest_future() -> None:
    client = DeputyClient(_ical_config())
    try:
        with respx.mock(assert_all_called=False) as router:
            router.get(FAKE_CAL_URL).mock(return_value=_cal(FUTURE_FEED))
            roster = await client.next_shift()
    finally:
        await client.aclose()
    assert roster is not None
    assert roster.Date == "2099-01-01"


async def test_next_shift_for_other_employee_needs_token() -> None:
    """The feed is the caller's own roster; asking for someone else fails closed."""
    client = DeputyClient(_ical_config())
    try:
        with pytest.raises(DeputyError) as exc:
            await client.next_shift(employee_id=999)
    finally:
        await client.aclose()
    assert "DEPUTY_API_TOKEN" in str(exc.value)


# -- every API-only method fails closed with the token error -----------------

_API_ONLY_CALLS: dict[str, Callable[[DeputyClient], Awaitable[object]]] = {
    "whoami": lambda c: c.whoami(),
    "get_company": lambda c: c.get_company(),
    "get_operational_units": lambda c: c.get_operational_units(),
    "get_employees": lambda c: c.get_employees(search="Alex"),
    "get_employee": lambda c: c.get_employee(101),
    "get_team_roster": lambda c: c.get_team_roster(date(2026, 7, 1), date(2026, 7, 8), None),
    "who_is_working": lambda c: c.who_is_working(datetime(2026, 7, 1, tzinfo=UTC)),
    "search_shifts": lambda c: c.search_shifts(),
    "get_my_timesheets": lambda c: c.get_my_timesheets(date(2026, 7, 1), date(2026, 7, 8)),
}


@pytest.mark.parametrize("name", sorted(_API_ONLY_CALLS))
async def test_api_only_method_raises_token_error(name: str) -> None:
    call = _API_ONLY_CALLS[name]
    client = DeputyClient(_ical_config())
    try:
        with pytest.raises(DeputyError) as exc:
            await call(client)
    finally:
        await client.aclose()
    message = str(exc.value)
    assert "DEPUTY_API_TOKEN" in message
    # The guidance points the employee at what DOES work in iCal mode.
    assert "get_my_roster" in message


async def test_api_only_error_never_hits_network() -> None:
    """API-only methods must fail before any HTTP call (no transport exists)."""
    client = DeputyClient(_ical_config())
    try:
        with respx.mock(assert_all_called=False) as router:
            route = router.get(FAKE_CAL_URL).mock(return_value=_cal(SAMPLE_FEED))
            with pytest.raises(DeputyError):
                await client.whoami()
        assert route.call_count == 0  # the feed was never fetched for an API-only call
    finally:
        await client.aclose()


# -- lifecycle ---------------------------------------------------------------


async def test_aclose_closes_feed_source() -> None:
    client = DeputyClient(_ical_config())
    await client.aclose()
    # aclose is safe to call again (idempotent shutdown).
    await client.aclose()


async def test_async_context_manager_closes() -> None:
    async with DeputyClient(_ical_config()) as client:
        with respx.mock(assert_all_called=False) as router:
            router.get(FAKE_CAL_URL).mock(return_value=_cal(SAMPLE_FEED))
            rosters = await client.get_my_roster(date(2026, 7, 22), date(2026, 7, 22))
        assert len(rosters) == 1
