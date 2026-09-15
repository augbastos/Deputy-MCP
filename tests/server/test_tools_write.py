"""In-memory FastMCP tests for the Deputy write tools (opt-in, gated).

These tests prove the design's central safety invariant from the MCP surface: the
five write tools are invisible when ``DEPUTY_ALLOW_WRITES`` is false and present when
it is true. When enabled, each tool is exercised against a respx-mocked Deputy API and
its confirmation / error text is checked -- a Deputy failure must surface as an
actionable string, never a traceback.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult
from mcp.shared.exceptions import MCPError

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
        # clock_out reads the in-progress timesheet from /me's InProgressTS.
        whoami=make_whoami(InProgressTS=8001),
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
    assert annotations.read_only_hint is False


# --------------------------------------------------------------------------- #
# Each write tool is callable when enabled
# --------------------------------------------------------------------------- #
async def _decline(message: str, response_type: Any, params: Any, context: Any) -> ElicitResult:
    return ElicitResult(action="decline")


# ``auto`` negotiates MCP 2026-07-28 (guard pattern); ``legacy`` stays on the handshake era.
_ERAS = pytest.mark.parametrize("mode", ["auto", "legacy"])


@_ERAS
async def test_claim_open_shift_runs_only_after_the_user_approves(
    mode: str,
    writes_env: dict[str, str],
    deputy_api: respx.MockRouter,
    make_whoami: Any,
    make_company: Any,
    make_timesheet: Any,
) -> None:
    _wire(deputy_api, make_whoami, make_company, make_timesheet)
    prompts: list[str] = []

    async def approve(message: str, response_type: Any, params: Any, context: Any) -> Any:
        prompts.append(message)
        return {"value": True}

    server = create_server()
    async with Client(server, mode=mode, elicitation_handler=approve) as client:
        result = await client.call_tool("deputy_claim_open_shift", {"shift_id": 9001})
    text = tool_text(result)
    assert not result.is_error
    assert "Open shift 9001 claimed" in text
    assert deputy_api.routes["claim"].call_count == 1
    # The user saw which shift, when, and that approval is skipped.
    assert len(prompts) == 1
    assert "#9001" in prompts[0] and "2021-01-01" in prompts[0]
    assert "manager approval" in prompts[0]


@_ERAS
async def test_declined_claim_changes_nothing(
    mode: str,
    writes_env: dict[str, str],
    deputy_api: respx.MockRouter,
    make_whoami: Any,
    make_company: Any,
    make_timesheet: Any,
) -> None:
    _wire(deputy_api, make_whoami, make_company, make_timesheet)
    server = create_server()
    async with Client(server, mode=mode, elicitation_handler=_decline) as client:
        result = await client.call_tool(
            "deputy_claim_open_shift", {"shift_id": 9001, "response_format": "json"}
        )
    assert json.loads(tool_text(result)) == {
        "shift_id": 9001,
        "claimed": False,
        "reason": "declined",
    }
    assert deputy_api.routes["claim"].call_count == 0


async def test_handshake_client_without_elicitation_cannot_claim(
    writes_env: dict[str, str],
    deputy_api: respx.MockRouter,
    make_whoami: Any,
    make_company: Any,
    make_timesheet: Any,
) -> None:
    _wire(deputy_api, make_whoami, make_company, make_timesheet)
    server = create_server()
    async with Client(server, mode="legacy") as client:
        result = await client.call_tool("deputy_claim_open_shift", {"shift_id": 9001})
    text = tool_text(result)
    assert "was not claimed" in text
    assert "does not support elicitation" in text
    assert deputy_api.routes["claim"].call_count == 0


async def test_modern_client_without_elicitation_cannot_claim(
    writes_env: dict[str, str],
    deputy_api: respx.MockRouter,
    make_whoami: Any,
    make_company: Any,
    make_timesheet: Any,
) -> None:
    _wire(deputy_api, make_whoami, make_company, make_timesheet)
    server = create_server()
    async with Client(server) as client:
        # The server asks for input; a client that cannot elicit refuses the round.
        with pytest.raises(MCPError):
            await client.call_tool("deputy_claim_open_shift", {"shift_id": 9001})
    assert deputy_api.routes["claim"].call_count == 0


async def test_claiming_a_shift_that_is_not_open_never_prompts(
    writes_env: dict[str, str],
    deputy_api: respx.MockRouter,
    make_whoami: Any,
    make_company: Any,
    make_timesheet: Any,
) -> None:
    _wire(deputy_api, make_whoami, make_company, make_timesheet)
    deputy_api.get(path__regex=r"/resource/Roster/\d+$").mock(
        return_value=httpx.Response(200, json={"Id": 9001, "Open": False, "Employee": 102})
    )
    prompted: list[str] = []

    async def spy(message: str, response_type: Any, params: Any, context: Any) -> Any:
        prompted.append(message)
        return {"value": True}

    server = create_server()
    async with Client(server, elicitation_handler=spy) as client:
        result = await client.call_tool("deputy_claim_open_shift", {"shift_id": 9001})
    assert "not an open shift" in tool_text(result)
    assert prompted == []
    assert deputy_api.routes["claim"].call_count == 0


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
    deputy_api.get("/me").mock(return_value=httpx.Response(200, json=make_whoami()))
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
