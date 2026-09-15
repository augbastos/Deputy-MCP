"""OAuth credential storage: OS keychain by default, plaintext file only on request.

The autouse ``memory_keyring`` fixture (``tests/conftest.py``) replaces the keyring
backend and points ``HOME`` at a temp dir, so nothing here touches a real keychain or a
real ``~/.deputy-mcp``. Every token value is FICTIONAL.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import keyring
import pytest
import respx
from keyring.backend import KeyringBackend
from keyring.backends import fail

from deputy_mcp import cli, oauth
from deputy_mcp.client import DeputyClient
from deputy_mcp.client.http import DeputyHTTP
from deputy_mcp.config import DeputyConfig
from deputy_mcp.errors import DeputyAuthError, DeputyConfigError, DeputyError
from deputy_mcp.token_store import (
    KEYRING_SERVICE,
    FileTokenStore,
    KeyringTokenStore,
    OAuthTokens,
    legacy_token_path,
    resolve_token_store,
)

_ORIGIN = "https://acme.eu.deputy.com"
_ROSTER_URL = f"{_ORIGIN}/api/v1/my/roster"
_FUTURE = 4_100_000_000.0
_PAST = 1_000_000_000.0
_WINDOW = (date(2035, 7, 1), date(2035, 7, 31))


def _tokens(access: str = "acc-1", refresh: str = "ref-1", expires: float = _FUTURE) -> OAuthTokens:
    return OAuthTokens(
        access_token=access, refresh_token=refresh, expires_at=expires, base_url=_ORIGIN
    )


def _oauth_config(**overrides: object) -> DeputyConfig:
    env = {
        "DEPUTY_OAUTH_CLIENT_ID": "fake-client-id",
        "DEPUTY_OAUTH_CLIENT_SECRET": "fake-client-secret",
        "DEPUTY_CACHE_TTL": "0",
        "DEPUTY_TIMEOUT": "5",
    }
    env.update({k: str(v) for k, v in overrides.items()})
    return DeputyConfig.from_env(env)


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #
def test_keychain_is_the_default_and_a_path_selects_the_file_fallback(tmp_path: Path) -> None:
    assert _oauth_config().token_store_path is None
    assert isinstance(resolve_token_store(None), KeyringTokenStore)
    path = tmp_path / "token.json"
    store = resolve_token_store(_oauth_config(DEPUTY_TOKEN_STORE=path).token_store_path)
    assert isinstance(store, FileTokenStore)
    assert store.location.endswith(str(path))


def test_keychain_round_trip_writes_no_file(memory_keyring: Any) -> None:
    store = KeyringTokenStore(legacy_path=legacy_token_path())
    store.save(_tokens())
    assert store.load() == _tokens()
    assert list(memory_keyring.entries) == [(KEYRING_SERVICE, "oauth-token")]
    assert not legacy_token_path().parent.exists()


# --------------------------------------------------------------------------- #
# Secrets never surface
# --------------------------------------------------------------------------- #
def test_no_secret_in_reprs_or_locations(tmp_path: Path) -> None:
    tokens = _tokens(access="acc-secret-value", refresh="ref-secret-value")
    for text in (
        repr(tokens),
        str(tokens),
        repr(KeyringTokenStore()),
        KeyringTokenStore().location,
        repr(FileTokenStore(tmp_path / "t.json")),
    ):
        assert "acc-secret-value" not in text
        assert "ref-secret-value" not in text


def test_missing_keychain_fails_closed_with_the_file_fallback_hint() -> None:
    keyring.set_keyring(fail.Keyring())
    with pytest.raises(DeputyConfigError) as excinfo:
        KeyringTokenStore().load()
    assert "DEPUTY_TOKEN_STORE" in str(excinfo.value)


class _LeakyBackend(KeyringBackend):
    """A backend whose error message echoes the secret it was handed."""

    priority = 1  # type: ignore[assignment]

    def get_password(self, service: str, username: str) -> str | None:
        return None

    def set_password(self, service: str, username: str, password: str) -> None:
        raise RuntimeError(f"blob too large: {password}")

    def delete_password(self, service: str, username: str) -> None:
        return None


def test_backend_errors_never_echo_the_secret() -> None:
    keyring.set_keyring(_LeakyBackend())
    with pytest.raises(DeputyConfigError) as excinfo:
        KeyringTokenStore().save(_tokens(access="acc-must-not-leak"))
    assert "acc-must-not-leak" not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Logout
# --------------------------------------------------------------------------- #
def test_delete_removes_the_keychain_entry_and_a_leftover_legacy_file(
    memory_keyring: Any,
) -> None:
    store = KeyringTokenStore(legacy_path=legacy_token_path())
    store.save(_tokens())
    FileTokenStore(legacy_token_path()).save(_tokens(expires=_PAST))
    # Loading would migrate; delete directly to prove both locations are cleared.
    assert store.delete() is True
    assert memory_keyring.entries == {}
    assert not legacy_token_path().exists()
    assert store.delete() is False


def test_cli_logout_clears_the_keychain(
    memory_keyring: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("DEPUTY_TOKEN_STORE", raising=False)
    KeyringTokenStore().save(_tokens())
    assert cli.main(["logout"]) == 0
    assert memory_keyring.entries == {}
    assert "OS keychain" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Migration from the pre-0.2.0 plaintext file
# --------------------------------------------------------------------------- #
def test_legacy_file_moves_into_the_keychain_and_is_deleted(
    memory_keyring: Any,
) -> None:
    FileTokenStore(legacy_token_path()).save(_tokens(access="legacy-acc"))
    loaded = KeyringTokenStore(legacy_path=legacy_token_path()).load()
    assert loaded is not None and loaded.access_token == "legacy-acc"
    assert not legacy_token_path().exists()
    assert KeyringTokenStore().load() == loaded


def test_newer_keychain_token_beats_an_older_legacy_file() -> None:
    KeyringTokenStore().save(_tokens(access="keychain-acc", expires=_FUTURE))
    FileTokenStore(legacy_token_path()).save(_tokens(access="legacy-acc", expires=_PAST))
    loaded = KeyringTokenStore(legacy_path=legacy_token_path()).load()
    assert loaded is not None and loaded.access_token == "keychain-acc"
    assert not legacy_token_path().exists()


def test_corrupt_legacy_file_is_left_untouched() -> None:
    legacy_token_path().parent.mkdir(parents=True)
    legacy_token_path().write_text("{not json", encoding="utf-8")
    assert KeyringTokenStore(legacy_path=legacy_token_path()).load() is None
    assert legacy_token_path().exists()


# --------------------------------------------------------------------------- #
# Client integration: refresh rotation, cross-process adoption, login while running
# --------------------------------------------------------------------------- #
def _token_body(access: str, refresh: str) -> dict[str, object]:
    return {"access_token": access, "refresh_token": refresh, "expires_in": 86400}


async def test_refresh_rotation_is_persisted_to_the_keychain() -> None:
    KeyringTokenStore().save(_tokens(access="old-acc", refresh="old-ref", expires=_PAST))
    with respx.mock(assert_all_called=False) as router:
        router.get(_ROSTER_URL).mock(return_value=httpx.Response(200, json=[]))
        token = router.post(oauth.refresh_url(_ORIGIN)).mock(
            return_value=httpx.Response(200, json=_token_body("new-acc", "new-ref"))
        )
        async with DeputyClient(_oauth_config()) as client:
            await client.get_my_roster(*_WINDOW)
    assert token.call_count == 1
    stored = KeyringTokenStore().load()
    assert stored is not None
    assert (stored.access_token, stored.refresh_token) == ("new-acc", "new-ref")


async def test_token_saved_by_another_process_is_adopted_instead_of_refreshing() -> None:
    # This process holds an expired token; another process (a CLI call, or a fresh
    # `deputy-mcp login`) already stored a newer one. Spending our stale refresh token
    # would race Deputy's rotation, so the stored token must be adopted.
    KeyringTokenStore().save(_tokens(access="stale-acc", refresh="stale-ref", expires=_PAST))
    client = DeputyClient(_oauth_config())
    KeyringTokenStore().save(_tokens(access="other-process-acc", refresh="other-ref"))
    with respx.mock(assert_all_called=False) as router:
        roster = router.get(_ROSTER_URL).mock(return_value=httpx.Response(200, json=[]))
        token = router.post(oauth.refresh_url(_ORIGIN)).mock(
            return_value=httpx.Response(200, json=_token_body("x", "y"))
        )
        try:
            await client.get_my_roster(*_WINDOW)
        finally:
            await client.aclose()
    assert token.call_count == 0
    assert roster.calls.last.request.headers["authorization"] == "Bearer other-process-acc"


async def test_concurrent_requests_share_a_single_refresh() -> None:
    KeyringTokenStore().save(_tokens(access="old-acc", refresh="old-ref", expires=_PAST))
    with respx.mock(assert_all_called=False) as router:
        router.get(_ROSTER_URL).mock(return_value=httpx.Response(200, json=[]))
        token = router.post(oauth.refresh_url(_ORIGIN)).mock(
            return_value=httpx.Response(200, json=_token_body("new-acc", "new-ref"))
        )
        async with DeputyClient(_oauth_config()) as client:
            await asyncio.gather(*(client.get_my_roster(*_WINDOW) for _ in range(5)))
    assert token.call_count == 1


async def test_login_while_the_server_runs_takes_effect_without_restart() -> None:
    async with DeputyClient(_oauth_config()) as client:
        with pytest.raises(DeputyError, match="deputy-mcp login"):
            await client.get_my_roster(*_WINDOW)
        KeyringTokenStore().save(_tokens(access="fresh-login-acc"))
        with respx.mock(assert_all_called=False) as router:
            roster = router.get(_ROSTER_URL).mock(return_value=httpx.Response(200, json=[]))
            await client.get_my_roster(*_WINDOW)
    assert roster.calls.last.request.headers["authorization"] == "Bearer fresh-login-acc"


# --------------------------------------------------------------------------- #
# A tampered store cannot redirect the bearer token
# --------------------------------------------------------------------------- #
async def test_tampered_store_host_is_refused_before_any_request() -> None:
    KeyringTokenStore().save(
        OAuthTokens("acc-must-stay-home", "ref", _FUTURE, base_url="https://evil.example.org")
    )
    with respx.mock(assert_all_called=False) as router:
        evil = router.route(host="evil.example.org")
        async with DeputyClient(_oauth_config()) as client:
            with pytest.raises(DeputyAuthError, match="not a Deputy install"):
                await client.get_my_roster(*_WINDOW)
    assert evil.call_count == 0


def test_transport_refuses_a_non_deputy_token_host_at_construction() -> None:
    evil = OAuthTokens("acc", "ref", _FUTURE, base_url="https://deputy.com.evil.example.org")
    with pytest.raises(DeputyAuthError):
        DeputyHTTP(_oauth_config(), oauth_tokens=evil)


async def test_token_for_another_install_is_not_adopted_during_refresh(
    memory_keyring: Any,
) -> None:
    # First read (initial load): our expired token. Second read (the refresh re-check):
    # a fresh token another process stored for a different install. It must be ignored.
    other = OAuthTokens("other-acc", "other-ref", _FUTURE, "https://other.eu.deputy.com")
    answers = iter([_tokens(access="stale-acc", expires=_PAST).to_json(), other.to_json()])
    memory_keyring.get_password = lambda service, username: next(answers, None)
    with respx.mock(assert_all_called=False) as router:
        roster = router.get(_ROSTER_URL).mock(return_value=httpx.Response(200, json=[]))
        token = router.post(oauth.refresh_url(_ORIGIN)).mock(
            return_value=httpx.Response(200, json=_token_body("new-acc", "new-ref"))
        )
        async with DeputyClient(_oauth_config()) as client:
            await client.get_my_roster(*_WINDOW)
    assert token.call_count == 1
    assert roster.calls.last.request.headers["authorization"] == "Bearer new-acc"
    assert str(roster.calls.last.request.url).startswith(_ORIGIN)


# --------------------------------------------------------------------------- #
# The keychain is never read on the event loop, nor at construction
# --------------------------------------------------------------------------- #
async def test_keychain_is_read_off_the_event_loop_and_not_at_construction(
    memory_keyring: Any,
) -> None:
    import threading

    reads: list[bool] = []
    original = memory_keyring.get_password
    loop_thread = threading.get_ident()

    def spying_get(service: str, username: str) -> str | None:
        reads.append(threading.get_ident() == loop_thread)
        return original(service, username)

    memory_keyring.get_password = spying_get
    KeyringTokenStore().save(_tokens())
    reads.clear()
    client = DeputyClient(_oauth_config())
    assert reads == []  # nothing read while the server is being built
    with respx.mock(assert_all_called=False) as router:
        router.get(_ROSTER_URL).mock(return_value=httpx.Response(200, json=[]))
        try:
            await asyncio.gather(*(client.get_my_roster(*_WINDOW) for _ in range(5)))
        finally:
            await client.aclose()
    assert reads == [False]  # one read, in a worker thread, shared by concurrent calls


# --------------------------------------------------------------------------- #
# Logout never reports success when the keychain could not delete
# --------------------------------------------------------------------------- #
class _LockedDeleteBackend(KeyringBackend):
    """Like macOS: any delete failure surfaces as PasswordDeleteError."""

    priority = 1  # type: ignore[assignment]

    def get_password(self, service: str, username: str) -> str | None:
        return _tokens().to_json()

    def set_password(self, service: str, username: str, password: str) -> None:
        return None

    def delete_password(self, service: str, username: str) -> None:
        from keyring.errors import PasswordDeleteError

        raise PasswordDeleteError("keychain is locked")


def test_failed_keychain_delete_is_an_error_not_nothing_to_remove() -> None:
    keyring.set_keyring(_LockedDeleteBackend())
    with pytest.raises(DeputyConfigError, match="remove the OAuth token"):
        KeyringTokenStore().delete()
