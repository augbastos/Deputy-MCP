"""Unit tests for :mod:`deputy_mcp.config`.

Covers env parsing, URL normalization, boolean/number coercion, token redaction and
the fail-closed behavior on missing/invalid input.
"""

from __future__ import annotations

import pytest

from deputy_mcp.client.errors import DeputyConfigError
from deputy_mcp.config import DeputyConfig

# Fictional values mirroring tests/conftest.py (kept local to avoid cross-package
# imports that would depend on tests being an importable package).
INSTALL_BASE = "https://cloud-nine-cafe.eu.deputy.com"
TEST_TOKEN = "test-token-fictional-not-a-secret"


def _base_env(**overrides: str) -> dict[str, str]:
    """A minimal valid env with optional overrides (empty string = present-but-blank)."""
    env = {"DEPUTY_API_TOKEN": TEST_TOKEN, "DEPUTY_BASE_URL": INSTALL_BASE}
    env.update(overrides)
    return env


# -- happy path --------------------------------------------------------------


def test_from_env_parses_defaults() -> None:
    cfg = DeputyConfig.from_env(_base_env())
    assert cfg.base_url == INSTALL_BASE
    assert cfg.token() == TEST_TOKEN
    assert cfg.allow_writes is False
    assert cfg.cache_ttl == 30
    assert cfg.timeout == 30.0
    assert cfg.max_retries == 3


def test_api_url_appends_version() -> None:
    cfg = DeputyConfig.from_env(_base_env())
    assert cfg.api_url == f"{INSTALL_BASE}/api/v1"


def test_from_env_reads_os_environ_via_fixture(config: DeputyConfig) -> None:
    # The ``config`` fixture monkeypatches os.environ; from_env() with no arg reads it.
    assert config.base_url == INSTALL_BASE
    assert config.token() == TEST_TOKEN


# -- URL normalization -------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "https://cloud-nine-cafe.eu.deputy.com",
        "https://cloud-nine-cafe.eu.deputy.com/",
        "https://cloud-nine-cafe.eu.deputy.com/api/v1",
        "https://cloud-nine-cafe.eu.deputy.com/api/v1/",
        "https://cloud-nine-cafe.eu.deputy.com/api/v2",
        "cloud-nine-cafe.eu.deputy.com",
        "HTTPS://cloud-nine-cafe.eu.deputy.com/API/V1/",
    ],
)
def test_base_url_normalization(raw: str) -> None:
    cfg = DeputyConfig.from_env(_base_env(DEPUTY_BASE_URL=raw))
    # All variants collapse to the bare origin (scheme + host, no suffix/slash). The
    # ``/api/v1`` (and ``/API/V1``) suffix is stripped case-insensitively; the scheme
    # case is preserved verbatim, so compare case-insensitively.
    assert cfg.base_url.rstrip("/").lower().endswith("cloud-nine-cafe.eu.deputy.com")
    assert "/api/" not in cfg.base_url.lower()
    assert cfg.base_url.lower().startswith("https://")


def test_missing_scheme_defaults_to_https() -> None:
    cfg = DeputyConfig.from_env(_base_env(DEPUTY_BASE_URL="cloud-nine-cafe.eu.deputy.com"))
    assert cfg.base_url == "https://cloud-nine-cafe.eu.deputy.com"


def test_non_deputy_host_fails_closed_by_default() -> None:
    # A host outside *.deputy.com is rejected unless explicitly allowed, and the
    # error names the escape-hatch variable for legitimate custom domains.
    with pytest.raises(DeputyConfigError) as exc:
        DeputyConfig.from_env(_base_env(DEPUTY_BASE_URL="https://schedule.acme.example"))
    assert "DEPUTY_ALLOW_CUSTOM_HOST" in str(exc.value)


def test_non_deputy_host_allowed_with_flag_warns_but_succeeds() -> None:
    with pytest.warns(UserWarning, match="DEPUTY_ALLOW_CUSTOM_HOST"):
        cfg = DeputyConfig.from_env(
            _base_env(
                DEPUTY_BASE_URL="https://schedule.acme.example",
                DEPUTY_ALLOW_CUSTOM_HOST="true",
            )
        )
    assert cfg.base_url == "https://schedule.acme.example"
    assert cfg.allow_custom_host is True


