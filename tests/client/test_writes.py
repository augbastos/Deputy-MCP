"""Client write-method tests: the write gate, exact documented payloads, cache
invalidation and adversarial permission/edge cases per deputy-api-write.md.

Coverage spans (1) every write raising :class:`DeputyWritesDisabledError` before any
HTTP call when ``DEPUTY_ALLOW_WRITES`` is false, (2) the paths that do NOT resolve the
caller's employee id (``clock_out`` with an explicit timesheet id, input validation),
and (3) the employee-id-resolving paths (claim/swap/unavail/clock-in), each asserting
its documented payload, cache invalidation, and 403 -> :class:`DeputyPermissionError`
mapping. The caller's own ``Employee.Id`` is resolved via ``own_employee_id()`` (the
whoami-derived cache on :class:`DeputyClient`).
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from deputy_mcp.client import (
    DeputyClient,
    DeputyConfig,
    DeputyError,
    DeputyPermissionError,
    DeputyWritesDisabledError,
)
from deputy_mcp.client.writes import _parse_recurrence

PayloadFactory = Callable[..., dict[str, Any]]
ConfigFactory = Callable[..., DeputyConfig]


@pytest.fixture
async def write_client(make_config: ConfigFactory) -> AsyncIterator[DeputyClient]:
    """A writes-ENABLED client for the fictional install."""
    instance = DeputyClient(make_config(allow_writes=True))
    try:
        yield instance
    finally:
        await instance.aclose()


@pytest.fixture
async def disabled_client(make_config: ConfigFactory) -> AsyncIterator[DeputyClient]:
    """A writes-DISABLED client (the safe default)."""
    instance = DeputyClient(make_config(allow_writes=False))
    try:
        yield instance
    finally:
        await instance.aclose()


def _posted_body(route: respx.Route) -> Any:
    """Return the JSON body of the most recent request captured by ``route``."""
    return json.loads(route.calls.last.request.content)


def _mock_whoami(deputy_api: respx.MockRouter, make_whoami: PayloadFactory) -> None:
    """Wire the WhoAmI probe used to resolve the caller's own employee id."""
    deputy_api.get("/resource/Account/WhoAmI").mock(
        return_value=httpx.Response(200, json=make_whoami())
    )


# -- the write gate (green: raises before any HTTP call) ---------------------


async def test_claim_open_shift_gated_when_writes_disabled(
    disabled_client: DeputyClient, deputy_api: respx.MockRouter
) -> None:
    with pytest.raises(DeputyWritesDisabledError) as exc:
        await disabled_client.claim_open_shift(9001)
    assert exc.value.hint is not None  # tells the user how to enable writes
    assert not deputy_api.calls  # gate short-circuits before touching the network


async def test_request_shift_swap_gated_when_writes_disabled(
    disabled_client: DeputyClient, deputy_api: respx.MockRouter
) -> None:
    with pytest.raises(DeputyWritesDisabledError):
        await disabled_client.request_shift_swap(9001, note="please")
    assert not deputy_api.calls


async def test_set_unavailability_gated_when_writes_disabled(
    disabled_client: DeputyClient, deputy_api: respx.MockRouter
) -> None:
    with pytest.raises(DeputyWritesDisabledError):
        await disabled_client.set_unavailability(
            datetime(2025, 1, 6, 9, 0, tzinfo=UTC),
            datetime(2025, 1, 6, 17, 0, tzinfo=UTC),
        )
    assert not deputy_api.calls


async def test_clock_in_gated_when_writes_disabled(
    disabled_client: DeputyClient, deputy_api: respx.MockRouter
) -> None:
    with pytest.raises(DeputyWritesDisabledError):
        await disabled_client.clock_in(opunit_id=11)
    assert not deputy_api.calls


async def test_clock_out_gated_when_writes_disabled(
    disabled_client: DeputyClient, deputy_api: respx.MockRouter
) -> None:
    with pytest.raises(DeputyWritesDisabledError):
        await disabled_client.clock_out(timesheet_id=7001)
    assert not deputy_api.calls


# -- clock_out with an explicit id (green: no employee-id resolution) ---------


async def test_clock_out_explicit_payload(
    write_client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_timesheet: PayloadFactory,
) -> None:
    route = deputy_api.post("/supervise/timesheet/end").mock(
        return_value=httpx.Response(200, json=make_timesheet(Id=7001, IsInProgress=False))
    )
    result = await write_client.clock_out(timesheet_id=7001, mealbreak_minutes=30)
    assert result.Id == 7001
    assert _posted_body(route) == {"intTimesheetId": 7001, "intMealbreakMinute": 30}


async def test_clock_out_explicit_without_mealbreak(
    write_client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_timesheet: PayloadFactory,
) -> None:
    route = deputy_api.post("/supervise/timesheet/end").mock(
        return_value=httpx.Response(200, json=make_timesheet(Id=7001))
    )
    await write_client.clock_out(timesheet_id=7001)
    assert _posted_body(route) == {"intTimesheetId": 7001}


