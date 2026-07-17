"""In-memory FastMCP tests for the Deputy resources and prompts.

Resources render the caller's weekly roster as markdown (mocked API); prompts are
static message templates that steer the assistant toward the right tools. Both are
driven through the real :class:`fastmcp.Client`.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client

from deputy_mcp.server import create_server

pytestmark = pytest.mark.usefixtures("config")

_THIS_WEEK = "deputy://my/roster/this-week"
_NEXT_WEEK = "deputy://my/roster/next-week"


def _wire_roster(
    router: respx.MockRouter,
    make_roster: Any,
    make_company: Any,
) -> None:
    """Mock the endpoints a weekly-roster resource reads."""
    # The resource has no date filter on /my/roster, so make the shift land in any
    # requested week by giving it no Date (kept by the range filter) — a real shift
    # with a fixed 2021 Date would be filtered out of the current calendar week.
    router.get("/my/roster").mock(return_value=httpx.Response(200, json=[make_roster(Date=None)]))
    router.post("/resource/Company/QUERY").mock(
        return_value=httpx.Response(200, json=[make_company()])
    )


# --------------------------------------------------------------------------- #
# Resources
# --------------------------------------------------------------------------- #
async def test_resources_are_listed() -> None:
    server = create_server()
    async with Client(server) as client:
        uris = {str(res.uri) for res in await client.list_resources()}
    assert _THIS_WEEK in uris
    assert _NEXT_WEEK in uris


@pytest.mark.parametrize("uri", [_THIS_WEEK, _NEXT_WEEK])
async def test_weekly_roster_resource_renders(
    uri: str,
    deputy_api: respx.MockRouter,
    make_roster: Any,
    make_company: Any,
) -> None:
    _wire_roster(deputy_api, make_roster, make_company)
    server = create_server()
    async with Client(server) as client:
        contents = await client.read_resource(uri)
    body = contents[0].text
    assert "My roster" in body
    assert "Europe/Dublin" in body


async def test_resource_error_is_graceful(deputy_api: respx.MockRouter) -> None:
    """A failing read degrades to an explanatory document, not an exception."""
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
