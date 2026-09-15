"""Agent-interface evals: does the MCP surface steer a model to the right, safe call?

Unit tests prove the Python works. These evals check what a model actually sees and
triggers through the protocol: the advertised tool list and schemas, the descriptions it
uses to pick between similar tools, which Deputy endpoints a given tool call reaches, and
whether answers stay short, consistent and free of personal data.

They are deterministic by design. Nothing here asks an LLM to choose a tool, because a
model-in-the-loop benchmark would be slow, flaky and would measure the model more than
the interface. Instead each eval asserts the property that makes the right choice easy
and the wrong one harmless, through a real in-memory FastMCP client against a
respx-mocked Deputy API. Run just this file with ``uv run pytest -m evals``.

All data is fictional (see ``tests/conftest.py``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from mcp.types import Tool

from deputy_mcp.server import create_server

from . import READ_TOOL_NAMES, WRITE_TOOL_NAMES, tool_text

pytestmark = pytest.mark.evals

Factory = Callable[..., dict[str, Any]]
_ERAS = pytest.mark.parametrize("mode", ["auto", "legacy"])

#: Tools that read across people and so need a manager/administrator access level.
_MANAGER_TOOLS = frozenset(
    {
        "deputy_get_team_roster",
        "deputy_who_is_working",
        "deputy_get_employee_info",
        "deputy_search_shifts",
    }
)
#: Tools whose whole point is the signed-in user's own data.
_SELF_SERVICE_TOOLS = frozenset(
    {"deputy_get_my_roster", "deputy_next_shift", "deputy_get_my_timesheets"}
)


def _ok(payload: Any) -> httpx.Response:
    return httpx.Response(200, json=payload)


@pytest.fixture
def employee_me(make_whoami: Factory, make_company: Factory) -> dict[str, Any]:
    """A /me record shaped like a plain employee's: it embeds the company object."""
    return make_whoami(CompanyObject=make_company())


@pytest.fixture
def api(
    deputy_env: dict[str, str],
    deputy_api: respx.MockRouter,
    employee_me: dict[str, Any],
    make_roster: Factory,
    make_timesheet: Factory,
    make_operational_unit: Factory,
    sample_employees: list[dict[str, Any]],
) -> respx.MockRouter:
    """Every read endpoint, answering like an employee-level token would for /my/*."""
    upcoming = make_roster(
        StartTime=4_100_000_000,
        EndTime=4_100_028_800,
        Date="2099-12-01",
        OperationalUnitObject={"Id": 11, "OperationalUnitName": "Front of House"},
    )
    deputy_api.get("/me").mock(return_value=_ok(employee_me))
    deputy_api.get("/my/roster").mock(return_value=_ok([upcoming]))
    deputy_api.get("/my/timesheets").mock(return_value=_ok([make_timesheet()]))
    deputy_api.get("/my/colleague").mock(return_value=_ok([]))
    deputy_api.post("/resource/Employee/QUERY").mock(return_value=_ok(sample_employees))
    deputy_api.post("/resource/Roster/QUERY").mock(return_value=_ok([make_roster()]))
    deputy_api.post("/resource/Timesheet/QUERY").mock(return_value=_ok([make_timesheet()]))
    deputy_api.post("/resource/OperationalUnit/QUERY").mock(
        return_value=_ok([make_operational_unit()])
    )
    deputy_api.post("/resource/Company/QUERY").mock(return_value=_ok([]))
    deputy_api.get(path__regex=r"/resource/Employee/\d+$").mock(
        return_value=_ok(sample_employees[0])
    )
    return deputy_api


def _paths(router: respx.MockRouter) -> list[str]:
    return [call.request.url.path.removeprefix("/api/v1") for call in router.calls]


async def _tools(mode: str = "auto") -> dict[str, Tool]:
    async with Client(create_server(), mode=mode) as client:
        return {tool.name: tool for tool in await client.list_tools()}


