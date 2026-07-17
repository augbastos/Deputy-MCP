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
    make_whoami: Any,
) -> None:
    """Mock the endpoints a weekly-roster resource reads."""
    # get_my_roster resolves the caller's own id (WhoAmI) then queries the Roster
    # resource by employee id + Date range. respx returns the same payload regardless
    # of the range in the body, so a single shift stands in for the week.
    router.get("/resource/Account/WhoAmI").mock(
        return_value=httpx.Response(200, json=make_whoami())
    )
    router.post("/resource/Roster/QUERY").mock(
        return_value=httpx.Response(200, json=[make_roster()])
    )
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
    make_whoami: Any,
) -> None:
    _wire_roster(deputy_api, make_roster, make_company, make_whoami)
    server = create_server()
    async with Client(server) as client:
        contents = await client.read_resource(uri)
    body = contents[0].text
    assert "My roster" in body
    assert "Europe/Dublin" in body


async def test_resource_error_is_graceful(deputy_api: respx.MockRouter) -> None:
    """A failing read degrades to an explanatory document, not an exception."""
    # get_my_roster resolves own id via WhoAmI first; a 401 there fails the whole read.
    deputy_api.get("/resource/Account/WhoAmI").mock(return_value=httpx.Response(401))
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
