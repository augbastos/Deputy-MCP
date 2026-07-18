"""In-memory FastMCP tests for iCal-mode tool behaviour (no API token).

Complements ``test_ical_mode_server.py`` (which owns the exact tool-surface assertions):
here we prove the *content* of the iCal-mode tools — that whoami/calendar report iCal mode
without leaking the secret feed URL, and that the roster/next tools render the real feed
shift with its area name (proving the embedded-``OperationalUnitObject`` renderer path so
output matches the API renderer). The feed is mocked with respx at the httpx layer.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from fastmcp import Client

from deputy_mcp.server import create_server

from . import tool_text

#: A fictional personal calendar feed URL (carries a feed token — treated as a secret).
FEED_URL = "https://cloud-nine-cafe.eu.deputy.com/ical/feedtoken123.ics"

#: A minimal Deputy-style feed with one far-future shift (deterministic for next_shift).
ICAL_BODY = "\r\n".join(
    [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Deputy//EN",
        "BEGIN:VEVENT",
        "UID:shift-1@deputy",
        "DTSTART:20350722T090000Z",
        "DTEND:20350722T170000Z",
        "SUMMARY:Front of House",
        "LOCATION:Cloud Nine Cafe",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ]
)

#: DEPUTY_* variables cleared before each test so only iCal config remains.
_VARS = (
    "DEPUTY_API_TOKEN",
    "DEPUTY_BASE_URL",
    "DEPUTY_ALLOW_WRITES",
    "DEPUTY_ALLOW_CUSTOM_HOST",
    "DEPUTY_CACHE_TTL",
    "DEPUTY_TIMEOUT",
    "DEPUTY_MAX_RETRIES",
    "DEPUTY_CALENDAR_URL",
    "DEPUTY_OAUTH_CLIENT_ID",
    "DEPUTY_OAUTH_CLIENT_SECRET",
    "DEPUTY_OAUTH_REDIRECT_PORT",
    "DEPUTY_TOKEN_STORE",
    "DEPUTY_ENV_FILE",
)


@pytest.fixture
def ical_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Set a clean iCal-only environment (calendar URL, no API token)."""
    for name in _VARS:
        monkeypatch.delenv(name, raising=False)
    # Point DEPUTY_ENV_FILE at an empty file so a developer's on-disk .env (which may
    # carry OAuth client creds) can never leak in and flip the mode to OAuth.
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DEPUTY_ENV_FILE", str(empty_env))
    monkeypatch.setenv("DEPUTY_CALENDAR_URL", FEED_URL)
    yield


@pytest.fixture
def feed() -> Iterator[respx.MockRouter]:
    """Mock the personal calendar feed at the httpx layer with the sample body."""
    with respx.mock(assert_all_called=False) as router:
        router.get(FEED_URL).mock(return_value=httpx.Response(200, text=ICAL_BODY))
        yield router


async def test_whoami_reports_ical_mode(ical_env: None) -> None:
    """whoami states iCal mode and roster-only scope, with no API-only identity detail."""
    server = create_server()
    async with Client(server) as client:
        md = tool_text(await client.call_tool("deputy_whoami", {}))
        js = tool_text(await client.call_tool("deputy_whoami", {"response_format": "json"}))
    assert "iCal mode" in md
    assert "deputy_get_my_roster" in md
    parsed = json.loads(js)
    assert parsed["mode"] == "ical"
    assert parsed["roster_only"] is True
    assert FEED_URL not in md and FEED_URL not in js


async def test_calendar_url_tool_never_leaks_the_secret_feed(ical_env: None) -> None:
    """In iCal mode the feed URL is the configured secret; the tool must not print it."""
    server = create_server()
    async with Client(server) as client:
        md = tool_text(await client.call_tool("deputy_get_my_calendar_url", {}))
        js = tool_text(
            await client.call_tool("deputy_get_my_calendar_url", {"response_format": "json"})
        )
    assert FEED_URL not in md and FEED_URL not in js
    assert "iCal mode" in md
    parsed = json.loads(js)
    assert parsed["configured"] is True
    assert parsed["calendar_url"] is None


async def test_my_roster_renders_feed_shift(ical_env: None, feed: respx.MockRouter) -> None:
    """get_my_roster reads the feed and renders the shift with its area name (no leak)."""
    server = create_server()
    async with Client(server) as client:
        md = tool_text(
            await client.call_tool(
                "deputy_get_my_roster",
                {"start_date": "2035-01-01", "end_date": "2035-12-31"},
            )
        )
        js = tool_text(
            await client.call_tool(
                "deputy_get_my_roster",
                {"start_date": "2035-01-01", "end_date": "2035-12-31", "response_format": "json"},
            )
        )
    assert "Front of House" in md
    assert FEED_URL not in md and FEED_URL not in js
    records = json.loads(js)
    assert isinstance(records, list) and len(records) == 1


async def test_next_shift_returns_feed_shift(ical_env: None, feed: respx.MockRouter) -> None:
    """next_shift returns the caller's own upcoming feed shift in iCal mode."""
    server = create_server()
    async with Client(server) as client:
        md = tool_text(await client.call_tool("deputy_next_shift", {}))
    assert "Next shift" in md
    assert "Front of House" in md
