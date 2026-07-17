"""In-memory FastMCP tests for the Deputy read tools.

Every test builds the real server via :func:`deputy_mcp.server.create_server` (writes
disabled by the default env), connects the real :class:`fastmcp.Client`, and mocks
Deputy's HTTP API with respx. Assertions cover the opt-in invariant (no write tools
when writes are off), that each read tool is callable and renders both markdown and
JSON, and that a Deputy error surfaces as an actionable string rather than a traceback.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client

from deputy_mcp.server import create_server

from . import READ_TOOL_NAMES, WRITE_TOOL_NAMES, tool_text, wire_read_api

pytestmark = pytest.mark.usefixtures("config")

PayloadFactory = Callable[..., dict[str, Any]]


def _wire(
    router: respx.MockRouter,
    make_whoami: PayloadFactory,
    make_company: PayloadFactory,
    make_employee: PayloadFactory,
    make_operational_unit: PayloadFactory,
    make_roster: PayloadFactory,
    make_timesheet: PayloadFactory,
    sample_employees: list[dict[str, Any]],
) -> None:
    """Register a full read API on ``router`` using the shared factories."""
    wire_read_api(
        router,
        whoami=make_whoami(),
        company=make_company(),
        employees=sample_employees,
        operational_units=[make_operational_unit()],
        rosters=[make_roster()],
        timesheets=[make_timesheet()],
    )


# --------------------------------------------------------------------------- #
# Tool listing / opt-in invariant
# --------------------------------------------------------------------------- #
async def test_all_read_tools_registered() -> None:
    server = create_server()
    async with Client(server) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names >= READ_TOOL_NAMES


async def test_write_tools_absent_when_writes_disabled() -> None:
    """The core opt-in invariant: read-only build never exposes a write tool."""
    server = create_server()
    async with Client(server) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names.isdisjoint(WRITE_TOOL_NAMES)
    # And nothing outside the documented read set leaks in either.
    assert names == set(READ_TOOL_NAMES)


async def test_read_tools_are_marked_read_only() -> None:
    server = create_server()
    async with Client(server) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    annotations = tools["deputy_whoami"].annotations
    assert annotations is not None
    assert annotations.readOnlyHint is True


# --------------------------------------------------------------------------- #
# Each read tool is callable against the mocked API
# --------------------------------------------------------------------------- #
async def test_whoami_tool_markdown(
    deputy_api: respx.MockRouter,
    make_whoami: PayloadFactory,
    make_company: PayloadFactory,
    make_employee: PayloadFactory,
    make_operational_unit: PayloadFactory,
    make_roster: PayloadFactory,
    make_timesheet: PayloadFactory,
    sample_employees: list[dict[str, Any]],
) -> None:
    _wire(
        deputy_api,
        make_whoami,
        make_company,
        make_employee,
        make_operational_unit,
        make_roster,
        make_timesheet,
        sample_employees,
    )
    server = create_server()
    async with Client(server) as client:
        result = await client.call_tool("deputy_whoami", {})
    text = tool_text(result)
    assert "Deputy connection OK" in text
    assert "Alex Rivera" in text
    assert "Europe/Dublin" in text


@pytest.mark.parametrize(
    ("tool", "args", "expected"),
    [
        ("deputy_get_my_roster", {}, "My roster"),
        ("deputy_get_team_roster", {}, "Team roster"),
        ("deputy_who_is_working", {}, "Who is working"),
        ("deputy_get_employee_info", {"name_or_id": "Alex"}, "Employee info"),
        ("deputy_search_shifts", {}, "shifts"),
        ("deputy_get_areas", {}, "Areas"),
        ("deputy_next_shift", {}, "Next shift"),
        ("deputy_get_my_timesheets", {}, "My timesheets"),
    ],
)
async def test_read_tool_renders_markdown(
    tool: str,
    args: dict[str, Any],
    expected: str,
    deputy_api: respx.MockRouter,
    make_whoami: PayloadFactory,
    make_company: PayloadFactory,
    make_employee: PayloadFactory,
    make_operational_unit: PayloadFactory,
    make_roster: PayloadFactory,
    make_timesheet: PayloadFactory,
    sample_employees: list[dict[str, Any]],
) -> None:
    _wire(
        deputy_api,
        make_whoami,
        make_company,
        make_employee,
        make_operational_unit,
        make_roster,
        make_timesheet,
        sample_employees,
    )
    server = create_server()
    async with Client(server) as client:
        result = await client.call_tool(tool, args)
    text = tool_text(result)
    assert expected.lower() in text.lower()
    assert not result.is_error


async def test_search_shifts_ambiguous_employee_lists_matches(
    deputy_api: respx.MockRouter,
    make_whoami: PayloadFactory,
    make_company: PayloadFactory,
    make_employee: PayloadFactory,
    make_operational_unit: PayloadFactory,
    make_roster: PayloadFactory,
    make_timesheet: PayloadFactory,
    sample_employees: list[dict[str, Any]],
) -> None:
    """A name that matches several people asks to retry by id instead of guessing."""
    _wire(
        deputy_api,
        make_whoami,
        make_company,
        make_employee,
        make_operational_unit,
        make_roster,
        make_timesheet,
        sample_employees,  # Employee/QUERY returns three people
    )
    server = create_server()
    async with Client(server) as client:
        result = await client.call_tool("deputy_search_shifts", {"employee": "a"})
    text = tool_text(result)
    # The tool caught the ambiguity and returned actionable prose (not an error result).
    assert not result.is_error
    assert "Multiple employees match" in text
    assert "id 101" in text
    assert "id 102" in text


async def test_next_shift_single_employee_match_resolves(
    deputy_api: respx.MockRouter,
    make_whoami: PayloadFactory,
    make_company: PayloadFactory,
    make_employee: PayloadFactory,
    make_operational_unit: PayloadFactory,
    make_roster: PayloadFactory,
    make_timesheet: PayloadFactory,
) -> None:
    """Exactly one active match resolves to that id and the lookup proceeds normally."""
    wire_read_api(
        deputy_api,
        whoami=make_whoami(),
        company=make_company(),
        employees=[make_employee()],  # a single, unambiguous match
        operational_units=[make_operational_unit()],
        rosters=[make_roster()],
        timesheets=[make_timesheet()],
    )
    server = create_server()
    async with Client(server) as client:
        result = await client.call_tool("deputy_next_shift", {"employee": "Alex"})
    text = tool_text(result)
    assert not result.is_error
    assert "Next shift" in text


async def test_get_employee_info_by_numeric_id(
    deputy_api: respx.MockRouter,
    make_whoami: PayloadFactory,
    make_company: PayloadFactory,
    make_employee: PayloadFactory,
    make_operational_unit: PayloadFactory,
    make_roster: PayloadFactory,
    make_timesheet: PayloadFactory,
    sample_employees: list[dict[str, Any]],
) -> None:
    """A numeric reference hits GET /resource/Employee/{id}, not the QUERY path."""
    _wire(
        deputy_api,
        make_whoami,
        make_company,
        make_employee,
        make_operational_unit,
        make_roster,
        make_timesheet,
        sample_employees,
    )
    server = create_server()
    async with Client(server) as client:
        result = await client.call_tool("deputy_get_employee_info", {"name_or_id": "101"})
    assert "Alex Rivera" in tool_text(result)


# --------------------------------------------------------------------------- #
# response_format: markdown vs json
# --------------------------------------------------------------------------- #
async def test_response_format_json_is_valid_json(
    deputy_api: respx.MockRouter,
    make_whoami: PayloadFactory,
    make_company: PayloadFactory,
    make_employee: PayloadFactory,
    make_operational_unit: PayloadFactory,
    make_roster: PayloadFactory,
    make_timesheet: PayloadFactory,
    sample_employees: list[dict[str, Any]],
) -> None:
    _wire(
        deputy_api,
        make_whoami,
        make_company,
        make_employee,
        make_operational_unit,
        make_roster,
        make_timesheet,
        sample_employees,
    )
    server = create_server()
    async with Client(server) as client:
        md = tool_text(await client.call_tool("deputy_get_areas", {}))
        js = tool_text(await client.call_tool("deputy_get_areas", {"response_format": "json"}))
    # markdown is prose; json parses into structured records.
    assert md.lstrip().startswith("#")
    parsed = json.loads(js)
    assert isinstance(parsed, list)
    assert parsed[0]["OperationalUnitName"] == "Front of House"


async def test_search_shifts_json_has_pagination_cursor(
    deputy_api: respx.MockRouter,
    make_whoami: PayloadFactory,
    make_company: PayloadFactory,
    make_employee: PayloadFactory,
    make_operational_unit: PayloadFactory,
    make_roster: PayloadFactory,
    make_timesheet: PayloadFactory,
    sample_employees: list[dict[str, Any]],
) -> None:
    """A full page must report has_more + next_offset so a caller can page on."""
    _wire(
        deputy_api,
        make_whoami,
        make_company,
        make_employee,
        make_operational_unit,
        make_roster,
        make_timesheet,
        sample_employees,
    )
    server = create_server()
    async with Client(server) as client:
        # Mock returns one shift; limit=1 -> a full page -> more may exist.
        js = tool_text(
            await client.call_tool("deputy_search_shifts", {"limit": 1, "response_format": "json"})
        )
    parsed = json.loads(js)
    assert "shifts" in parsed
    assert parsed["pagination"]["has_more"] is True
    assert parsed["pagination"]["next_offset"] == 1
    assert parsed["pagination"]["returned"] == 1


async def test_who_is_working_json_has_both_lists(
    deputy_api: respx.MockRouter,
    make_whoami: PayloadFactory,
    make_company: PayloadFactory,
    make_employee: PayloadFactory,
    make_operational_unit: PayloadFactory,
    make_roster: PayloadFactory,
    make_timesheet: PayloadFactory,
    sample_employees: list[dict[str, Any]],
) -> None:
    _wire(
        deputy_api,
        make_whoami,
        make_company,
        make_employee,
        make_operational_unit,
        make_roster,
        make_timesheet,
        sample_employees,
    )
    server = create_server()
    async with Client(server) as client:
        js = tool_text(await client.call_tool("deputy_who_is_working", {"response_format": "json"}))
    parsed = json.loads(js)
    assert "clocked_in" in parsed
    assert "rostered_now" in parsed


# --------------------------------------------------------------------------- #
# Error surface: actionable string, not a traceback
# --------------------------------------------------------------------------- #
async def test_bad_token_surfaces_actionable_text(deputy_api: respx.MockRouter) -> None:
    deputy_api.get("/resource/Account/WhoAmI").mock(return_value=httpx.Response(401))
    server = create_server()
    async with Client(server) as client:
        result = await client.call_tool("deputy_whoami", {})
    text = tool_text(result)
    # The tool caught the DeputyError and returned prose, so the call did not error.
    assert not result.is_error
    assert text.startswith("Error:")
    assert "401" in text
    assert "DEPUTY_API_TOKEN" in text
    assert "Traceback" not in text


async def test_invalid_date_argument_is_actionable(
    deputy_api: respx.MockRouter,
    make_whoami: PayloadFactory,
    make_company: PayloadFactory,
    make_employee: PayloadFactory,
    make_operational_unit: PayloadFactory,
    make_roster: PayloadFactory,
    make_timesheet: PayloadFactory,
    sample_employees: list[dict[str, Any]],
) -> None:
    _wire(
        deputy_api,
        make_whoami,
        make_company,
        make_employee,
        make_operational_unit,
        make_roster,
        make_timesheet,
        sample_employees,
    )
    server = create_server()
    async with Client(server) as client:
        result = await client.call_tool("deputy_get_my_roster", {"start_date": "not-a-date"})
    text = tool_text(result)
    assert "Invalid date" in text
    assert "YYYY-MM-DD" in text
