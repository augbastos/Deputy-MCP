"""Client read-method tests against the REAL Deputy behavior (live smoke findings).

Every read method on :class:`~deputy_mcp.client.DeputyClient` is exercised against a
respx-mocked Deputy install. The endpoints mirror what a live employee token actually
reaches (``smoke-findings.md``):

* ``whoami`` -> ``GET /api/v1/me`` (top-level ``EmployeeId``, embedded ``CompanyObject``,
  ``InProgressTS``, ``CalendarURL``); the documented ``/resource/Account/WhoAmI`` is only
  a 404 fallback.
* ``get_my_roster`` / ``get_my_timesheets`` / self ``next_shift`` -> the self-service
  ``GET /api/v1/my/roster`` and ``GET /api/v1/my/timesheets`` (bare JSON arrays), which any
  employee token can read. The admin ``resource/*/QUERY`` path is only used for the past /
  other people, and a 403 there degrades to the ``/my/*`` data instead of surfacing a raw
  permission error.
* The manager/admin tools (team roster, who-is-working, employee lookup, ...) still QUERY
  the Resource API; on a 403 "Access to object-type denied" they raise
  :class:`DeputyPermissionError` carrying actionable guidance toward the self-service tools.

QUERY reads still assert the *exact* JSON body posted so a DSL drift is caught. All data is
fictional (see ``tests/conftest.py``).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
import respx

from deputy_mcp.client import (
    DeputyClient,
    DeputyConfig,
    DeputyNotFoundError,
    DeputyPermissionError,
)
from deputy_mcp.client import reads as reads_module
from deputy_mcp.client.whoami import (
    whoami_calendar_url,
    whoami_company_name,
    whoami_is_clocked_in,
)

PayloadFactory = Callable[..., dict[str, Any]]

#: A fictional, fake iCal feed URL. Not a real credential; the token segment is a marker.
_FAKE_CAL_URL = "https://cloud-nine-cafe.eu.deputy.com/api/v1/my/ical/FAKE-NOT-A-SECRET.ics"


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


def _me_payload(**overrides: Any) -> dict[str, Any]:
    """A realistic ``GET /api/v1/me`` body (top-level EmployeeId + embedded objects).

    Pass ``CompanyObject=None`` to model an install whose ``/me`` omits the embedded
    company (forcing the ``Company/QUERY`` fallback), or ``InProgressTS=0`` for "not
    clocked in".
    """
    base: dict[str, Any] = {
        "UserId": 201,
        "EmployeeId": 101,
        "Name": "Alex Rivera",
        "FirstName": "Alex",
        "LastName": "Rivera",
        "CompanyObject": {
            "Id": 1,
            "CompanyName": "Cloud Nine Cafe",
            "TradingName": "Cloud Nine Cafe",
            "Timezone": "Europe/Dublin",
        },
        "InProgressTS": 7001,
        "CalendarURL": _FAKE_CAL_URL,
        "Permissions": {},
    }
    base.update(overrides)
    if base.get("CompanyObject") is None:
        base.pop("CompanyObject", None)
    return base


_OBJECT_DENIED = {"error": {"code": 403, "message": "Access to object-type denied"}}


# -- whoami: GET /api/v1/me is the real "who am I" ---------------------------


async def test_whoami_hits_me_endpoint(client: DeputyClient, deputy_api: respx.MockRouter) -> None:
    route = deputy_api.get("/me").mock(return_value=httpx.Response(200, json=_me_payload()))
    who = await client.whoami()
    assert route.called
    # Schema is install-dependent, so the values live in the model extras / accessors.
    assert (who.model_extra or {})["EmployeeId"] == 101
    assert whoami_is_clocked_in(who) is True  # InProgressTS is a live timesheet
    assert whoami_calendar_url(who) == _FAKE_CAL_URL
    assert whoami_company_name(who) == "Cloud Nine Cafe"


async def test_own_employee_id_reads_me_employee_id(
    client: DeputyClient, deputy_api: respx.MockRouter
) -> None:
    deputy_api.get("/me").mock(return_value=httpx.Response(200, json=_me_payload()))
    assert await client.own_employee_id() == 101


async def test_whoami_falls_back_to_account_whoami_on_404(
    client: DeputyClient, deputy_api: respx.MockRouter
) -> None:
    # Installs that lack /me 404; the legacy /resource/Account/WhoAmI is the fallback.
    me_route = deputy_api.get("/me").mock(return_value=httpx.Response(404))
    legacy = deputy_api.get("/resource/Account/WhoAmI").mock(
        return_value=httpx.Response(200, json=_me_payload())
    )
    who = await client.whoami()
    assert me_route.called
    assert legacy.called
    assert (who.model_extra or {})["EmployeeId"] == 101


async def test_whoami_unwraps_single_element_list(
    client: DeputyClient, deputy_api: respx.MockRouter
) -> None:
    deputy_api.get("/me").mock(return_value=httpx.Response(200, json=[_me_payload()]))
    who = await client.whoami()
    assert (who.model_extra or {})["EmployeeId"] == 101


# -- get_my_roster: /api/v1/my/roster is the self-service source -------------


async def test_get_my_roster_uses_my_roster_endpoint(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_roster: PayloadFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A current/future window is answered purely from /my/roster (future-only bare array),
    # filtered client-side; the admin Roster/QUERY is never touched.
    monkeypatch.setattr(reads_module, "_utc_today", lambda: date(2021, 1, 1))
    my_route = deputy_api.get("/my/roster").mock(
        return_value=httpx.Response(
            200,
            json=[
                make_roster(Id=1, Date="2021-01-03"),
                make_roster(Id=2, Date="2021-02-15"),  # outside the requested window
            ],
        )
    )
    query = deputy_api.post("/resource/Roster/QUERY").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = await client.get_my_roster(date(2021, 1, 1), date(2021, 1, 7))
    assert [r.Id for r in result] == [1]
    assert my_route.called
    assert not query.called


async def test_get_my_roster_falls_back_to_my_roster_on_query_403(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_roster: PayloadFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A window reaching into the past tries the admin Roster/QUERY; a non-admin employee
    # 403s there, and the "my week" case degrades to /my/roster instead of surfacing 403.
    monkeypatch.setattr(reads_module, "_utc_today", lambda: date(2021, 1, 10))
    deputy_api.get("/me").mock(return_value=httpx.Response(200, json=_me_payload()))
    query = deputy_api.post("/resource/Roster/QUERY").mock(
        return_value=httpx.Response(403, json=_OBJECT_DENIED)
    )
    my_route = deputy_api.get("/my/roster").mock(
        return_value=httpx.Response(200, json=[make_roster(Id=5, Date="2021-01-05")])
    )
    result = await client.get_my_roster(date(2021, 1, 1), date(2021, 1, 31))
    assert [r.Id for r in result] == [5]
    assert query.called  # the admin path was attempted
    assert my_route.called  # then it degraded to the self-service feed


async def test_get_my_roster_past_window_queries_by_own_id(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_roster: PayloadFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # For an admin token the past window resolves via Roster/QUERY scoped to the caller's
    # own id (101, from /me), with the documented Date range + Employee join.
    monkeypatch.setattr(reads_module, "_utc_today", lambda: date(2021, 1, 10))
    deputy_api.get("/me").mock(return_value=httpx.Response(200, json=_me_payload()))
    route = deputy_api.post("/resource/Roster/QUERY").mock(
        return_value=httpx.Response(200, json=[make_roster(Id=9, Date="2021-01-02")])
    )
    result = await client.get_my_roster(date(2021, 1, 1), date(2021, 1, 7))
    assert [r.Id for r in result] == [9]
    assert _posted_body(route) == {
        "search": {
            "s1": {"field": "Employee", "type": "eq", "data": 101},
            "s2": {"field": "Date", "type": "ge", "data": "2021-01-01"},
            "s3": {"field": "Date", "type": "le", "data": "2021-01-07"},
        },
        "sort": {"StartTime": "asc"},
        "join": ["EmployeeObject"],
        "max": 500,
        "start": 0,
    }


# -- get_my_timesheets: /api/v1/my/timesheets primary ------------------------


async def test_get_my_timesheets_uses_my_timesheets_endpoint(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_timesheet: PayloadFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reads_module, "_utc_today", lambda: date(2021, 1, 1))
    my_route = deputy_api.get("/my/timesheets").mock(
        return_value=httpx.Response(
            200,
            json=[
                make_timesheet(Id=2, Date="2021-01-04"),
                make_timesheet(Id=3, Date="2020-12-01"),  # outside the window
            ],
        )
    )
    query = deputy_api.post("/resource/Timesheet/QUERY").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = await client.get_my_timesheets(date(2021, 1, 1), date(2021, 1, 31))
    assert [t.Id for t in result] == [2]
    assert my_route.called
    assert not query.called


async def test_get_my_timesheets_past_empty_falls_back_to_query(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_timesheet: PayloadFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Nothing in /my for a past window -> an admin can pull deeper history via QUERY.
    monkeypatch.setattr(reads_module, "_utc_today", lambda: date(2021, 2, 1))
    deputy_api.get("/my/timesheets").mock(return_value=httpx.Response(200, json=[]))
    deputy_api.get("/me").mock(return_value=httpx.Response(200, json=_me_payload()))
    route = deputy_api.post("/resource/Timesheet/QUERY").mock(
        return_value=httpx.Response(200, json=[make_timesheet(Id=8, Date="2021-01-15")])
    )
    result = await client.get_my_timesheets(date(2021, 1, 1), date(2021, 1, 20))
    assert [t.Id for t in result] == [8]
    assert _posted_body(route) == {
        "search": {
            "s1": {"field": "Employee", "type": "eq", "data": 101},
            "s2": {"field": "Date", "type": "ge", "data": "2021-01-01"},
            "s3": {"field": "Date", "type": "le", "data": "2021-01-20"},
        },
        "sort": {"StartTime": "asc"},
        "join": ["EmployeeObject"],
        "max": 500,
        "start": 0,
    }


async def test_get_my_timesheets_query_403_degrades_to_my_data(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A non-admin 403 on the history QUERY must NOT surface; it degrades to /my (empty).
    monkeypatch.setattr(reads_module, "_utc_today", lambda: date(2021, 2, 1))
    deputy_api.get("/my/timesheets").mock(return_value=httpx.Response(200, json=[]))
    deputy_api.get("/me").mock(return_value=httpx.Response(200, json=_me_payload()))
    deputy_api.post("/resource/Timesheet/QUERY").mock(
        return_value=httpx.Response(403, json=_OBJECT_DENIED)
    )
    assert await client.get_my_timesheets(date(2021, 1, 1), date(2021, 1, 20)) == []


# -- Manager/admin Roster/QUERY reads (exact bodies) -------------------------


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
    assert body["search"]["s3"] == {"field": "OperationalUnit", "type": "eq", "data": 11}


async def test_get_team_roster_403_raises_permission_guidance(
    client: DeputyClient, deputy_api: respx.MockRouter
) -> None:
    # The single most important behavior for shift workers: a manager tool 403 becomes an
    # actionable DeputyPermissionError pointing at the self-service tools, not a raw 403.
    deputy_api.post("/resource/Roster/QUERY").mock(
        return_value=httpx.Response(403, json=_OBJECT_DENIED)
    )
    with pytest.raises(DeputyPermissionError) as exc:
        await client.get_team_roster(date(2021, 1, 1), date(2021, 1, 7))
    message = str(exc.value)
    assert exc.value.status_code == 403
    assert "manager or administrator access level" in message
    assert "get_my_roster" in message  # redirected to a tool that works


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
    await client.search_shifts(employee_id=101, open_only=True)
    body = _posted_body(route)
    assert body["search"] == {"s1": {"field": "Open", "type": "eq", "data": True}}


async def test_search_shifts_403_raises_permission_guidance(
    client: DeputyClient, deputy_api: respx.MockRouter
) -> None:
    deputy_api.post("/resource/Roster/QUERY").mock(
        return_value=httpx.Response(403, json=_OBJECT_DENIED)
    )
    with pytest.raises(DeputyPermissionError):
        await client.search_shifts(employee_id=102)


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
        "search": {
            "s1": {"field": "IsInProgress", "type": "eq", "data": True},
            "s2": {"field": "StartTime", "type": "le", "data": now},
        },
        "join": ["EmployeeObject"],
        "max": 500,
        "start": 0,
    }
    assert _posted_body(roster_route) == {
        "search": {
            "s1": {"field": "StartTime", "type": "le", "data": now},
            "s2": {"field": "EndTime", "type": "gt", "data": now},
            "s3": {"field": "Published", "type": "eq", "data": True},
            "s4": {"field": "Open", "type": "ne", "data": True},
        },
        "join": ["EmployeeObject"],
        "max": 500,
        "start": 0,
    }


async def test_who_is_working_403_raises_permission_guidance(
    client: DeputyClient, deputy_api: respx.MockRouter
) -> None:
    # The clocked-in signal (Timesheet/QUERY) is read first; a non-admin 403s there.
    deputy_api.post("/resource/Timesheet/QUERY").mock(
        return_value=httpx.Response(403, json=_OBJECT_DENIED)
    )
    with pytest.raises(DeputyPermissionError):
        await client.who_is_working()


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


async def test_get_employees_403_raises_permission_guidance(
    client: DeputyClient, deputy_api: respx.MockRouter
) -> None:
    deputy_api.post("/resource/Employee/QUERY").mock(
        return_value=httpx.Response(403, json=_OBJECT_DENIED)
    )
    with pytest.raises(DeputyPermissionError):
        await client.get_employees(search="Alex")


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


async def test_get_employee_403_raises_permission_guidance(
    client: DeputyClient, deputy_api: respx.MockRouter
) -> None:
    deputy_api.get("/resource/Employee/101").mock(
        return_value=httpx.Response(403, json=_OBJECT_DENIED)
    )
    with pytest.raises(DeputyPermissionError):
        await client.get_employee(101)


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


async def test_get_operational_units_403_derives_areas_from_my_roster(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_roster: PayloadFactory,
) -> None:
    # A non-admin cannot QUERY OperationalUnit; the areas the employee actually works are
    # derived from the OperationalUnitObject embedded in /my/roster (no 403 surfaced).
    deputy_api.post("/resource/OperationalUnit/QUERY").mock(
        return_value=httpx.Response(403, json=_OBJECT_DENIED)
    )
    deputy_api.get("/my/roster").mock(
        return_value=httpx.Response(
            200,
            json=[
                make_roster(
                    Id=1,
                    OperationalUnit=11,
                    OperationalUnitObject={"Id": 11, "OperationalUnitName": "Front of House"},
                ),
                make_roster(
                    Id=2,
                    OperationalUnit=12,
                    OperationalUnitObject={"Id": 12, "OperationalUnitName": "Kitchen"},
                ),
            ],
        )
    )
    units = await client.get_operational_units()
    assert {u.OperationalUnitName for u in units} == {"Front of House", "Kitchen"}


async def test_get_company_reads_company_object_from_me(
    client: DeputyClient, deputy_api: respx.MockRouter
) -> None:
    # /api/v1/me embeds CompanyObject, so an employee never needs the admin Company/QUERY.
    deputy_api.get("/me").mock(return_value=httpx.Response(200, json=_me_payload()))
    query = deputy_api.post("/resource/Company/QUERY").mock(
        return_value=httpx.Response(200, json=[])
    )
    company = await client.get_company()
    assert company.Id == 1
    assert company.CompanyName == "Cloud Nine Cafe"
    assert not query.called


async def test_get_company_falls_back_to_query_when_me_lacks_company_object(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_company: PayloadFactory,
) -> None:
    deputy_api.get("/me").mock(
        return_value=httpx.Response(200, json=_me_payload(CompanyObject=None))
    )
    route = deputy_api.post("/resource/Company/QUERY").mock(
        return_value=httpx.Response(
            200, json=[make_company(), make_company(Id=2, CompanyName="Second")]
        )
    )
    company = await client.get_company()
    assert company.Id == 1
    assert _posted_body(route) == {"sort": {"Id": "asc"}, "max": 500, "start": 0}


async def test_get_company_raises_when_no_source(
    client: DeputyClient, deputy_api: respx.MockRouter
) -> None:
    deputy_api.get("/me").mock(
        return_value=httpx.Response(200, json=_me_payload(CompanyObject=None))
    )
    deputy_api.post("/resource/Company/QUERY").mock(return_value=httpx.Response(200, json=[]))
    with pytest.raises(DeputyNotFoundError):
        await client.get_company()


# -- next_shift --------------------------------------------------------------


async def test_next_shift_self_uses_my_roster(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_roster: PayloadFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Self next-shift derives from the self-service /my/roster feed (earliest future start);
    # it needs no employee lookup and never touches the admin Roster/QUERY.
    monkeypatch.setattr(reads_module, "_now_unix", lambda _at: "1700000000")
    my_route = deputy_api.get("/my/roster").mock(
        return_value=httpx.Response(
            200,
            json=[
                make_roster(Id=9600, StartTime=1700000900),  # soonest future
                make_roster(Id=9700, StartTime=1700500000),
                make_roster(Id=9500, StartTime=1600000000),  # already past
            ],
        )
    )
    query = deputy_api.post("/resource/Roster/QUERY").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = await client.next_shift()
    assert result is not None
    assert result.Id == 9600
    assert my_route.called
    assert not query.called


async def test_next_shift_for_other_employee_queries(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    make_roster: PayloadFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reads_module, "_now_unix", lambda _at: "1700000000")
    route = deputy_api.post("/resource/Roster/QUERY").mock(
        return_value=httpx.Response(200, json=[make_roster(Id=9500)])
    )
    result = await client.next_shift(employee_id=102)
    assert result is not None
    assert result.Id == 9500
    assert _posted_body(route) == {
        "search": {
            "s1": {"field": "StartTime", "type": "gt", "data": "1700000000"},
            "s2": {"field": "Employee", "type": "eq", "data": 102},
        },
        "sort": {"StartTime": "asc"},
        "join": ["EmployeeObject"],
        "max": 1,
        "start": 0,
    }


async def test_next_shift_other_403_raises_permission_guidance(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reads_module, "_now_unix", lambda _at: "1700000000")
    deputy_api.post("/resource/Roster/QUERY").mock(
        return_value=httpx.Response(403, json=_OBJECT_DENIED)
    )
    with pytest.raises(DeputyPermissionError):
        await client.next_shift(employee_id=102)


async def test_next_shift_self_none_when_no_future_shift(
    client: DeputyClient,
    deputy_api: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reads_module, "_now_unix", lambda _at: "1700000000")
    deputy_api.get("/my/roster").mock(return_value=httpx.Response(200, json=[]))
    assert await client.next_shift() is None
