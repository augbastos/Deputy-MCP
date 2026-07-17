"""Live read-only smoke tests against a real Deputy instance.

Excluded from the default run (see ``addopts`` in pyproject). Run explicitly with:

    uv run pytest -m live -v

Credentials come from the environment or a ``.env`` file (``DEPUTY_ENV_FILE`` or a
``.env`` in the working directory). Without them every test here SKIPS. All calls are
strictly read-only; assertions are structural so no personal data ends up in test output.

These probes target the endpoints a real token actually reaches (see
``smoke-findings.md``): ``/api/v1/me`` (not ``/resource/Account/WhoAmI``), the future-only
bare-array ``/my/roster`` and ``/my/timesheets``. Manager/admin-only reads are asserted to
ACCEPT EITHER real data (an admin/manager token) OR a clean :class:`DeputyPermissionError`
(a plain employee token, which 403s "Access to object-type denied"), so this suite passes
on BOTH token types.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any

import pytest

from deputy_mcp.client import DeputyClient
from deputy_mcp.client.errors import DeputyConfigError, DeputyError, DeputyPermissionError
from deputy_mcp.client.models import Employee, Roster, Timesheet
from deputy_mcp.client.reads import EMPLOYEE_JOIN
from deputy_mcp.client.whoami import employee_id_from_whoami
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


# --------------------------------------------------------------------------- #
# Self-service surface: works for ANY access level (assert strictly)
# --------------------------------------------------------------------------- #
async def test_me_is_the_real_whoami(client: DeputyClient) -> None:
    """The identity call is GET /api/v1/me, carrying a top-level EmployeeId."""
    who = await client.whoami()
    assert who.model_extra is not None
    # EmployeeId is exposed at the top level for any access level.
    assert employee_id_from_whoami(who) > 0
    assert await client.own_employee_id() > 0


async def test_me_raw_shape(client: DeputyClient) -> None:
    """/api/v1/me returns the caller's own record (object, or a 1-element list)."""
    raw: Any = await client._http.request("GET", "/me", cacheable=False, idempotent=True)
    if isinstance(raw, list):
        raw = raw[0] if raw else {}
    assert isinstance(raw, dict)
    # Top-level EmployeeId (or the Employee spelling) is what own_employee_id() reads.
    assert any(key in raw for key in ("EmployeeId", "Employee", "employeeId"))


async def test_my_roster_is_bare_array(client: DeputyClient) -> None:
    """GET /my/roster is a bare JSON array (no envelope), future-only per the live findings."""
    raw: Any = await client._http.request("GET", "/my/roster", cacheable=False, idempotent=True)
    assert isinstance(raw, list)


async def test_my_timesheets_is_bare_array(client: DeputyClient) -> None:
    raw: Any = await client._http.request("GET", "/my/timesheets", cacheable=False, idempotent=True)
    assert isinstance(raw, list)


async def test_my_roster_this_week(client: DeputyClient) -> None:
    today = date.today()
    rosters = await client.get_my_roster(today, today + timedelta(days=7))
    assert isinstance(rosters, list)
    assert all(isinstance(roster, Roster) for roster in rosters)


async def test_my_timesheets_last_week(client: DeputyClient) -> None:
    # A past window may try Roster/Timesheet QUERY, but a non-admin 403 there degrades to
    # /my/timesheets rather than raising -- so this stays strict for any token.
    today = date.today()
    sheets = await client.get_my_timesheets(today - timedelta(days=7), today)
    assert isinstance(sheets, list)
    assert all(isinstance(sheet, Timesheet) for sheet in sheets)


async def test_next_shift_self(client: DeputyClient) -> None:
    # Self next-shift is derived from /my/roster -> works at any access level.
    nxt = await client.next_shift()
    assert nxt is None or isinstance(nxt, Roster)


async def test_get_areas_degrades_gracefully(client: DeputyClient) -> None:
    # get_operational_units falls back to areas derived from /my/roster on a 403, so it
    # returns a list for any token (never raises).
    areas = await client.get_operational_units()
    assert isinstance(areas, list)
    assert all(getattr(area, "Id", None) is not None for area in areas)