async def test_clock_out_negative_mealbreak_rejected(
    write_client: DeputyClient, deputy_api: respx.MockRouter
) -> None:
    with pytest.raises(DeputyError) as exc:
        await write_client.clock_out(timesheet_id=7001, mealbreak_minutes=-5)
    assert "negative" in str(exc.value).lower()
    assert not deputy_api.calls  # validated before any request


async def test_clock_out_invalidates_cache(
    write_client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_timesheet: PayloadFactory,
) -> None:
    deputy_api.post("/supervise/timesheet/end").mock(
        return_value=httpx.Response(200, json=make_timesheet(Id=7001))
    )
    # Prime the read cache with a sentinel entry; a successful write must clear it.
    write_client._http._cache["sentinel"] = (time.monotonic(), {"stale": True})
    await write_client.clock_out(timesheet_id=7001)
    assert write_client._http._cache == {}


async def test_clock_out_403_maps_to_permission_error(
    write_client: DeputyClient, deputy_api: respx.MockRouter
) -> None:
    deputy_api.post("/supervise/timesheet/end").mock(
        return_value=httpx.Response(403, text="You are not allowed to edit this timesheet")
    )
    with pytest.raises(DeputyPermissionError) as exc:
        await write_client.clock_out(timesheet_id=7001)
    assert exc.value.status_code == 403
    assert exc.value.hint is not None  # actionable guidance, not a bare traceback


# -- set_unavailability input validation (green: before employee-id lookup) --


async def test_set_unavailability_end_before_start_rejected(
    write_client: DeputyClient, deputy_api: respx.MockRouter
) -> None:
    start = datetime(2025, 1, 6, 17, 0, tzinfo=UTC)
    end = datetime(2025, 1, 6, 9, 0, tzinfo=UTC)
    with pytest.raises(DeputyError) as exc:
        await write_client.set_unavailability(start, end)
    assert "after start" in str(exc.value).lower()
    assert not deputy_api.calls


# -- recurrence payload builder (green: tested directly) ---------------------


def test_parse_recurrence_weekly() -> None:
    assert _parse_recurrence("FREQ=WEEKLY;INTERVAL=1;BYDAY=MO") == {
        "FREQ": "WEEKLY",
        "INTERVAL": 1,
        "BYDAY": "MO",
    }


def test_parse_recurrence_monthly_bymonthday() -> None:
    assert _parse_recurrence("FREQ=MONTHLY;BYMONTHDAY=6") == {
        "FREQ": "MONTHLY",
        "INTERVAL": 1,
        "BYMONTHDAY": 6,
    }


@pytest.mark.parametrize(
    "repeat",
    [
        "INTERVAL=1",  # no FREQ
        "FREQ=YEARLY",  # unsupported FREQ
        "FREQ=WEEKLY;INTERVAL=zero",  # non-integer interval
        "FREQ=WEEKLY;BYDAY=XX",  # invalid day token
        "FREQ=MONTHLY;BYMONTHDAY=40",  # out-of-range day
        "no-equals-token",  # malformed token
    ],
)
def test_parse_recurrence_malformed_rejected(repeat: str) -> None:
    with pytest.raises(DeputyError):
        _parse_recurrence(repeat)


# -- write payloads that resolve the caller's own employee id ----------------
# Each mocks WhoAmI (own_employee_id -> 101) and asserts the documented body.


async def test_claim_open_shift_payload(
    write_client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_whoami: PayloadFactory,
) -> None:
    _mock_whoami(deputy_api, make_whoami)
    route = deputy_api.post("/supervise/roster").mock(return_value=httpx.Response(200))
    await write_client.claim_open_shift(9001)
    assert _posted_body(route) == {
        "intRosterId": 9001,
        "intRosterEmployee": 101,
        "blnOpen": 0,
    }


async def test_claim_open_shift_403_maps_to_permission_error(
    write_client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_whoami: PayloadFactory,
) -> None:
    _mock_whoami(deputy_api, make_whoami)
    deputy_api.post("/supervise/roster").mock(return_value=httpx.Response(403))
    with pytest.raises(DeputyPermissionError):
        await write_client.claim_open_shift(9001)


async def test_request_shift_swap_payload_with_note(
    write_client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_whoami: PayloadFactory,
) -> None:
    _mock_whoami(deputy_api, make_whoami)
    route = deputy_api.post("/resource/RosterSwap").mock(
        return_value=httpx.Response(
            200,
            json={
                "Id": 55,
                "SourceRoster": 9001,
                "TargetRoster": 0,
                "Employee": 101,
                "Status": 4,
                "RequestMessage": "Cover Saturday",
            },
        )
    )
    swap = await write_client.request_shift_swap(9001, note="Cover Saturday")
    assert swap.Id == 55
    assert _posted_body(route) == {
        "SourceRoster": 9001,
        "TargetRoster": 0,
        "Employee": 101,
        "Status": 4,
        "RequestMessage": "Cover Saturday",
    }