# --------------------------------------------------------------------------- #
# 1. "What is my next shift?" takes the self-service path, nothing else
# --------------------------------------------------------------------------- #
async def test_next_shift_for_me_only_reaches_self_service_endpoints(
    api: respx.MockRouter,
) -> None:
    async with Client(create_server()) as client:
        text = tool_text(await client.call_tool("deputy_next_shift", {}))
    assert "Front of House" in text
    assert set(_paths(api)) <= {"/me", "/my/roster"}


async def test_my_roster_this_week_never_calls_a_manager_endpoint(api: respx.MockRouter) -> None:
    async with Client(create_server()) as client:
        await client.call_tool("deputy_get_my_roster", {})
    assert not [path for path in _paths(api) if path.startswith("/resource/")]


# --------------------------------------------------------------------------- #
# 2. Asking about someone else without permission degrades safely
# --------------------------------------------------------------------------- #
async def test_other_employee_without_permission_gets_a_short_redirect(
    api: respx.MockRouter,
) -> None:
    api.post("/resource/Employee/QUERY").mock(
        return_value=httpx.Response(
            403, json={"error": {"code": 403, "message": "Access to object-type denied"}}
        )
    )
    async with Client(create_server()) as client:
        result = await client.call_tool("deputy_next_shift", {"employee": "Sam"})
    text = tool_text(result)
    assert not result.is_error  # an answer the model can act on, not a protocol failure
    assert text.startswith("Error:")
    assert "manager or administrator" in text
    assert "deputy_get_my_roster" in text  # a working next step
    assert "Traceback" not in text
    assert len(text) < 700


# --------------------------------------------------------------------------- #
# 3. An ambiguous name never silently selects a person
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("deputy_next_shift", {"employee": "Alex"}),
        ("deputy_search_shifts", {"employee": "Alex"}),
    ],
)
async def test_ambiguous_name_lists_matches_and_reads_no_shifts(
    api: respx.MockRouter, make_employee: Factory, tool: str, args: dict[str, Any]
) -> None:
    api.post("/resource/Employee/QUERY").mock(
        return_value=_ok(
            [make_employee(), make_employee(Id=104, DisplayName="Alex Byrne", LastName="Byrne")]
        )
    )
    async with Client(create_server()) as client:
        text = tool_text(await client.call_tool(tool, args))
    assert "Multiple employees match 'Alex'" in text
    assert "(id 101)" in text and "(id 104)" in text
    assert "/resource/Roster/QUERY" not in _paths(api)


# --------------------------------------------------------------------------- #
# 4. Read-only requests never write, even with writes enabled
# --------------------------------------------------------------------------- #
_READ_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("deputy_whoami", {}),
    ("deputy_get_my_calendar_url", {}),
    ("deputy_get_my_roster", {"start_date": "2020-01-01", "end_date": "2099-12-31"}),
    ("deputy_next_shift", {"employee": "102"}),
    ("deputy_get_team_roster", {"date": "2021-01-01"}),
    ("deputy_who_is_working", {}),
    ("deputy_get_employee_info", {"name_or_id": "101"}),
    ("deputy_search_shifts", {"open_only": True}),
    ("deputy_get_areas", {}),
    ("deputy_get_my_timesheets", {"start_date": "2020-01-01"}),
    ("deputy_get_my_colleagues", {"same_workplace_only": False}),
]


async def test_every_read_tool_is_covered_by_the_read_only_eval() -> None:
    assert {name for name, _ in _READ_CALLS} == READ_TOOL_NAMES