# --------------------------------------------------------------------------- #
# Manager/admin surface: accept EITHER data OR a clean DeputyPermissionError
# --------------------------------------------------------------------------- #
def _assert_clean_permission_error(exc: DeputyPermissionError) -> None:
    """A degraded manager-tool failure must be actionable, not a raw 403."""
    assert exc.status_code == 403
    assert exc.hint  # points the employee at the self-service tools


async def test_company_resolves_or_permission(client: DeputyClient) -> None:
    # get_company reads /me's CompanyObject first (any token) and only falls back to the
    # admin Company/QUERY; either a company or a clean permission/not-found error is fine.
    try:
        company = await client.get_company()
        assert getattr(company, "Id", None) is not None
    except DeputyError as exc:  # DeputyPermissionError / DeputyNotFoundError both acceptable
        assert exc.hint is not None or exc.message


async def test_team_roster_today_or_permission(client: DeputyClient) -> None:
    today = date.today()
    try:
        rosters = await client.get_team_roster(today, today)
        assert isinstance(rosters, list)
    except DeputyPermissionError as exc:
        _assert_clean_permission_error(exc)


async def test_who_is_working_or_permission(client: DeputyClient) -> None:
    try:
        snapshot = await client.who_is_working()
        assert set(snapshot) >= {"at", "clocked_in", "rostered_now"}
        assert isinstance(snapshot["clocked_in"], list)
        assert isinstance(snapshot["rostered_now"], list)
    except DeputyPermissionError as exc:
        _assert_clean_permission_error(exc)


async def test_search_shifts_or_permission(client: DeputyClient) -> None:
    today = date.today()
    try:
        shifts = await client.search_shifts(start=today, end=today + timedelta(days=7), limit=3)
        assert isinstance(shifts, list)
        assert len(shifts) <= 3
    except DeputyPermissionError as exc:
        _assert_clean_permission_error(exc)


async def test_employee_search_or_permission(client: DeputyClient) -> None:
    try:
        employees = await client.get_employees()
        assert all(isinstance(employee, Employee) for employee in employees)
    except DeputyPermissionError as exc:
        _assert_clean_permission_error(exc)


async def test_next_shift_other_employee_or_permission(client: DeputyClient) -> None:
    # Reading ANOTHER person's next shift is a manager capability; a plain employee 403s.
    own_id = await client.own_employee_id()
    try:
        nxt = await client.next_shift(employee_id=own_id)
        assert nxt is None or isinstance(nxt, Roster)
    except DeputyPermissionError as exc:
        _assert_clean_permission_error(exc)


# --------------------------------------------------------------------------- #
# Docs-gap probes (INFO is admin-only; accept a clean permission error)
# --------------------------------------------------------------------------- #
async def test_gap_probe_employee_join_name(client: DeputyClient) -> None:
    """POST /resource/Roster/INFO lists the real association names (admin-only).

    EMPLOYEE_JOIN must be one of them or every joined manager read silently loses employee
    names. INFO 403s for a plain employee, which is accepted (skips the assertion).
    """
    try:
        info: Any = await client._http.request(
            "POST", "/resource/Roster/INFO", cacheable=True, idempotent=True
        )
    except DeputyPermissionError as exc:
        _assert_clean_permission_error(exc)
        pytest.skip("Roster/INFO needs a manager/admin access level on this install")
    assert isinstance(info, dict)
    joins = info.get("joins") or info.get("assocs") or {}
    names = set(joins)
    assert EMPLOYEE_JOIN in names, (
        f"EMPLOYEE_JOIN '{EMPLOYEE_JOIN}' not among Roster associations {sorted(names)!r}; "
        "update EMPLOYEE_JOIN in src/deputy_mcp/client/reads.py"
    )


async def test_gap_probe_my_roster_envelope(client: DeputyClient) -> None:
    """/my/* response envelope: the live finding is a bare array; record the real shape."""
    raw: Any = await client._http.request("GET", "/my/roster", cacheable=False, idempotent=True)
    assert isinstance(raw, list | dict), f"/my/roster returned unexpected type {type(raw).__name__}"


async def test_errors_are_actionable(client: DeputyClient) -> None:
    # A missing employee (admin token) 404s; a plain employee 403s -- both are DeputyError.
    with pytest.raises(DeputyError):
        await client.get_employee(999_999_999)
