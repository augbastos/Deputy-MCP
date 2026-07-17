"""In-memory FastMCP tests for the Deputy write tools (opt-in, gated).

These tests prove the design's central safety invariant from the MCP surface: the
five write tools are invisible when ``DEPUTY_ALLOW_WRITES`` is false and present when
it is true. When enabled, each tool is exercised against a respx-mocked Deputy API and
its confirmation / error text is checked -- a Deputy failure must surface as an
actionable string, never a traceback.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client

from deputy_mcp.server import create_server

from . import WRITE_TOOL_NAMES, tool_text, wire_write_api


@pytest.fixture
def writes_env(
    deputy_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, str]]:
    """The default env, but with writes enabled (create_server reads env)."""
    monkeypatch.setenv("DEPUTY_ALLOW_WRITES", "true")
    env = {**deputy_env, "DEPUTY_ALLOW_WRITES": "true"}
    yield env


def _wire(
    router: respx.MockRouter,
    make_whoami: Any,
    make_company: Any,
    make_timesheet: Any,
) -> None:
    """Wire the write endpoints (plus the reads a write path needs)."""
    wire_write_api(
        router,
        whoami=make_whoami(),
        company=make_company(),
        swap={
            "Id": 555,
            "SourceRoster": 9001,
            "TargetRoster": 0,
            "Employee": 101,
            "Status": 4,
            "RequestMessage": "Please cover",
        },
        unavailability={"Id": 777, "Type": 0},
        timesheet_started=make_timesheet(Id=8001, EndTime=None, IsInProgress=True, TotalTime=None),
        timesheet_ended=make_timesheet(Id=8001, IsInProgress=False, TotalTime=8.0),
        in_progress_timesheet=make_timesheet(
            Id=8001, EndTime=None, IsInProgress=True, TotalTime=None
        ),
    )


# --------------------------------------------------------------------------- #
# The opt-in invariant, from both sides
# --------------------------------------------------------------------------- #
async def test_write_tools_present_when_enabled(writes_env: dict[str, str]) -> None:
    server = create_server()
    async with Client(server) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names >= WRITE_TOOL_NAMES


@pytest.mark.usefixtures("config")
async def test_write_tools_absent_when_disabled() -> None:
    """Mirror image: the default (writes-disabled) build hides every write tool."""
    server = create_server()
    async with Client(server) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names.isdisjoint(WRITE_TOOL_NAMES)


async def test_write_tools_marked_not_read_only(writes_env: dict[str, str]) -> None:
    server = create_server()
    async with Client(server) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    annotations = tools["deputy_clock_in"].annotations
    assert annotations is not None
    assert annotations.readOnlyHint is False


# --------------------------------------------------------------------------- #
# Each write tool is callable when enabled
# --------------------------------------------------------------------------- #
async def test_claim_open_shift(
    writes_env: dict[str, str],
    deputy_api: respx.MockRouter,
    make_whoami: Any,
    make_company: Any,
    make_timesheet: Any,
) -> None:
    _wire(deputy_api, make_whoami, make_company, make_timesheet)
    server = create_server()
    async with Client(server) as client:
        result = await client.call_tool("deputy_claim_open_shift", {"shift_id": 9001})
    text = tool_text(result)
    assert not result.is_error
    assert "9001" in text
    assert "claimed" in text.lower()


async def test_request_shift_swap(
    writes_env: dict[str, str],
    deputy_api: respx.MockRouter,
    make_whoami: Any,
    make_company: Any,
    make_timesheet: Any,
) -> None:
    _wire(deputy_api, make_whoami, make_company, make_timesheet)
    server = create_server()
    async with Client(server) as client:
        result = await client.call_tool(
            "deputy_request_shift_swap", {"shift_id": 9001, "note": "Please cover"}
        )
    text = tool_text(result)
    assert "555" in text
    assert "Pending Approval" in text


async def test_set_unavailability(
    writes_env: dict[str, str],
    deputy_api: respx.MockRouter,
    make_whoami: Any,
    make_company: Any,
    make_timesheet: Any,
) -> None:
    _wire(deputy_api, make_whoami, make_company, make_timesheet)
    server = create_server()
    async with Client(server) as client:
        result = await client.call_tool(
            "deputy_set_unavailability",
            {"start": "2026-07-20T09:00:00", "end": "2026-07-20T17:00:00", "reason": "Exam"},
        )
    text = tool_text(result)
    assert not result.is_error
    assert "777" in text
    assert "Exam" in text


async def test_clock_in(
    writes_env: dict[str, str],
    deputy_api: respx.MockRouter,
    make_whoami: Any,
    make_company: Any,
    make_timesheet: Any,
) -> None:
    _wire(deputy_api, make_whoami, make_company, make_timesheet)
    server = create_server()
    async with Client(server) as client:
        result = await client.call_tool("deputy_clock_in", {"area_id": 11})
    text = tool_text(result)
    assert not result.is_error
    assert "8001" in text
    assert "clocked in" in text.lower()


async def test_clock_out(
    writes_env: dict[str, str],
    deputy_api: respx.MockRouter,
    make_whoami: Any,
    make_company: Any,
    make_timesheet: Any,
) -> None:
    _wire(deputy_api, make_whoami, make_company, make_timesheet)
    server = create_server()
    async with Client(server) as client:
        result = await client.call_tool("deputy_clock_out", {"mealbreak_minutes": 30})
    text = tool_text(result)
    assert not result.is_error
    assert "8001" in text
    assert "8.0" in text
    assert "30 min" in text


async def test_clock_in_json_format(
    writes_env: dict[str, str],
    deputy_api: respx.MockRouter,
    make_whoami: Any,
    make_company: Any,
    make_timesheet: Any,
) -> None:
    import json

    _wire(deputy_api, make_whoami, make_company, make_timesheet)
    server = create_server()
    async with Client(server) as client:
        result = await client.call_tool(
            "deputy_clock_in", {"area_id": 11, "response_format": "json"}
        )
    parsed = json.loads(tool_text(result))
    assert parsed["timesheet_id"] == 8001
    assert parsed["area_id"] == 11


# --------------------------------------------------------------------------- #
# Write errors surface as actionable text
# --------------------------------------------------------------------------- #
async def test_permission_error_surfaces_as_text(
    writes_env: dict[str, str],
    deputy_api: respx.MockRouter,
    make_whoami: Any,
    make_company: Any,
) -> None:
    deputy_api.get("/resource/Account/WhoAmI").mock(
        return_value=httpx.Response(200, json=make_whoami())
    )
    deputy_api.post("/resource/OperationalUnit/QUERY").mock(
        return_value=httpx.Response(200, json=[])
    )
    deputy_api.post("/supervise/timesheet/start").mock(return_value=httpx.Response(403))
    deputy_api.post("/resource/Company/QUERY").mock(
        return_value=httpx.Response(200, json=[make_company()])
    )
    server = create_server()
    async with Client(server) as client:
        result = await client.call_tool("deputy_clock_in", {"area_id": 11})
    text = tool_text(result)
    assert not result.is_error
    assert "Deputy write did not complete" in text
    assert "403" in text
    assert "Traceback" not in text