async def test_request_shift_swap_payload_without_note(
    write_client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_whoami: PayloadFactory,
) -> None:
    _mock_whoami(deputy_api, make_whoami)
    route = deputy_api.post("/resource/RosterSwap").mock(
        return_value=httpx.Response(
            200,
            json={"Id": 56, "SourceRoster": 9001, "TargetRoster": 0, "Status": 4},
        )
    )
    await write_client.request_shift_swap(9001)
    assert "RequestMessage" not in _posted_body(route)


async def test_set_unavailability_payload(
    write_client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_whoami: PayloadFactory,
) -> None:
    _mock_whoami(deputy_api, make_whoami)
    route = deputy_api.post("/supervise/unavail").mock(
        return_value=httpx.Response(200, json={"Id": 88, "Type": 1})
    )
    start = datetime(2025, 1, 6, 9, 0, tzinfo=UTC)
    end = datetime(2025, 1, 6, 17, 0, tzinfo=UTC)
    unavail = await write_client.set_unavailability(
        start, end, reason="Holiday", repeat="FREQ=WEEKLY;INTERVAL=1;BYDAY=MO"
    )
    assert unavail.Id == 88
    assert _posted_body(route) == {
        "blnSubmitSuperUnavail": True,
        "intAssignedEmployeeId": 101,
        "start": {"timestamp": str(int(start.timestamp()))},
        "end": {"timestamp": str(int(end.timestamp()))},
        "strComment": "Holiday",
        "recurrence": {"FREQ": "WEEKLY", "INTERVAL": 1, "BYDAY": "MO"},
    }


async def test_clock_in_payload_with_area_and_roster(
    write_client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_whoami: PayloadFactory,
    make_timesheet: PayloadFactory,
) -> None:
    _mock_whoami(deputy_api, make_whoami)
    route = deputy_api.post("/supervise/timesheet/start").mock(
        return_value=httpx.Response(
            200, json=make_timesheet(Id=7100, EndTime=None, IsInProgress=True)
        )
    )
    result = await write_client.clock_in(opunit_id=11, roster_id=9001)
    assert result.Id == 7100
    assert _posted_body(route) == {
        "intEmployeeId": 101,
        "intOpunitId": 11,
        "intRosterId": 9001,
    }


async def test_clock_in_auto_resolves_single_area(
    write_client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_whoami: PayloadFactory,
    make_operational_unit: PayloadFactory,
    make_timesheet: PayloadFactory,
) -> None:
    _mock_whoami(deputy_api, make_whoami)
    deputy_api.post("/resource/OperationalUnit/QUERY").mock(
        return_value=httpx.Response(200, json=[make_operational_unit(Id=11)])
    )
    route = deputy_api.post("/supervise/timesheet/start").mock(
        return_value=httpx.Response(
            200, json=make_timesheet(Id=7101, EndTime=None, IsInProgress=True)
        )
    )
    await write_client.clock_in()
    assert _posted_body(route) == {"intEmployeeId": 101, "intOpunitId": 11}


async def test_clock_in_ambiguous_area_rejected(
    write_client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_whoami: PayloadFactory,
    make_operational_unit: PayloadFactory,
) -> None:
    _mock_whoami(deputy_api, make_whoami)
    deputy_api.post("/resource/OperationalUnit/QUERY").mock(
        return_value=httpx.Response(
            200,
            json=[make_operational_unit(Id=11), make_operational_unit(Id=12)],
        )
    )
    with pytest.raises(DeputyError):
        await write_client.clock_in()


async def test_clock_out_finds_in_progress_timesheet(
    write_client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_whoami: PayloadFactory,
    make_timesheet: PayloadFactory,
) -> None:
    _mock_whoami(deputy_api, make_whoami)
    query_route = deputy_api.post("/resource/Timesheet/QUERY").mock(
        return_value=httpx.Response(
            200, json=[make_timesheet(Id=7200, EndTime=None, IsInProgress=True)]
        )
    )
    end_route = deputy_api.post("/supervise/timesheet/end").mock(
        return_value=httpx.Response(200, json=make_timesheet(Id=7200))
    )
    await write_client.clock_out()
    assert _posted_body(query_route) == {
        "search": {
            "s1": {"field": "Employee", "type": "eq", "data": 101},
            "s2": {"field": "IsInProgress", "type": "eq", "data": True},
        },
        "sort": {"StartTime": "desc"},
        "max": 50,
        "start": 0,
    }
    assert _posted_body(end_route) == {"intTimesheetId": 7200}


async def test_clock_out_no_in_progress_timesheet_errors(
    write_client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_whoami: PayloadFactory,
) -> None:
    _mock_whoami(deputy_api, make_whoami)
    deputy_api.post("/resource/Timesheet/QUERY").mock(return_value=httpx.Response(200, json=[]))
    with pytest.raises(DeputyError) as exc:
        await write_client.clock_out()
    assert "in-progress" in str(exc.value).lower()
