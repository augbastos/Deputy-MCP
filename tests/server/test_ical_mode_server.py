"""In-memory FastMCP tests for the server built in iCal mode.

With only ``DEPUTY_CALENDAR_URL`` set the server runs in iCal mode: it advertises ONLY the
four tools the personal calendar feed can serve — ``deputy_get_my_roster``,
``deputy_next_shift``, ``deputy_get_my_calendar_url`` and ``deputy_whoami`` — and the
API-only read tools plus every write tool are absent. The roster read is exercised over
the MCP protocol against a respx-mocked feed. All values are fictional.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
import respx
from fastmcp import Client

from deputy_mcp.server import create_server

from . import READ_TOOL_NAMES, WRITE_TOOL_NAMES, tool_text

# A fictional feed URL and a hand-written VCALENDAR of fictional shifts (self-contained so
# this module does not import across test packages).
FAKE_CAL_URL = "https://cloud-nine-cafe.eu.deputy.com/api/v1/my/ical/FAKE-NOT-A-SECRET.ics"
SAMPLE_FEED = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//Deputy//Roster//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:shift-1@deputy\r\n"
    "DTSTART:20260722T090000Z\r\n"
    "DTEND:20260722T173000Z\r\n"
    "SUMMARY:Front of House\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:shift-2@deputy\r\n"
    "DTSTART:20260724T083000Z\r\n"
    "DTEND:20260724T163000Z\r\n"
    "SUMMARY:Kitchen\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)

#: The exact tool surface iCal mode must advertise.
ICAL_TOOL_NAMES = frozenset(
    {"deputy_get_my_roster", "deputy_next_shift", "deputy_get_my_calendar_url", "deputy_whoami"}
)

#: Every DEPUTY_* variable to clear so the environment is deterministic per test.
_DEPUTY_VARS = (
    "DEPUTY_API_TOKEN",
    "DEPUTY_BASE_URL",
    "DEPUTY_CALENDAR_URL",
    "DEPUTY_ALLOW_WRITES",
    "DEPUTY_ALLOW_CUSTOM_HOST",
    "DEPUTY_CACHE_TTL",
    "DEPUTY_TIMEOUT",
    "DEPUTY_MAX_RETRIES",
    "DEPUTY_ENV_FILE",
)


@pytest.fixture
def ical_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Set an iCal-only environment (only DEPUTY_CALENDAR_URL), clearing everything else."""
    for name in _DEPUTY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DEPUTY_CALENDAR_URL", FAKE_CAL_URL)
    monkeypatch.setenv("DEPUTY_CACHE_TTL", "0")
    yield


def _cal() -> httpx.Response:
    return httpx.Response(200, text=SAMPLE_FEED, headers={"Content-Type": "text/calendar"})


# -- tool surface ------------------------------------------------------------


async def test_ical_mode_exposes_only_roster_tools(ical_env: None) -> None:
    server = create_server()
    async with Client(server) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == ICAL_TOOL_NAMES


async def test_ical_mode_hides_api_only_and_write_tools(ical_env: None) -> None:
    server = create_server()
    async with Client(server) as client:
        names = {tool.name for tool in await client.list_tools()}
    # No write tool leaks (read-only feed) ...
    assert names.isdisjoint(WRITE_TOOL_NAMES)
    # ... and the API-only read tools (everything in the api set that iCal cannot serve)
    # are absent too.
    api_only = READ_TOOL_NAMES - ICAL_TOOL_NAMES
    assert names.isdisjoint(api_only)
    assert "deputy_get_team_roster" not in names
    assert "deputy_get_my_timesheets" not in names


# -- roster read over the protocol -------------------------------------------


async def test_get_my_roster_renders_feed_shifts_markdown(ical_env: None) -> None:
    server = create_server()
    async with Client(server) as client:
        with respx.mock(assert_all_called=False) as router:
            router.get(FAKE_CAL_URL).mock(return_value=_cal())
            result = await client.call_tool(
                "deputy_get_my_roster",
                {"start_date": "2026-07-22", "end_date": "2026-07-24"},
            )
    text = tool_text(result)
    assert not result.is_error
    assert "My roster" in text
    # Both fictional shifts render (times fall back to UTC when the company lookup is
    # unavailable in iCal mode).
    assert "2026-07-22" in text
    assert "2026-07-24" in text


async def test_get_my_roster_renders_feed_shifts_json(ical_env: None) -> None:
    server = create_server()
    async with Client(server) as client:
        with respx.mock(assert_all_called=False) as router:
            router.get(FAKE_CAL_URL).mock(return_value=_cal())
            result = await client.call_tool(
                "deputy_get_my_roster",
                {
                    "start_date": "2026-07-22",
                    "end_date": "2026-07-24",
                    "response_format": "json",
                },
            )
    rosters = json.loads(tool_text(result))
    assert [r["Date"] for r in rosters] == ["2026-07-22", "2026-07-24"]
    assert rosters[0]["Comment"] == "Front of House"
    assert rosters[0]["OperationalUnitObject"]["OperationalUnitName"] == "Front of House"


async def test_get_my_roster_empty_window_is_graceful(ical_env: None) -> None:
    server = create_server()
    async with Client(server) as client:
        with respx.mock(assert_all_called=False) as router:
            router.get(FAKE_CAL_URL).mock(return_value=_cal())
            result = await client.call_tool(
                "deputy_get_my_roster",
                {"start_date": "2020-01-01", "end_date": "2020-01-02"},
            )
    text = tool_text(result)
    assert not result.is_error
    assert "No shifts found" in text
