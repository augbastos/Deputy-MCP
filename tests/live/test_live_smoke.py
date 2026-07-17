"""Live read-only smoke tests against a real Deputy instance.

Excluded from the default run (see ``addopts`` in pyproject). Run explicitly with:

    uv run pytest -m live -v

Credentials come from the environment or a ``.env`` file (``DEPUTY_ENV_FILE`` or a
``.env`` in the working directory). Without them every test here SKIPS. All calls
are strictly read-only; assertions are structural so no personal data ends up in
test output. These tests also probe the gaps the Deputy docs leave open (see
ROADMAP): the Employee join/assoc name, ``/my/*`` response envelopes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any

import pytest

from deputy_mcp.client import DeputyClient
from deputy_mcp.client.errors import DeputyConfigError, DeputyError
from deputy_mcp.client.models import Employee, Roster, Timesheet
from deputy_mcp.client.reads import EMPLOYEE_JOIN
from deputy_mcp.config import DeputyConfig

pytestmark = pytest.mark.live


@pytest.fixture
async def client() -> AsyncIterator[DeputyClient]:
    try:
        config = DeputyConfig.from_env()
    except DeputyConfigError:
        pytest.skip("No live Deputy credentials (set DEPUTY_API_TOKEN/DEPUTY_BASE_URL or .env)")
    live = DeputyClient(config)
    try:
        yield live
    finally:
        await live.aclose()


async def test_whoami_authenticates(client: DeputyClient) -> None:
    me = await client.whoami()
    assert me.model_extra is not None
    assert await client.own_employee_id() > 0


async def test_company_has_timezone(client: DeputyClient) -> None:
    company = await client.get_company()
    tz = getattr(company, "Timezone", None) or company.model_extra.get("Timezone")
    assert tz, "Company record should expose a Timezone for local rendering"


async def test_areas_listed(client: DeputyClient) -> None:
    areas = await client.get_operational_units()
    assert isinstance(areas, list)
    assert all(getattr(area, "Id", None) is not None for area in areas)


async def test_my_roster_this_week(client: DeputyClient) -> None:
    today = date.today()
    rosters = await client.get_my_roster(today, today + timedelta(days=7))
    assert isinstance(rosters, list)
    assert all(isinstance(roster, Roster) for roster in rosters)


async def test_team_roster_today(client: DeputyClient) -> None:
    today = date.today()
    rosters = await client.get_team_roster(today, today)
    assert isinstance(rosters, list)


async def test_who_is_working_shape(client: DeputyClient) -> None:
    snapshot = await client.who_is_working()
    assert set(snapshot) >= {"at", "clocked_in", "rostered_now"}
    assert isinstance(snapshot["clocked_in"], list)
    assert isinstance(snapshot["rostered_now"], list)


async def test_next_shift(client: DeputyClient) -> None:
    nxt = await client.next_shift()
    assert nxt is None or isinstance(nxt, Roster)


async def test_my_timesheets_last_week(client: DeputyClient) -> None:
    today = date.today()
    sheets = await client.get_my_timesheets(today - timedelta(days=7), today)
    assert isinstance(sheets, list)
    assert all(isinstance(sheet, Timesheet) for sheet in sheets)


async def test_search_shifts_paginates(client: DeputyClient) -> None:
    today = date.today()
    shifts = await client.search_shifts(start=today, end=today + timedelta(days=7), limit=3)
    assert isinstance(shifts, list)
    assert len(shifts) <= 3


async def test_employee_search_returns_models(client: DeputyClient) -> None:
    employees = await client.get_employees()
    assert all(isinstance(employee, Employee) for employee in employees)


async def test_gap_probe_employee_join_name(client: DeputyClient) -> None:
    """Docs gap: the Employee join/assoc name is documented-but-unconfirmed.

    POST /resource/Roster/INFO lists the real association names; EMPLOYEE_JOIN
    must be one of them or every joined read silently loses employee names.
    """
    info: Any = await client._http.request(
        "POST", "/resource/Roster/INFO", cacheable=True, idempotent=True
    )
    assert isinstance(info, dict)
    joins = info.get("joins") or info.get("assocs") or {}
    names = set(joins)
    assert EMPLOYEE_JOIN in names, (
        f"EMPLOYEE_JOIN '{EMPLOYEE_JOIN}' not among Roster associations {sorted(names)!r}; "
        "update EMPLOYEE_JOIN in src/deputy_mcp/client/reads.py"
    )


async def test_gap_probe_my_roster_envelope(client: DeputyClient) -> None:
    """Docs gap: /my/* response envelopes are undocumented; record the real shape."""
    raw: Any = await client._http.request(
        "GET", "/my/roster", cacheable=False, idempotent=True
    )
    assert isinstance(raw, list | dict), f"/my/roster returned unexpected type {type(raw).__name__}"


async def test_errors_are_actionable(client: DeputyClient) -> None:
    with pytest.raises(DeputyError):
        await client.get_employee(999_999_999)
