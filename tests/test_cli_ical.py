"""CLI tests for iCal mode (no API token).

Drive the real :func:`deputy_mcp.cli.main` with only ``DEPUTY_CALENDAR_URL`` set, so the
client resolves to iCal mode, and mock the personal calendar feed with respx. These prove
that ``roster`` and ``next`` work from the feed while API-only subcommands fail closed with
a single actionable stderr line and exit code 1 (never a traceback), and that the secret
feed URL is never printed.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx

from deputy_mcp import cli

FEED_URL = "https://cloud-nine-cafe.eu.deputy.com/ical/feedtoken123.ics"

ICAL_BODY = "\r\n".join(
    [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "BEGIN:VEVENT",
        "UID:shift-1@deputy",
        "DTSTART:20350722T090000Z",
        "DTEND:20350722T170000Z",
        "SUMMARY:Front of House",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ]
)

_VARS = (
    "DEPUTY_API_TOKEN",
    "DEPUTY_BASE_URL",
    "DEPUTY_ALLOW_WRITES",
    "DEPUTY_ALLOW_CUSTOM_HOST",
    "DEPUTY_CACHE_TTL",
    "DEPUTY_TIMEOUT",
    "DEPUTY_MAX_RETRIES",
    "DEPUTY_CALENDAR_URL",
    "DEPUTY_ENV_FILE",
)


@pytest.fixture
def ical_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in _VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DEPUTY_CALENDAR_URL", FEED_URL)
    yield


@pytest.fixture
def feed() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.get(FEED_URL).mock(return_value=httpx.Response(200, text=ICAL_BODY))
        yield router


def test_roster_works_in_ical_mode(
    ical_env: None,
    feed: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`deputy-mcp roster` reads the feed and renders the shift; the URL never leaks."""
    assert cli.main(["roster", "--start", "2035-01-01", "--end", "2035-12-31"]) == 0
    out = capsys.readouterr().out
    assert "My roster" in out
    assert "Front of House" in out
    assert FEED_URL not in out


def test_next_works_in_ical_mode(
    ical_env: None,
    feed: respx.MockRouter,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`deputy-mcp next` returns the caller's own upcoming feed shift."""
    assert cli.main(["next"]) == 0
    assert "Next shift" in capsys.readouterr().out


def test_api_only_subcommand_fails_closed(
    ical_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An API-only subcommand in iCal mode prints the needs-token message + exits 1."""
    assert cli.main(["whoami"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "deputy-mcp error:" in captured.err
    assert "DEPUTY_API_TOKEN" in captured.err
    assert "Traceback" not in captured.err
    assert FEED_URL not in captured.err
