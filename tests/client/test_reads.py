"""Client read-method tests: exact endpoint path + QUERY body per deputy-api-read.md.

Every read method on :class:`~deputy_mcp.client.DeputyClient` is exercised against a
respx-mocked Deputy install. QUERY reads assert the *exact* JSON body the transport
posts (including the ``max``/``start`` page window that
:func:`~deputy_mcp.client.query.query_all` appends), so a drift in the DSL wiring is
caught rather than silently accepted. All data is fictional (see ``tests/conftest.py``).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
import respx

from deputy_mcp.client import DeputyClient, DeputyConfig, DeputyNotFoundError
from deputy_mcp.client import reads as reads_module

PayloadFactory = Callable[..., dict[str, Any]]


@pytest.fixture
async def client(config: DeputyConfig) -> AsyncIterator[DeputyClient]:
    """A writes-disabled client for the fictional install (reads ignore the gate)."""
    instance = DeputyClient(config)
    try:
        yield instance
    finally:
        await instance.aclose()


def _posted_body(route: respx.Route) -> Any:
    """Return the JSON body of the most recent request captured by ``route``."""
    return json.loads(route.calls.last.request.content)


# -- whoami ------------------------------------------------------------------


async def test_whoami_hits_whoami_endpoint(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_whoami: PayloadFactory,
) -> None:
    route = deputy_api.get("/resource/Account/WhoAmI").mock(
        return_value=httpx.Response(200, json=make_whoami())
    )
    who = await client.whoami()
    assert route.called
    # Schema is install-dependent, so the employee id lives in the model extras.
    assert (who.model_extra or {})["EmployeeId"] == 101


async def test_whoami_falls_back_to_me_on_404(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_whoami: PayloadFactory,
) -> None:
    whoami_route = deputy_api.get("/resource/Account/WhoAmI").mock(return_value=httpx.Response(404))
    me_route = deputy_api.get("/me").mock(return_value=httpx.Response(200, json=make_whoami()))
    who = await client.whoami()
    assert whoami_route.called
    assert me_route.called
    assert (who.model_extra or {})["EmployeeId"] == 101


async def test_whoami_unwraps_single_element_list(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_whoami: PayloadFactory,
) -> None:
    deputy_api.get("/resource/Account/WhoAmI").mock(
        return_value=httpx.Response(200, json=[make_whoami()])
    )
    who = await client.whoami()
    assert (who.model_extra or {})["EmployeeId"] == 101


# -- /my/* range-filtered reads ----------------------------------------------


async def test_get_my_roster_filters_by_date_range(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_roster: PayloadFactory,
) -> None:
    payload = [
        make_roster(Id=1, Date="2020-12-31"),  # before window
        make_roster(Id=2, Date="2021-01-03"),  # inside window
        make_roster(Id=3, Date="2021-01-10"),  # after window
    ]
    route = deputy_api.get("/my/roster").mock(return_value=httpx.Response(200, json=payload))
    result = await client.get_my_roster(date(2021, 1, 1), date(2021, 1, 7))
    assert route.called
    assert [r.Id for r in result] == [2]


async def test_get_my_timesheets_filters_by_date_range(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_timesheet: PayloadFactory,
) -> None:
    payload = [
        make_timesheet(Id=1, Date="2020-12-31"),
        make_timesheet(Id=2, Date="2021-01-04"),
        make_timesheet(Id=3, Date="2021-02-01"),
    ]
    route = deputy_api.get("/my/timesheets").mock(return_value=httpx.Response(200, json=payload))
    result = await client.get_my_timesheets(date(2021, 1, 1), date(2021, 1, 31))
    assert route.called
    assert [t.Id for t in result] == [2]


# -- Roster/QUERY reads ------------------------------------------------------


async def test_get_team_roster_query_body(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_roster: PayloadFactory,
) -> None:
    route = deputy_api.post("/resource/Roster/QUERY").mock(
        return_value=httpx.Response(200, json=[make_roster()])
    )
    result = await client.get_team_roster(date(2021, 1, 1), date(2021, 1, 7))
    assert [r.Id for r in result] == [9001]
    assert _posted_body(route) == {
        "search": {
            "s1": {"field": "Date", "type": "ge", "data": "2021-01-01"},
            "s2": {"field": "Date", "type": "le", "data": "2021-01-07"},
        },
        "sort": {"StartTime": "asc"},
        "join": ["EmployeeObject"],
        "max": 500,
        "start": 0,
    }


async def test_get_team_roster_with_area_adds_filter(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_roster: PayloadFactory,
) -> None:
    route = deputy_api.post("/resource/Roster/QUERY").mock(
        return_value=httpx.Response(200, json=[make_roster()])
    )
    await client.get_team_roster(date(2021, 1, 1), date(2021, 1, 7), opunit_id=11)
    body = _posted_body(route)
    assert body["search"]["s3"] == {
        "field": "OperationalUnit",
        "type": "eq",
        "data": 11,
    }


async def test_search_shifts_composed_filters(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_roster: PayloadFactory,
) -> None:
    route = deputy_api.post("/resource/Roster/QUERY").mock(
        return_value=httpx.Response(200, json=[make_roster()])
    )
    await client.search_shifts(
        employee_id=101,
        opunit_id=11,
        start=date(2021, 1, 1),
        end=date(2021, 1, 31),
        limit=50,
    )
    assert _posted_body(route) == {
        "search": {
            "s1": {"field": "Employee", "type": "eq", "data": 101},
            "s2": {"field": "OperationalUnit", "type": "eq", "data": 11},
            "s3": {"field": "Date", "type": "ge", "data": "2021-01-01"},
            "s4": {"field": "Date", "type": "le", "data": "2021-01-31"},
        },
        "sort": {"StartTime": "asc"},
        "join": ["EmployeeObject"],
        "max": 50,
        "start": 0,
    }


async def test_search_shifts_open_only(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_roster: PayloadFactory,
) -> None:
    route = deputy_api.post("/resource/Roster/QUERY").mock(
        return_value=httpx.Response(200, json=[make_roster(Open=True, Employee=0)])
    )
    # open_only wins over an employee filter: open shifts have no assignee.
    await client.search_shifts(employee_id=101, open_only=True)
    body = _posted_body(route)
    assert body["search"] == {"s1": {"field": "Open", "type": "eq", "data": True}}


# -- who_is_working (two independent signals) --------------------------------


async def test_who_is_working_queries_both_signals(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_roster: PayloadFactory,
    make_timesheet: PayloadFactory,
) -> None:
    ts_route = deputy_api.post("/resource/Timesheet/QUERY").mock(
        return_value=httpx.Response(
            200, json=[make_timesheet(Id=7001, EndTime=None, IsInProgress=True)]
        )
    )
    roster_route = deputy_api.post("/resource/Roster/QUERY").mock(
        return_value=httpx.Response(200, json=[make_roster(Id=9001)])
    )
    at = datetime(2021, 1, 1, 4, 0, 0, tzinfo=UTC)
    now = str(int(at.timestamp()))
    result = await client.who_is_working(at=at)

    assert result["at"] == at.isoformat()
    assert [t.Id for t in result["clocked_in"]] == [7001]
    assert [r.Id for r in result["rostered_now"]] == [9001]

    assert _posted_body(ts_route) == {
        "search": {"s1": {"field": "IsInProgress", "type": "eq", "data": True}},
        "join": ["EmployeeObject"],
        "max": 500,
        "start": 0,
    }
    assert _posted_body(roster_route) == {
        "search": {
            "s1": {"field": "StartTime", "type": "le", "data": now},
            "s2": {"field": "EndTime", "type": "gt", "data": now},
            "s3": {"field": "Open", "type": "ne", "data": True},
        },
        "join": ["EmployeeObject"],
        "max": 500,
        "start": 0,
    }


# -- Employee reads ----------------------------------------------------------


async def test_get_employees_with_search(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    sample_employees: list[dict[str, Any]],
) -> None:
    route = deputy_api.post("/resource/Employee/QUERY").mock(
        return_value=httpx.Response(200, json=[sample_employees[0]])
    )
    await client.get_employees(search="Alex")
    assert _posted_body(route) == {
        "search": {
            "s1": {"field": "DisplayName", "type": "lk", "data": "%Alex%"},
            "s2": {"field": "Active", "type": "eq", "data": True},
        },
        "sort": {"DisplayName": "asc"},
        "max": 500,
        "start": 0,
    }


async def test_get_employees_active_only_default(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    sample_employees: list[dict[str, Any]],
) -> None:
    route = deputy_api.post("/resource/Employee/QUERY").mock(
        return_value=httpx.Response(200, json=sample_employees[:2])
    )
    await client.get_employees()
    assert _posted_body(route) == {
        "search": {"s1": {"field": "Active", "type": "eq", "data": True}},
        "sort": {"DisplayName": "asc"},
        "max": 500,
        "start": 0,
    }


async def test_get_employees_include_inactive(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    sample_employees: list[dict[str, Any]],
) -> None:
    route = deputy_api.post("/resource/Employee/QUERY").mock(
        return_value=httpx.Response(200, json=sample_employees)
    )
    result = await client.get_employees(active_only=False)
    assert _posted_body(route) == {
        "sort": {"DisplayName": "asc"},
        "max": 500,
        "start": 0,
    }
    assert [e.Id for e in result] == [101, 102, 103]


async def test_get_employee_by_id(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_employee: PayloadFactory,
) -> None:
    route = deputy_api.get("/resource/Employee/101").mock(
        return_value=httpx.Response(200, json=make_employee())
    )
    employee = await client.get_employee(101)
    assert route.called
    assert employee.DisplayName == "Alex Rivera"


# -- OperationalUnit + Company reads -----------------------------------------


async def test_get_operational_units_query_body(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_operational_unit: PayloadFactory,
) -> None:
    route = deputy_api.post("/resource/OperationalUnit/QUERY").mock(
        return_value=httpx.Response(200, json=[make_operational_unit()])
    )
    result = await client.get_operational_units()
    assert [u.Id for u in result] == [11]
    assert _posted_body(route) == {
        "sort": {"OperationalUnitName": "asc"},
        "max": 500,
        "start": 0,
    }


async def test_get_company_returns_first_record(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_company: PayloadFactory,
) -> None:
    route = deputy_api.post("/resource/Company/QUERY").mock(
        return_value=httpx.Response(
            200, json=[make_company(), make_company(Id=2, CompanyName="Second")]
        )
    )
    company = await client.get_company()
    assert _posted_body(route) == {"sort": {"Id": "asc"}, "max": 500, "start": 0}
    assert company.Id == 1


async def test_get_company_raises_when_empty(
    client: DeputyClient, deputy_api: respx.MockRouter
) -> None:
    deputy_api.post("/resource/Company/QUERY").mock(return_value=httpx.Response(200, json=[]))
    with pytest.raises(DeputyNotFoundError):
        await client.get_company()


# -- next_shift --------------------------------------------------------------


async def test_next_shift_for_employee(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_roster: PayloadFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pin "now" so the StartTime>now filter is deterministic.
    monkeypatch.setattr(reads_module, "_now_unix", lambda _at: "1700000000")
    route = deputy_api.post("/resource/Roster/QUERY").mock(
        return_value=httpx.Response(200, json=[make_roster(Id=9500)])
    )
    result = await client.next_shift(employee_id=101)
    assert result is not None
    assert result.Id == 9500
    assert _posted_body(route) == {
        "search": {
            "s1": {"field": "StartTime", "type": "gt", "data": "1700000000"},
            "s2": {"field": "Employee", "type": "eq", "data": 101},
        },
        "sort": {"StartTime": "asc"},
        "join": ["EmployeeObject"],
        "max": 1,
        "start": 0,
    }


async def test_next_shift_defaults_to_self_via_whoami(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_roster: PayloadFactory,
    make_whoami: PayloadFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reads_module, "_now_unix", lambda _at: "1700000000")
    whoami_route = deputy_api.get("/resource/Account/WhoAmI").mock(
        return_value=httpx.Response(200, json=make_whoami())
    )
    roster_route = deputy_api.post("/resource/Roster/QUERY").mock(
        return_value=httpx.Response(200, json=[make_roster(Id=9600)])
    )
    result = await client.next_shift()
    assert whoami_route.called
    assert result is not None
    # own id (101) resolved from whoami feeds the Employee filter.
    assert _posted_body(roster_route)["search"]["s2"] == {
        "field": "Employee",
        "type": "eq",
        "data": 101,
    }


async def test_next_shift_none_when_no_future_shift(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reads_module, "_now_unix", lambda _at: "1700000000")
    deputy_api.post("/resource/Roster/QUERY").mock(return_value=httpx.Response(200, json=[]))
    assert await client.next_shift(employee_id=101) is None
