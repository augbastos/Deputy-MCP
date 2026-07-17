"""Unit tests for the api / iCal / unconfigured mode resolution in :mod:`deputy_mcp.config`.

The config decides how the whole server behaves:

* ``DEPUTY_API_TOKEN`` (+ ``DEPUTY_BASE_URL``) present -> **api** mode (full tool surface);
* only ``DEPUTY_CALENDAR_URL`` present -> **ical** mode (roster-only, no token, no base URL);
* neither -> fail closed naming BOTH paths.

The calendar feed URL carries its own feed token, so it is a SECRET: these tests prove it
is stored as a :class:`~pydantic.SecretStr` and never rendered into ``repr``/``str``.
All values are fictional (see ``tests/conftest.py``).
"""

from __future__ import annotations

import pytest

from deputy_mcp.client.errors import DeputyConfigError
from deputy_mcp.config import DeputyConfig

# Fictional values, kept local to avoid coupling the unit test to the suite fixtures.
INSTALL_BASE = "https://cloud-nine-cafe.eu.deputy.com"
TEST_TOKEN = "test-token-fictional-not-a-secret"
# A fictional feed URL. NOT a real credential; the token segment is a marker string.
FAKE_CAL_URL = "https://cloud-nine-cafe.eu.deputy.com/api/v1/my/ical/FAKE-NOT-A-SECRET.ics"


# -- api mode ----------------------------------------------------------------


def test_mode_is_api_when_token_set() -> None:
    cfg = DeputyConfig.from_env({"DEPUTY_API_TOKEN": TEST_TOKEN, "DEPUTY_BASE_URL": INSTALL_BASE})
    assert cfg.mode == "api"
    assert cfg.token() == TEST_TOKEN
    assert cfg.base_url == INSTALL_BASE
    assert cfg.api_url == f"{INSTALL_BASE}/api/v1"


def test_mode_is_api_when_both_credentials_set() -> None:
    """A token is primary: with BOTH a token and a feed URL, the full API surface wins."""
    cfg = DeputyConfig.from_env(
        {
            "DEPUTY_API_TOKEN": TEST_TOKEN,
            "DEPUTY_BASE_URL": INSTALL_BASE,
            "DEPUTY_CALENDAR_URL": FAKE_CAL_URL,
        }
    )
    assert cfg.mode == "api"
    # The feed URL is still captured (fallback source) but api mode is authoritative.
    assert cfg.calendar_url_value() == FAKE_CAL_URL


# -- ical mode ---------------------------------------------------------------


def test_mode_is_ical_when_only_calendar_url_set() -> None:
    cfg = DeputyConfig.from_env({"DEPUTY_CALENDAR_URL": FAKE_CAL_URL})
    assert cfg.mode == "ical"
    assert cfg.calendar_url_value() == FAKE_CAL_URL


def test_base_url_not_required_in_ical_mode() -> None:
    """A calendar URL alone is a valid config; base_url stays None and api_url fails clearly."""
    cfg = DeputyConfig.from_env({"DEPUTY_CALENDAR_URL": FAKE_CAL_URL})
    assert cfg.base_url is None
    # The API accessors are unavailable in iCal mode and say so, without leaking anything.
    with pytest.raises(DeputyConfigError) as api_url_exc:
        _ = cfg.api_url
    assert "iCal mode" in str(api_url_exc.value)
    with pytest.raises(DeputyConfigError):
        cfg.token()


def test_ical_mode_ignores_missing_base_url_even_when_writes_requested() -> None:
    """No base URL is needed in iCal mode; other knobs still parse."""
    cfg = DeputyConfig.from_env(
        {"DEPUTY_CALENDAR_URL": FAKE_CAL_URL, "DEPUTY_CACHE_TTL": "0", "DEPUTY_TIMEOUT": "12.5"}
    )
    assert cfg.mode == "ical"
    assert cfg.base_url is None
    assert cfg.cache_ttl == 0
    assert cfg.timeout == 12.5


# -- fail closed (unconfigured) ----------------------------------------------


def test_unconfigured_fails_closed_naming_both_paths() -> None:
    with pytest.raises(DeputyConfigError) as exc:
        DeputyConfig.from_env({})
    message = str(exc.value)
    # Fail-closed message must name BOTH legitimate credential paths.
    assert "DEPUTY_API_TOKEN" in message
    assert "DEPUTY_CALENDAR_URL" in message


def test_blank_both_credentials_fails_closed() -> None:
    with pytest.raises(DeputyConfigError) as exc:
        DeputyConfig.from_env({"DEPUTY_API_TOKEN": "  ", "DEPUTY_CALENDAR_URL": "   "})
    message = str(exc.value)
    assert "DEPUTY_API_TOKEN" in message
    assert "DEPUTY_CALENDAR_URL" in message


def test_direct_construction_without_any_credential_fails_closed() -> None:
    """Defence in depth: constructing the model directly must also refuse an empty config."""
    with pytest.raises(ValueError, match="DEPUTY_CALENDAR_URL"):
        DeputyConfig()


# -- calendar_url is a secret ------------------------------------------------


def test_calendar_url_redacted_in_repr_and_str() -> None:
    cfg = DeputyConfig.from_env({"DEPUTY_CALENDAR_URL": FAKE_CAL_URL})
    # The tokenised feed URL must never surface in a repr/str (only the accessor reveals it).
    assert FAKE_CAL_URL not in repr(cfg)
    assert FAKE_CAL_URL not in str(cfg)
    assert "FAKE-NOT-A-SECRET" not in repr(cfg)
    assert "FAKE-NOT-A-SECRET" not in str(cfg)
    assert cfg.calendar_url_value() == FAKE_CAL_URL


def test_calendar_url_missing_accessor_is_actionable_in_api_mode() -> None:
    """In api mode there is no feed URL; the accessor fails with guidance, not a secret."""
    cfg = DeputyConfig.from_env({"DEPUTY_API_TOKEN": TEST_TOKEN, "DEPUTY_BASE_URL": INSTALL_BASE})
    with pytest.raises(DeputyConfigError) as exc:
        cfg.calendar_url_value()
    assert "DEPUTY_CALENDAR_URL" in str(exc.value)
