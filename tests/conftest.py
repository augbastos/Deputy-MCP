"""Shared test fixtures for the deputy-mcp suite.

Everything here is FICTIONAL: the install ``cloud-nine-cafe.eu.deputy.com`` and the
people Alex Rivera, Sam O'Brien and Jo Murphy do not exist, and ``TEST_TOKEN`` is a
placeholder string, not a real credential. The payload factories reproduce the field
shapes documented in ``deputy-api-read.md`` so client/server tests can mock Deputy's
Resource API responses without hand-writing dicts.

Fixtures provided for the whole suite:

* ``deputy_env`` / ``config`` / ``make_config`` — environment + parsed configuration.
* ``deputy_api`` — a respx router bound to the fictional install's ``/api/v1`` base.
* ``make_employee`` / ``make_roster`` / ``make_timesheet`` / ``make_operational_unit``
  / ``make_company`` / ``make_contact`` / ``make_whoami`` — payload factories.
* ``sample_employees`` — Alex, Sam and Jo as a list of employee payloads.
* ``load_fixture`` / ``fixtures_dir`` — read the JSON files under ``tests/fixtures``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import respx
from pydantic import SecretStr

# NOTE: importing ``deputy_mcp.config`` before ``deputy_mcp.client`` triggers a circular
# import in the source (config -> client.errors -> client/__init__ -> http -> config).
# That is a pre-existing structural bug in the client/config modules, not in the tests;
# importing the client package first here breaks the cycle so the whole suite can load.
# See the W4 test report deviations — the real fix belongs in the source.
import deputy_mcp.client  # noqa: F401  (side-effect import: ordering guard)
from deputy_mcp.config import DeputyConfig

# -- fictional install constants (shared by every test) ----------------------

#: Fictional install subdomain used across the suite.
TEST_INSTALL: str = "cloud-nine-cafe"
#: Fictional install origin (no ``/api/v1`` suffix).
INSTALL_BASE: str = f"https://{TEST_INSTALL}.eu.deputy.com"
#: Fictional versioned API base the transport talks to.
API_BASE: str = f"{INSTALL_BASE}/api/v1"
#: Placeholder token — NOT a real secret, never printed anywhere but tests.
TEST_TOKEN: str = "test-token-fictional-not-a-secret"

#: Default DEPUTY_* environment used by the ``deputy_env`` fixture.
_DEFAULT_ENV: dict[str, str] = {
    "DEPUTY_API_TOKEN": TEST_TOKEN,
    "DEPUTY_BASE_URL": INSTALL_BASE,
    "DEPUTY_ALLOW_WRITES": "false",
    "DEPUTY_CACHE_TTL": "30",
    "DEPUTY_TIMEOUT": "30",
    "DEPUTY_MAX_RETRIES": "3",
}

#: Every DEPUTY_* variable the config reads (cleared before each env fixture).
_DEPUTY_VARS: tuple[str, ...] = (
    "DEPUTY_API_TOKEN",
    "DEPUTY_BASE_URL",
    "DEPUTY_ALLOW_WRITES",
    "DEPUTY_ALLOW_CUSTOM_HOST",
    "DEPUTY_CALENDAR_URL",
    "DEPUTY_OAUTH_CLIENT_ID",
    "DEPUTY_OAUTH_CLIENT_SECRET",
    "DEPUTY_OAUTH_REDIRECT_PORT",
    "DEPUTY_CACHE_TTL",
    "DEPUTY_TIMEOUT",
    "DEPUTY_MAX_RETRIES",
)

#: Env-file / token-store pointers. The autouse isolation fixture pins these at hermetic
#: temp paths and, unlike the credential vars above, they are NOT cleared by the per-test
#: env fixtures — so an ambient repo ``.env`` or a real ``~/.deputy-mcp`` never leaks in.
_ISOLATION_VARS: tuple[str, ...] = ("DEPUTY_ENV_FILE", "DEPUTY_TOKEN_STORE")


@pytest.fixture(autouse=True)
def _isolate_environment(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Make every (non-live) test hermetic against the real environment.

    Without this, a developer who has run ``deputy-mcp login`` (a real token store at
    ``~/.deputy-mcp/token.json``) or who keeps a ``.env`` in the repo — the documented
    setup — would see the suite resolve a real OAuth/api config instead of the test's
    intended one. This clears every ``DEPUTY_*`` variable, points the token store at an
    isolated temp path, and neutralises any ambient ``.env`` by pointing
    ``DEPUTY_ENV_FILE`` at an empty file. Tests marked ``live`` opt out — they are meant
    to read the real credentials.
    """
    if request.node.get_closest_marker("live") is not None:
        return
    for name in (*_DEPUTY_VARS, *_ISOLATION_VARS):
        monkeypatch.delenv(name, raising=False)
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DEPUTY_ENV_FILE", str(empty_env))
    monkeypatch.setenv("DEPUTY_TOKEN_STORE", str(tmp_path / "token.json"))