async def test_read_tools_never_send_a_write_request(
    api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEPUTY_ALLOW_WRITES", "true")
    async with Client(create_server()) as client:
        for name, args in _READ_CALLS:
            for response_format in ("markdown", "json"):
                await client.call_tool(name, {**args, "response_format": response_format})
        for resource in await client.list_resources():
            await client.read_resource(resource.uri)
    writes = [
        f"{call.request.method} {call.request.url.path}"
        for call in api.calls
        if call.request.method != "GET" and not call.request.url.path.endswith("/QUERY")
    ]
    assert writes == []


# --------------------------------------------------------------------------- #
# 5. With writes off, write tools do not exist on either protocol era
# --------------------------------------------------------------------------- #
@_ERAS
async def test_write_tools_are_absent_when_writes_are_off(deputy_env: Any, mode: str) -> None:
    tools = await _tools(mode)
    assert set(tools) == READ_TOOL_NAMES
    async with Client(create_server(), mode=mode) as client:
        result = await client.call_tool("deputy_clock_in", {}, raise_on_error=False)
    # Asking for a write tool that does not exist is a protocol error, not a Deputy call.
    assert result.is_error


async def test_both_protocol_eras_advertise_identical_tools(
    monkeypatch: Any, deputy_env: Any
) -> None:
    monkeypatch.setenv("DEPUTY_ALLOW_WRITES", "true")
    modern, legacy = await _tools("auto"), await _tools("legacy")
    assert modern.keys() == legacy.keys()
    for name in modern:
        assert modern[name].input_schema == legacy[name].input_schema, name
        assert modern[name].description == legacy[name].description, name


# --------------------------------------------------------------------------- #
# 6. Clock in/out: schema and semantics a model can rely on
# --------------------------------------------------------------------------- #
async def test_clock_in_and_out_schemas_and_hints(monkeypatch: Any, deputy_env: Any) -> None:
    monkeypatch.setenv("DEPUTY_ALLOW_WRITES", "true")
    tools = await _tools()
    clock_in, clock_out = tools["deputy_clock_in"], tools["deputy_clock_out"]

    assert set(clock_in.input_schema["properties"]) == {"area_id", "response_format"}
    assert "required" not in clock_in.input_schema
    area = clock_in.input_schema["properties"]["area_id"]["anyOf"][0]
    assert area == {"exclusiveMinimum": 0, "type": "integer"}

    # Clock-out resolves the running timesheet itself: no id for the model to invent.
    assert set(clock_out.input_schema["properties"]) == {"mealbreak_minutes", "response_format"}
    assert clock_out.input_schema["properties"]["mealbreak_minutes"]["anyOf"][0]["minimum"] == 0
    assert "keep the" not in (clock_in.description or "").lower()
    assert "no timesheet id is needed" in " ".join((clock_out.description or "").split())

    for tool in (clock_in, clock_out):
        hints = tool.annotations
        assert hints is not None and hints.title
        assert hints.read_only_hint is False
        assert hints.destructive_hint is False
        assert hints.idempotent_hint is False  # two clock-ins are two timesheets


# --------------------------------------------------------------------------- #
# 7. Authorization failures are short and useful
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("status", "phrase"), [(401, "HTTP 401"), (403, "manager or administrator")]
)
async def test_auth_errors_are_short_actionable_and_body_free(
    api: respx.MockRouter, status: int, phrase: str
) -> None:
    api.post("/resource/Roster/QUERY").mock(
        return_value=httpx.Response(status, text="internal-trace 7f3e9a1c secret stack dump")
    )
    async with Client(create_server()) as client:
        text = tool_text(await client.call_tool("deputy_get_team_roster", {}))
    assert phrase in text
    assert "Hint:" in text
    assert "stack dump" not in text
    assert text.count("\n") <= 2 and len(text) < 600


# --------------------------------------------------------------------------- #
# 8. Answers do not leak personal data or secrets
# --------------------------------------------------------------------------- #
async def test_employee_and_joined_employee_json_carry_no_pii(
    api: respx.MockRouter, make_employee: Factory, make_roster: Factory, test_token: str
) -> None:
    person = make_employee(DateOfBirth="1990-02-03", Email="alex.rivera@example.com")
    api.post("/resource/Employee/QUERY").mock(return_value=_ok([person]))
    api.post("/resource/Roster/QUERY").mock(return_value=_ok([make_roster(EmployeeObject=person)]))
    outputs: list[str] = []
    async with Client(create_server()) as client:
        for name, args in (
            ("deputy_get_employee_info", {"name_or_id": "Alex"}),
            ("deputy_get_team_roster", {}),
            ("deputy_search_shifts", {}),
        ):
            for response_format in ("markdown", "json"):
                args_with_format = {**args, "response_format": response_format}
                outputs.append(tool_text(await client.call_tool(name, args_with_format)))
    for text in outputs:
        assert "1990-02-03" not in text
        assert "alex.rivera@example.com" not in text
        assert test_token not in text
    assert any("Alex Rivera" in text for text in outputs)  # the useful part survives


