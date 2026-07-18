"""CLI tests for the OAuth ``login`` / ``logout`` subcommands.

These drive the real :func:`deputy_mcp.cli.main`. The browser/token exchange of
``login`` is not re-tested here (that is ``test_login_flow.py``); instead
``oauth.run_login_flow`` is monkeypatched so these focus on the CLI wiring:
registration guidance when creds are missing, persisting tokens + a token-free
success line on success, and deleting the store on logout.

All values are FICTIONAL. Every test points ``DEPUTY_ENV_FILE`` at an empty file so a
developer's on-disk ``.env`` (which may carry OAuth creds) never leaks into the run.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from deputy_mcp import cli, oauth
from deputy_mcp.oauth import OAuthTokens, TokenStore

_INSTALL_ORIGIN = "https://acme.eu.deputy.com"
_CLIENT_ID = "fake-oauth-client-id"
_CLIENT_SECRET = "fake-oauth-client-secret"
_ACCESS_TOKEN = "super-secret-access-token-value"

_VARS = (
    "DEPUTY_API_TOKEN",
    "DEPUTY_BASE_URL",
    "DEPUTY_CALENDAR_URL",
    "DEPUTY_OAUTH_CLIENT_ID",
    "DEPUTY_OAUTH_CLIENT_SECRET",
    "DEPUTY_OAUTH_REDIRECT_PORT",
    "DEPUTY_TOKEN_STORE",
    "DEPUTY_ENV_FILE",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Clear all DEPUTY_* vars and neutralize the on-disk .env; yield the store path."""
    for name in _VARS:
        monkeypatch.delenv(name, raising=False)
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("DEPUTY_ENV_FILE", str(empty_env))
    yield tmp_path / "token.json"


def test_login_without_credentials_prints_registration_steps(
    clean_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No creds at all: login guides the user to register an app and exits 1."""
    assert cli.main(["login"]) == 1
    out = capsys.readouterr().out
    assert "once.deputy.com/my/oauth_clients" in out
    assert "http://localhost:8823/callback" in out
    assert "DEPUTY_OAUTH_CLIENT_ID" in out
    assert "DEPUTY_OAUTH_CLIENT_SECRET" in out


def test_login_success_persists_tokens_without_printing_a_token(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A successful login stores tokens and prints base_url/expiry — never a token value."""
    store_path = clean_env
    monkeypatch.setenv("DEPUTY_OAUTH_CLIENT_ID", _CLIENT_ID)
    monkeypatch.setenv("DEPUTY_OAUTH_CLIENT_SECRET", _CLIENT_SECRET)
    monkeypatch.setenv("DEPUTY_TOKEN_STORE", str(store_path))

    async def _fake_flow(_config: object, *, open_browser: bool = True) -> OAuthTokens:
        return OAuthTokens(
            access_token=_ACCESS_TOKEN,
            refresh_token="fake-refresh-token-value",
            expires_at=4_100_000_000.0,
            base_url=_INSTALL_ORIGIN,
        )

    monkeypatch.setattr(oauth, "run_login_flow", _fake_flow)

    assert cli.main(["login"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Logged in")
    assert _INSTALL_ORIGIN in out
    assert str(store_path) in out
    # The success line must never echo a secret token value.
    assert _ACCESS_TOKEN not in out
    assert "fake-refresh-token-value" not in out
    # The tokens were actually persisted and round-trip from the store.
    reloaded = TokenStore(store_path).load()
    assert reloaded is not None
    assert reloaded.access_token == _ACCESS_TOKEN


def test_logout_removes_an_existing_store(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """logout deletes the token store and reports the removal."""
    store_path = clean_env
    monkeypatch.setenv("DEPUTY_TOKEN_STORE", str(store_path))
    TokenStore(store_path).save(
        OAuthTokens("a", "r", expires_at=4_100_000_000.0, base_url=_INSTALL_ORIGIN)
    )
    assert store_path.exists()

    assert cli.main(["logout"]) == 0
    out = capsys.readouterr().out
    assert "removed token store" in out.lower()
    assert not store_path.exists()


def test_logout_without_a_store_is_graceful(
    clean_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """logout with nothing to remove still exits 0 with a clear message."""
    store_path = clean_env
    monkeypatch.setenv("DEPUTY_TOKEN_STORE", str(store_path))
    assert cli.main(["logout"]) == 0
    out = capsys.readouterr().out
    assert "no deputy token store" in out.lower()
