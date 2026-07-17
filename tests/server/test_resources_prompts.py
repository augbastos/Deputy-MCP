"""In-memory FastMCP tests for the Deputy resources and prompts.

Resources render the caller's weekly roster as markdown (mocked API); prompts are
static message templates that steer the assistant toward the right tools. Both are
driven through the real :class:`fastmcp.Client`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client

from deputy_mcp.server import create_server
from deputy_mcp.server.resources import _week_bounds

pytestmark = pytest.mark.usefixtures("config")

_THIS_WEEK = "deputy://my/roster/this-week"
_NEXT_WEEK = "deputy://my/roster/next-week"


def _wire_roster(
    router: respx.MockRouter,
    roster: dict[str, Any],
    company: dict[str, Any],
    whoami: dict[str, Any],
) -> None:
    """Mock the endpoints a weekly-roster resource reads.

    ``get_my_roster`` reads the self-service ``/my/roster`` for a current/future window
    and only falls back to the admin ``Roster/QUERY`` when the window reaches the past, so
    both are wired to the same in-window shift. Identity resolves from ``/me`` and the
    timezone from ``Company/QUERY`` (``/me`` here omits ``CompanyObject``).
    """
    router.get("/me").mock(return_value=httpx.Response(200, json=whoami))
    router.get("/my/roster").mock(return_value=httpx.Response(200, json=[roster]))
    router.post("/resource/Roster/QUERY").mock(return_value=httpx.Response(200, json=[roster]))
    router.post("/resource/Company/QUERY").mock(return_value=httpx.Response(200, json=[company]))


# --------------------------------------------------------------------------- #
# Resources
# --------------------------------------------------------------------------- #
async def test_resources_are_listed() -> None:
    server = create_server()
    async with Client(server) as client:
        uris = {str(res.uri) for res in await client.list_resources()}
    assert _THIS_WEEK in uris
    assert _NEXT_WEEK in uris


@pytest.mark.parametrize(("uri", "offset"), [(_THIS_WEEK, 0), (_NEXT_WEEK, 1)])
async def test_weekly_roster_resource_renders(
    uri: str,
    offset: int,
    deputy_api: respx.MockRouter,
    make_roster: Any,
    make_company: Any,
    make_whoami: Any,
) -> None:
    # Date the mocked shift inside the requested week so it survives the client-side
    # window filter regardless of whether the /my/roster or Roster/QUERY path is taken.
    start, _ = _week_bounds(offset)
    roster = make_roster(Date=(start + timedelta(days=1)).isoformat())
    _wire_roster(deputy_api, roster, make_company(), make_whoami())
    server = create_server()
    async with Client(server) as client:
        contents = await client.read_resource(uri)
    body = contents[0].text
    assert "My roster" in body
    assert "Europe/Dublin" in body


async def test_resource_error_is_graceful(deputy_api: respx.MockRouter) -> None:
    """A failing read degrades to an explanatory document, not an exception."""
    # get_my_roster reads /me (past-window path) or /my/roster (future path); a 401 on
    # either fails the whole read, so both are wired to reject regardless of the weekday.
    deputy_api.get("/me").mock(return_value=httpx.Response(401))
    deputy_api.get("/my/roster").mock(return_value=httpx.Response(401))
    server = create_server()
    async with Client(server) as client:
        contents = await client.read_resource(_THIS_WEEK)
    body = contents[0].text
    assert "Could not load roster" in body
    assert "401" in body


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #
async def test_prompts_are_listed() -> None:
    server = create_server()
    async with Client(server) as client:
        names = {prompt.name for prompt in await client.list_prompts()}
    assert {"summarize_my_week", "coverage_check"} <= names


async def test_summarize_my_week_prompt() -> None:
    server = create_server()
    async with Client(server) as client:
        result = await client.get_prompt("summarize_my_week", {})
    text = result.messages[0].content.text
    assert "deputy_get_my_roster" in text
    assert "deputy_get_my_timesheets" in text


async def test_coverage_check_prompt_embeds_date() -> None:
    server = create_server()
    async with Client(server) as client:
        result = await client.get_prompt("coverage_check", {"date": "2026-07-20"})
    text = result.messages[0].content.text
    assert "2026-07-20" in text
    assert "deputy_get_team_roster" in text
    assert "deputy_search_shifts" in text