# -- constant fixtures (import-free access for other test modules) -----------


@pytest.fixture
def install_base() -> str:
    """The fictional install origin, e.g. ``https://cloud-nine-cafe.eu.deputy.com``."""
    return INSTALL_BASE


@pytest.fixture
def api_base() -> str:
    """The fictional versioned API base the transport talks to."""
    return API_BASE


@pytest.fixture
def test_token() -> str:
    """The placeholder token string (never a real credential)."""
    return TEST_TOKEN


# -- configuration fixtures --------------------------------------------------


@pytest.fixture
def deputy_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Monkeypatch a clean, valid set of DEPUTY_* variables.

    Any DEPUTY_* value inherited from the real environment is removed first so the
    tests are deterministic. Returns the applied mapping for convenience.
    """
    for name in _DEPUTY_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in _DEFAULT_ENV.items():
        monkeypatch.setenv(name, value)
    return dict(_DEFAULT_ENV)


@pytest.fixture
def config(deputy_env: dict[str, str]) -> DeputyConfig:
    """A parsed, writes-disabled configuration for the fictional install."""
    return DeputyConfig.from_env()


@pytest.fixture
def make_config() -> Callable[..., DeputyConfig]:
    """Factory building a :class:`DeputyConfig` directly, with overrides.

    Bypasses the environment so a test can pin a single knob (e.g. ``max_retries=0``
    for error-mapping tests or ``cache_ttl=0`` to disable the cache).
    """

    def _make(**overrides: Any) -> DeputyConfig:
        params: dict[str, Any] = {
            "api_token": SecretStr(TEST_TOKEN),
            "base_url": INSTALL_BASE,
            "allow_writes": False,
            "cache_ttl": 30,
            "timeout": 30.0,
            "max_retries": 3,
        }
        params.update(overrides)
        return DeputyConfig(**params)

    return _make


# -- HTTP mocking ------------------------------------------------------------


@pytest.fixture
def deputy_api() -> Iterator[respx.MockRouter]:
    """A respx router bound to the fictional install's ``/api/v1`` base.

    Yields the router so tests can register routes with paths relative to
    ``API_BASE`` (e.g. ``deputy_api.post("/resource/Roster/QUERY")``). Assertions on
    "all routes called" are left to the test (``assert_all_called=False``).
    """
    with respx.mock(base_url=API_BASE, assert_all_called=False) as router:
        yield router


# -- payload factories -------------------------------------------------------


@pytest.fixture
def make_employee() -> Callable[..., dict[str, Any]]:
    """Factory for an ``Employee`` payload (defaults to Alex Rivera)."""

    def _make(**overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "Id": 101,
            "Company": 1,
            "FirstName": "Alex",
            "LastName": "Rivera",
            "DisplayName": "Alex Rivera",
            "OtherName": None,
            "Contact": 501,
            "User": 201,
            "Active": True,
            "StartDate": "2023-04-01",
            "TerminationDate": None,
            "Role": 45,
            "Created": "2023-04-01T09:00:00",
            "Modified": "2024-01-15T12:00:00",
        }
        base.update(overrides)
        return base

    return _make


@pytest.fixture
def make_roster() -> Callable[..., dict[str, Any]]:
    """Factory for a ``Roster`` (shift) payload.

    Defaults to an 8-hour published shift for employee 101 on 2021-01-01
    (``StartTime`` 1609459200 = 2021-01-01T00:00:00Z, ``EndTime`` +8h).
    """

    def _make(**overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "Id": 9001,
            "StartTime": 1609459200,
            "EndTime": 1609488000,
            "Date": "2021-01-01",
            "Employee": 101,
            "OperationalUnit": 11,
            "MatchedByTimesheet": None,
            "Comment": "Morning shift",
            "Warning": None,
            "TotalTime": 8.0,
            "Cost": 120.0,
            "Published": True,
            "Open": False,
            "ApprovalRequired": False,
            "ConfirmStatus": 0,
            "SwapStatus": 0,
            "Creator": 1,
            "Created": "2020-12-01T10:00:00",
            "Modified": "2020-12-20T10:00:00",
        }
        base.update(overrides)
        return base

    return _make


@pytest.fixture
def make_timesheet() -> Callable[..., dict[str, Any]]:
    """Factory for a ``Timesheet`` payload.

    Defaults to a completed timesheet; pass ``EndTime=None, IsInProgress=True`` for a
    clocked-in (in-progress) record.
    """

    def _make(**overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "Id": 7001,
            "Employee": 101,
            "Roster": 9001,
            "OperationalUnit": 11,
            "Date": "2021-01-01",
            "StartTime": 1609459200,
            "EndTime": 1609488000,
            "TotalTime": 8.0,
            "Cost": 120.0,
            "IsInProgress": False,
            "RealTime": True,
            "TimeApproved": True,
            "PayRuleApproved": False,
            "Discarded": False,
        }
        base.update(overrides)
        return base

    return _make


@pytest.fixture
def make_operational_unit() -> Callable[..., dict[str, Any]]:
    """Factory for an ``OperationalUnit`` (Area) payload."""

    def _make(**overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "Id": 11,
            "Company": 1,
            "ParentOperationalUnit": None,
            "OperationalUnitName": "Front of House",
            "Active": True,
            "RosterActive": True,
            "ShowOnRoster": True,
            "Address": 301,
            "Contact": 401,
        }
        base.update(overrides)
        return base

    return _make


@pytest.fixture
def make_company() -> Callable[..., dict[str, Any]]:
    """Factory for a ``Company`` (Location) payload.

    Carries a ``Timezone`` extra field (kept via ``extra='allow'``) so formatting tests
    have a timezone source, mirroring the install's local business timezone.
    """

    def _make(**overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "Id": 1,
            "ParentCompany": None,
            "CompanyName": "Cloud Nine Cafe",
            "TradingName": "Cloud Nine Cafe",
            "Address": 301,
            "Timezone": "Europe/Dublin",
        }
        base.update(overrides)
        return base

    return _make


@pytest.fixture
def make_contact() -> Callable[..., dict[str, Any]]:
    """Factory for a ``Contact`` payload (fictional phone/email)."""

    def _make(**overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "Id": 501,
            "Phone1": "+353-1-555-0101",
            "Phone2": None,
            "Email1": "alex.rivera@example.com",
            "Email2": None,
            "PrimaryPhone": 1,
            "PrimaryEmail": 1,
        }
        base.update(overrides)
        return base

    return _make


@pytest.fixture
def make_whoami() -> Callable[..., dict[str, Any]]:
    """Factory for a ``WhoAmI`` payload (install-dependent, mostly extra fields)."""

    def _make(**overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "UserId": 201,
            "EmployeeId": 101,
            "Name": "Alex Rivera",
            "Company": 1,
            "CompanyName": "Cloud Nine Cafe",
            "Permissions": {},
        }
        base.update(overrides)
        return base

    return _make


@pytest.fixture
def sample_employees(
    make_employee: Callable[..., dict[str, Any]],
) -> list[dict[str, Any]]:
    """Alex Rivera, Sam O'Brien and Jo Murphy as employee payloads."""
    return [
        make_employee(),
        make_employee(
            Id=102,
            FirstName="Sam",
            LastName="O'Brien",
            DisplayName="Sam O'Brien",
            Contact=502,
            User=202,
        ),
        make_employee(
            Id=103,
            FirstName="Jo",
            LastName="Murphy",
            DisplayName="Jo Murphy",
            Contact=503,
            User=203,
            Active=False,
            TerminationDate="2024-06-30",
        ),
    ]


# -- JSON fixture loading ----------------------------------------------------


@pytest.fixture
def fixtures_dir() -> Path:
    """Absolute path to the ``tests/fixtures`` directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def load_fixture(fixtures_dir: Path) -> Callable[[str], Any]:
    """Return a loader that parses a JSON file from ``tests/fixtures``.

    Usage: ``load_fixture("rosters.json")`` -> parsed JSON (list or dict).
    """

    def _load(name: str) -> Any:
        return json.loads((fixtures_dir / name).read_text(encoding="utf-8"))

    return _load