# -- boolean / number coercion ----------------------------------------------


@pytest.mark.parametrize("value", ["true", "TRUE", "True", "1", "yes", "YES"])
def test_allow_writes_truthy(value: str) -> None:
    cfg = DeputyConfig.from_env(_base_env(DEPUTY_ALLOW_WRITES=value))
    assert cfg.allow_writes is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "", "maybe"])
def test_allow_writes_falsey(value: str) -> None:
    cfg = DeputyConfig.from_env(_base_env(DEPUTY_ALLOW_WRITES=value))
    assert cfg.allow_writes is False


def test_numeric_overrides_parse() -> None:
    cfg = DeputyConfig.from_env(
        _base_env(
            DEPUTY_CACHE_TTL="0",
            DEPUTY_TIMEOUT="12.5",
            DEPUTY_MAX_RETRIES="5",
        )
    )
    assert cfg.cache_ttl == 0
    assert cfg.timeout == 12.5
    assert cfg.max_retries == 5


def test_blank_numeric_falls_back_to_default() -> None:
    cfg = DeputyConfig.from_env(_base_env(DEPUTY_CACHE_TTL="  ", DEPUTY_MAX_RETRIES=""))
    assert cfg.cache_ttl == 30
    assert cfg.max_retries == 3


# -- fail closed -------------------------------------------------------------


def test_missing_token_fails_closed() -> None:
    env = {"DEPUTY_BASE_URL": INSTALL_BASE}
    with pytest.raises(DeputyConfigError) as exc:
        DeputyConfig.from_env(env)
    assert "DEPUTY_API_TOKEN" in str(exc.value)


def test_blank_token_fails_closed() -> None:
    with pytest.raises(DeputyConfigError) as exc:
        DeputyConfig.from_env(_base_env(DEPUTY_API_TOKEN="   "))
    assert "DEPUTY_API_TOKEN" in str(exc.value)


def test_missing_base_url_fails_closed() -> None:
    env = {"DEPUTY_API_TOKEN": TEST_TOKEN}
    with pytest.raises(DeputyConfigError) as exc:
        DeputyConfig.from_env(env)
    assert "DEPUTY_BASE_URL" in str(exc.value)


def test_invalid_int_env_fails_closed() -> None:
    with pytest.raises(DeputyConfigError) as exc:
        DeputyConfig.from_env(_base_env(DEPUTY_CACHE_TTL="thirty"))
    assert "DEPUTY_CACHE_TTL" in str(exc.value)


def test_invalid_float_env_fails_closed() -> None:
    with pytest.raises(DeputyConfigError) as exc:
        DeputyConfig.from_env(_base_env(DEPUTY_TIMEOUT="soon"))
    assert "DEPUTY_TIMEOUT" in str(exc.value)


def test_negative_max_retries_rejected() -> None:
    # ge=0 constraint -> ValidationError wrapped as DeputyConfigError.
    with pytest.raises(DeputyConfigError):
        DeputyConfig.from_env(_base_env(DEPUTY_MAX_RETRIES="-1"))


def test_zero_timeout_rejected() -> None:
    # gt=0 constraint on timeout.
    with pytest.raises(DeputyConfigError):
        DeputyConfig.from_env(_base_env(DEPUTY_TIMEOUT="0"))


# -- token redaction ---------------------------------------------------------


def test_token_absent_from_repr_and_str() -> None:
    cfg = DeputyConfig.from_env(_base_env())
    assert TEST_TOKEN not in repr(cfg)
    assert TEST_TOKEN not in str(cfg)
    # Only the explicit accessor reveals it.
    assert cfg.token() == TEST_TOKEN


def test_config_error_message_never_leaks_token() -> None:
    # A validation failure must not echo the secret in its hint.
    with pytest.raises(DeputyConfigError) as exc:
        DeputyConfig.from_env(_base_env(DEPUTY_MAX_RETRIES="-5"))
    assert TEST_TOKEN not in str(exc.value)
