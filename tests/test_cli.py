"""Tests for the deputy-mcp CLI.

The read subcommands are smoke-tested against a fake :class:`DeputyClient` swapped in
by monkeypatch, so no network or real config is needed. They assert the human and
``--json`` renderings, that a :class:`DeputyError` becomes a single stderr line with
exit code 1, and that ``serve`` launches the server on stdio without polluting stdout
(which belongs to the MCP protocol).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from deputy_mcp import cli
from deputy_mcp.client.errors import DeputyAuthError, DeputyError
from deputy_mcp.client.models import (
    Company,
    Employee,
    OperationalUnit,
    Roster,
    Timesheet,
    WhoAmI,
)

PayloadFactory = Any


# --------------------------------------------------------------------------- #
# Fake client (no network)
# --------------------------------------------------------------------------- #
class FakeClient:
    """A stand-in for ``DeputyClient`` returning canned models (or raising)."""

    def __init__(self, **values: Any) -> None:
        self._values = values
        self.calls: dict[str, Any] = {}

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def _value(self, key: str) -> Any:
        value = self._values[key]
        if isinstance(value, Exception):
            raise value
        return value

    async def whoami(self) -> Any:
        return self._value("whoami")

    async def get_company(self) -> Any:
        return self._value("company")

    async def get_my_roster(self, start: Any, end: Any) -> Any:
        return self._value("rosters")

    async def get_team_roster(self, start: Any, end: Any, area: Any) -> Any:
        self.calls["team_area"] = area
        return self._value("rosters")

    async def get_my_timesheets(self, start: Any, end: Any) -> Any:
        return self._value("timesheets")

    async def who_is_working(self) -> Any:
        return self._value("who")

    async def get_operational_units(self) -> Any:
        return self._value("areas")

    async def next_shift(self, employee_id: int | None) -> Any:
        self.calls["next_employee"] = employee_id
        return self._value("next")

    async def get_employees(self, search: str | None = None) -> Any:
        self.calls["search"] = search
        return self._value("employees")


def _install(monkeypatch: pytest.MonkeyPatch, fake: FakeClient) -> None:
    """Point ``cli.DeputyClient.from_env`` at ``fake`` (no real client built)."""

    class _Shim:
        @classmethod
        def from_env(cls) -> FakeClient:
            return fake

    monkeypatch.setattr(cli, "DeputyClient", _Shim)


@pytest.fixture
def models(
    make_whoami: PayloadFactory,
    make_company: PayloadFactory,
    make_roster: PayloadFactory,
    make_timesheet: PayloadFactory,
    make_operational_unit: PayloadFactory,
    sample_employees: list[dict[str, Any]],
) -> dict[str, Any]:
    """Canned model objects the fake client can return."""
    return {
        "whoami": WhoAmI.model_validate(make_whoami()),
        "company": Company.model_validate(make_company()),
        "roster": Roster.model_validate(make_roster()),
        "timesheet": Timesheet.model_validate(make_timesheet()),
        "area": OperationalUnit.model_validate(make_operational_unit()),
        "employees": [Employee.model_validate(e) for e in sample_employees],
    }


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #
def test_parser_exposes_all_subcommands() -> None:
    parser = cli.build_parser()
    for command in ("serve", "whoami", "roster", "timesheets", "who", "areas", "next"):
        args = parser.parse_args([command])
        assert args.command == command


def test_roster_team_and_area_flags() -> None:
    args = cli.build_parser().parse_args(["roster", "--team", "--area", "11"])
    assert args.team is True
    assert args.area == 11


def test_invalid_date_arg_rejected() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["roster", "--start", "nope"])


# --------------------------------------------------------------------------- #
# Read subcommands (human output)
# --------------------------------------------------------------------------- #
def test_whoami_human(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    models: dict[str, Any],
) -> None:
    _install(monkeypatch, FakeClient(whoami=models["whoami"], company=models["company"]))
    assert cli.main(["whoami"]) == 0
    out = capsys.readouterr().out
    assert "Alex Rivera" in out
    assert "Cloud Nine Cafe" in out


def test_roster_mine_human(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    models: dict[str, Any],
) -> None:
    _install(monkeypatch, FakeClient(rosters=[models["roster"]]))
    assert cli.main(["roster"]) == 0
    out = capsys.readouterr().out
    assert "My roster" in out
    assert "Employee #101" in out


def test_roster_team_passes_area(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    models: dict[str, Any],
) -> None:
    fake = FakeClient(rosters=[models["roster"]])
    _install(monkeypatch, fake)
    assert cli.main(["roster", "--team", "--area", "11"]) == 0
    assert fake.calls["team_area"] == 11
    assert "Team roster" in capsys.readouterr().out


def test_timesheets_human(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    models: dict[str, Any],
) -> None:
    _install(monkeypatch, FakeClient(timesheets=[models["timesheet"]]))
    assert cli.main(["timesheets"]) == 0
    out = capsys.readouterr().out
    assert "My timesheets" in out
    assert "completed" in out


def test_who_human(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    models: dict[str, Any],
) -> None:
    who = {
        "at": "2026-07-20T12:00:00+00:00",
        "clocked_in": [models["timesheet"]],
        "rostered_now": [models["roster"]],
    }
    _install(monkeypatch, FakeClient(who=who))
    assert cli.main(["who"]) == 0
    out = capsys.readouterr().out
    assert "Clocked in (1)" in out
    assert "Rostered now (1)" in out


def test_areas_human(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    models: dict[str, Any],
) -> None:
    _install(monkeypatch, FakeClient(areas=[models["area"]]))
    assert cli.main(["areas"]) == 0
    out = capsys.readouterr().out
    assert "Front of House" in out
    assert "#11" in out


def test_next_by_name_resolves_employee(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    models: dict[str, Any],
) -> None:
    fake = FakeClient(employees=models["employees"], next=models["roster"])
    _install(monkeypatch, fake)
    assert cli.main(["next", "--employee", "Alex"]) == 0
    # Alex Rivera resolves to id 101 before next_shift is called.
    assert fake.calls["next_employee"] == 101
    assert "Next shift" in capsys.readouterr().out


def test_next_none_scheduled(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install(monkeypatch, FakeClient(next=None))
    assert cli.main(["next"]) == 0
    assert "No upcoming shift found" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# JSON output
# --------------------------------------------------------------------------- #
def test_roster_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    models: dict[str, Any],
) -> None:
    _install(monkeypatch, FakeClient(rosters=[models["roster"]]))
    assert cli.main(["roster", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert isinstance(parsed, list)
    assert parsed[0]["Id"] == 9001


def test_areas_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    models: dict[str, Any],
) -> None:
    _install(monkeypatch, FakeClient(areas=[models["area"]]))
    assert cli.main(["areas", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed[0]["OperationalUnitName"] == "Front of House"


# --------------------------------------------------------------------------- #
# Errors and serve mode
# --------------------------------------------------------------------------- #
def test_error_goes_to_stderr_exit_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install(monkeypatch, FakeClient(whoami=DeputyAuthError()))
    assert cli.main(["whoami"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "deputy-mcp error:" in captured.err
    assert "Traceback" not in captured.err


def test_serve_runs_server_without_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import deputy_mcp.server as server_pkg

    recorded: dict[str, Any] = {}

    class _FakeServer:
        def run(self, transport: str) -> None:
            recorded["transport"] = transport

    monkeypatch.setattr(server_pkg, "create_server", lambda: _FakeServer())

    assert cli.main([]) == 0  # no subcommand defaults to serve
    assert recorded["transport"] == "stdio"
    captured = capsys.readouterr()
    # stdout is reserved for the MCP protocol; the banner goes to stderr.
    assert captured.out == ""
    assert "starting MCP server" in captured.err


def test_serve_config_error_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import deputy_mcp.server as server_pkg

    def _boom() -> None:
        raise DeputyError("bad config", hint="set DEPUTY_BASE_URL")

    monkeypatch.setattr(server_pkg, "create_server", _boom)
    assert cli.main(["serve"]) == 1
    assert "deputy-mcp error:" in capsys.readouterr().err
