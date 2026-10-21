"""Exercise local-time confirmations and identity over the MCP protocol."""

import json
from typing import Any

import pytest
import respx
from fastmcp import Client

from deputy_mcp.server import create_server

from . import tool_text, wire_read_api, wire_write_api


@pytest.mark.usefixtures("config")
async def test_whoami_without_timezone(deputy_api: respx.MockRouter, make_whoami: Any) -> None:
    wire_read_api(
        deputy_api,
        whoami=make_whoami(),
        company={"Id": 1},
        employees=[],
        operational_units=[],
        rosters=[],
        timesheets=[],
    )
    async with Client(create_server()) as client:
        md = tool_text(await client.call_tool("deputy_whoami", {}))
        js = tool_text(await client.call_tool("deputy_whoami", {"response_format": "json"}))
    assert "Timezone: not exposed by Deputy (shift times use each shift's local time)" in md
    assert json.loads(js)["timezone"] is None


@pytest.mark.usefixtures("config")
async def test_write_confirmations(
    deputy_api: respx.MockRouter, make_whoami: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEPUTY_ALLOW_WRITES", "true")
    record = {
        "Id": 8001,
        "OperationalUnit": 11,
        "StartTime": 1792659600,
        "EndTime": 1792688400,
        "StartTimeLocalized": "2026-10-22T10:00:00+01:00",
        "EndTimeLocalized": "2026-10-22T18:00:00+01:00",
    }
    wire_write_api(
        deputy_api,
        whoami=make_whoami(InProgressTS=8001),
        company={"Id": 1},
        unavailability=record,
        timesheet_started={**record, "EndTime": None},
        timesheet_ended=record,
        in_progress_timesheet={**record, "IsInProgress": True},
    )
    async with Client(create_server()) as client:
        started = tool_text(await client.call_tool("deputy_clock_in", {"area_id": 11}))
        ended = tool_text(await client.call_tool("deputy_clock_out", {}))
        unavail = tool_text(
            await client.call_tool(
                "deputy_set_unavailability",
                {
                    "start": "2026-10-22T09:00:00Z",
                    "end": "2026-10-22T17:00:00Z",
                },
            )
        )
    assert "2026-10-22 10:00" in started
    assert "2026-10-22 18:00" in ended
    assert "2026-10-22 10:00" in unavail and "2026-10-22 18:00" in unavail
    for text in (started, ended, unavail):
        assert "the workplace's local time (UTC+01:00)" in text
