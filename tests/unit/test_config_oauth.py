"""Config resolution tests for OAuth mode (:class:`deputy_mcp.config.DeputyConfig`).

OAuth is the third credential path, slotted between static-token and iCal. These
tests pin the precedence (``static > oauth > ical > fail-closed``), that OAuth
resolves to the public ``api`` mode (full tool surface), that OAuth mode needs no
``DEPUTY_BASE_URL`` (the base URL comes from the stored token endpoint), that the
OAuth client secret is redacted, and that the pre-existing static and iCal modes
resolve exactly as before. All identifiers are FICTIONAL.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deputy_mcp.client.errors import DeputyConfigError
from deputy_mcp.config import DeputyConfig
from deputy_mcp.oauth import OAuthTokens, TokenStore

_BASE_URL = "https://cloud-nine-cafe.eu.deputy.com"
_CLIENT_ID = "fake-oauth-client-id"
_CLIENT_SECRET = "fake-oauth-client-secret"  # fictional, not a real secret
_CAL_URL = "https://cloud-nine-cafe.eu.deputy.com/api/v1/my/ical/FAKE-NOT-A-SECRET.ics"


def _absent_store(tmp_path: Path) -> str:
    """A token-store path that definitely does not exist (isolates from any real store)."""
    return str(tmp_path / "token.json")


def _write_store(tmp_path: Path) -> str:
    """Write a valid token store (with a refresh token) and return its path."""
    path = tmp_path / "token.json"
    TokenStore(path).save(
        OAuthTokens(
            access_token="stored-access",
            refresh_token="stored-refresh",
            expires_at=1_900_000_000.0,
            base_url=_BASE_URL,
        )
    )
    return str(path)


# --------------------------------------------------------------------------- #
# auth_kind precedence
# --------------------------------------------------------------------------- #
def test_static_token_wins_over_oauth_and_ical(tmp_path: Path) -> None:
    # All three credential sets present -> the permanent token takes precedence.
    config = DeputyConfig.from_env(
        {
            "DEPUTY_API_TOKEN": "static-token",
            "DEPUTY_BASE_URL": _BASE_URL,
            "DEPUTY_OAUTH_CLIENT_ID": _CLIENT_ID,
            "DEPUTY_OAUTH_CLIENT_SECRET": _CLIENT_SECRET,
            "DEPUTY_CALENDAR_URL": _CAL_URL,
            "DEPUTY_TOKEN_STORE": _write_store(tmp_path),
        }
    )
    assert config.auth_kind == "static"
    assert config.mode == "api"


def test_oauth_wins_over_ical_when_no_static_token(tmp_path: Path) -> None:
    config = DeputyConfig.from_env(
        {
            "DEPUTY_OAUTH_CLIENT_ID": _CLIENT_ID,
            "DEPUTY_OAUTH_CLIENT_SECRET": _CLIENT_SECRET,
            "DEPUTY_CALENDAR_URL": _CAL_URL,
            "DEPUTY_TOKEN_STORE": _absent_store(tmp_path),
        }
    )
    assert config.auth_kind == "oauth"
    assert config.mode == "api"


def test_oauth_resolves_from_client_credentials_alone(tmp_path: Path) -> None:
    # Client id + secret (so `login` can run) is enough to select OAuth even before a
    # token has been minted.
    config = DeputyConfig.from_env(
        {
            "DEPUTY_OAUTH_CLIENT_ID": _CLIENT_ID,
            "DEPUTY_OAUTH_CLIENT_SECRET": _CLIENT_SECRET,
            "DEPUTY_TOKEN_STORE": _absent_store(tmp_path),
        }
    )
    assert config.auth_kind == "oauth"


def test_oauth_resolves_from_stored_token_without_client_creds(tmp_path: Path) -> None:
    # A stored refresh token alone (no client id/secret in env) still selects OAuth: the
    # precedence check reads the store file's presence, never its secret values.
    config = DeputyConfig.from_env({"DEPUTY_TOKEN_STORE": _write_store(tmp_path)})
    assert config.auth_kind == "oauth"
    assert config.mode == "api"


def test_ical_resolves_when_only_calendar_url(tmp_path: Path) -> None:
    config = DeputyConfig.from_env(
        {"DEPUTY_CALENDAR_URL": _CAL_URL, "DEPUTY_TOKEN_STORE": _absent_store(tmp_path)}
    )
    assert config.auth_kind == "ical"
    assert config.mode == "ical"


def test_static_mode_resolves_as_before(tmp_path: Path) -> None:
    config = DeputyConfig.from_env(
        {
            "DEPUTY_API_TOKEN": "static-token",
            "DEPUTY_BASE_URL": _BASE_URL,
            "DEPUTY_TOKEN_STORE": _absent_store(tmp_path),
        }
    )
    assert config.auth_kind == "static"
    assert config.mode == "api"
    assert config.base_url == _BASE_URL


def test_no_credentials_fails_closed_naming_all_three_paths(tmp_path: Path) -> None:
    with pytest.raises(DeputyConfigError) as excinfo:
        DeputyConfig.from_env({"DEPUTY_TOKEN_STORE": _absent_store(tmp_path)})
    message = str(excinfo.value)
    assert "DEPUTY_API_TOKEN" in message
    assert "deputy-mcp login" in message
    assert "DEPUTY_CALENDAR_URL" in message


# --------------------------------------------------------------------------- #
# OAuth mode: base URL comes from the store, not DEPUTY_BASE_URL
# --------------------------------------------------------------------------- #
def test_oauth_mode_does_not_require_base_url(tmp_path: Path) -> None:
    # OAuth mode omits DEPUTY_BASE_URL: the install origin is recovered from the token
    # endpoint at sign-in, so config must build without it (unlike static mode).
    config = DeputyConfig.from_env(
        {
            "DEPUTY_OAUTH_CLIENT_ID": _CLIENT_ID,
            "DEPUTY_OAUTH_CLIENT_SECRET": _CLIENT_SECRET,
            "DEPUTY_TOKEN_STORE": _absent_store(tmp_path),
        }
    )
    assert config.base_url is None


def test_oauth_mode_token_store_path_from_env(tmp_path: Path) -> None:
    store_path = _absent_store(tmp_path)
    config = DeputyConfig.from_env(
        {
            "DEPUTY_OAUTH_CLIENT_ID": _CLIENT_ID,
            "DEPUTY_OAUTH_CLIENT_SECRET": _CLIENT_SECRET,
            "DEPUTY_TOKEN_STORE": store_path,
        }
    )
    assert config.token_store_path == Path(store_path)


def test_stored_endpoint_is_the_base_url_used_for_oauth(tmp_path: Path) -> None:
    # The base URL an OAuth-mode transport talks to is the store's endpoint. Assert the
    # stored value round-trips to the normalized origin the transport will use.
    path = tmp_path / "token.json"
    TokenStore(path).save(OAuthTokens("a", "r", expires_at=1_900_000_000.0, base_url=_BASE_URL))
    reloaded = TokenStore(path).load()
    assert reloaded is not None
    assert reloaded.base_url == _BASE_URL
    # Sanity: the file really is JSON on disk carrying that endpoint.
    assert json.loads(path.read_text(encoding="utf-8"))["base_url"] == _BASE_URL


# --------------------------------------------------------------------------- #
# OAuth client secret redaction
# --------------------------------------------------------------------------- #
def test_oauth_client_secret_is_redacted_but_accessible(tmp_path: Path) -> None:
    config = DeputyConfig.from_env(
        {
            "DEPUTY_OAUTH_CLIENT_ID": _CLIENT_ID,
            "DEPUTY_OAUTH_CLIENT_SECRET": _CLIENT_SECRET,
            "DEPUTY_TOKEN_STORE": _absent_store(tmp_path),
        }
    )
    # The secret never renders in repr/str...
    assert _CLIENT_SECRET not in repr(config)
    assert _CLIENT_SECRET not in str(config)
    # ...but is retrievable through the explicit accessor for the token exchange.
    assert config.oauth_client_secret_value() == _CLIENT_SECRET


def test_oauth_client_secret_accessor_raises_when_unset(tmp_path: Path) -> None:
    # A stored-token-only OAuth config has no client secret in env; the accessor must
    # fail with an actionable message rather than return an empty string.
    config = DeputyConfig.from_env({"DEPUTY_TOKEN_STORE": _write_store(tmp_path)})
    with pytest.raises(DeputyConfigError):
        config.oauth_client_secret_value()