async def test_upstream_error_body_is_redacted_before_the_model_sees_it(
    api: respx.MockRouter,
) -> None:
    api.get("/my/timesheets").mock(
        return_value=httpx.Response(
            500,
            json={
                "message": "database timeout",
                "access_token": "a1b2c3d4e5f60718293a4b5c6d7e8f90",
                "debug": "user jo.murphy@example.com",
            },
        )
    )
    async with Client(create_server()) as client:
        text = tool_text(await client.call_tool("deputy_get_my_timesheets", {}))
    assert "database timeout" in text
    assert "a1b2c3d4e5f60718293a4b5c6d7e8f90" not in text
    assert "jo.murphy@example.com" not in text


# --------------------------------------------------------------------------- #
# 9. Markdown and JSON describe the same thing, as each description promises
# --------------------------------------------------------------------------- #
def _documented_json_keys(description: str) -> set[str]:
    """Top-level keys of the ``{"a", "b": ...}`` JSON contract named in a description."""
    match = re.search(r'with\s+response_format="json",.*?``(\{.*?\})``', description, re.S)
    if match is None:
        return set()
    contract = match.group(1)
    depth, top = 0, []
    for char in contract[1:-1]:
        depth += char in "[{"
        depth -= char in "]}"
        top.append(char if depth == 0 else " ")
    return set(re.findall(r'"(\w+)"', "".join(top)))


async def test_json_output_matches_the_contract_each_description_documents(
    api: respx.MockRouter,
) -> None:
    async with Client(create_server()) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        checked = 0
        for name, args in _READ_CALLS:
            markdown = tool_text(await client.call_tool(name, args))
            raw = tool_text(await client.call_tool(name, {**args, "response_format": "json"}))
            assert markdown.startswith("###"), (name, markdown[:80])
            payload = json.loads(raw)
            keys = _documented_json_keys(tools[name].description or "")
            if keys:
                assert isinstance(payload, dict), name
                assert set(payload) == keys, (name, sorted(payload), sorted(keys))
                checked += 1
            else:
                assert isinstance(payload, list | dict | None), name
    assert checked >= 4  # whoami, calendar url, who is working, search shifts


# --------------------------------------------------------------------------- #
# 10. Descriptions help a model tell similar tools apart
# --------------------------------------------------------------------------- #
async def test_descriptions_disambiguate_and_never_point_at_missing_tools(
    monkeypatch: Any, deputy_env: Any
) -> None:
    monkeypatch.setenv("DEPUTY_ALLOW_WRITES", "true")
    tools = await _tools()
    assert set(tools) == READ_TOOL_NAMES | WRITE_TOOL_NAMES
    first_lines = set()
    for name, tool in tools.items():
        description = tool.description or ""
        assert "When NOT to use:" in description, name
        referenced = set(re.findall(r"\bdeputy_[a-z_]+", description)) - {name}
        assert referenced, f"{name} names no sibling tool to use instead"
        assert referenced <= set(tools), (name, referenced - set(tools))
        assert len(description) <= 1200, (name, len(description))
        first_lines.add(description.splitlines()[0])
    assert len(first_lines) == len(tools)  # no two tools open with the same sentence


async def test_access_level_is_explicit_where_it_matters(deputy_env: Any) -> None:
    tools = await _tools()
    for name in _MANAGER_TOOLS:
        assert "manager or administrator" in (tools[name].description or ""), name
        assert ((tools[name].annotations and tools[name].annotations.title) or "").endswith(
            "(manager)"
        ), name
    for name in _SELF_SERVICE_TOOLS:
        assert "any Deputy access level" in (tools[name].description or ""), name
