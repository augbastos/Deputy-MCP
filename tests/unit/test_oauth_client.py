"""Tests for :class:`~deputy_mcp.client.DeputyClient` in OAuth mode.

These exercise the OAuth-only transport wiring: the stored access token + install
``base_url`` drive requests; an expired token is refreshed pre-emptively; a 401
mid-flight triggers exactly one refresh-persist-retry; a failed refresh surfaces an
actionable ``DeputyAuthError``; and a tool call with OAuth creds but no stored token
fails closed telling the user to run ``deputy-mcp login`` (never a crash).

Only the loopback token endpoint and the ``/my/roster`` read are respx-mocked. Every
value here is FICTIONAL — no real install, client id/secret or token appears.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from deputy_mcp.client import DeputyClient
from deputy_mcp.client.errors import DeputyAuthError, DeputyError
from deputy_mcp.oauth import TOKEN_URL, OAuthTokens, TokenStore

_INSTALL_ORIGIN = "https://acme.eu.deputy.com"
_ROSTER_URL = f"{_INSTALL_ORIGIN}/api/v1/my/roster"
_CLIENT_ID = "fake-oauth-client-id"
_CLIENT_SECRET = "fake-oauth-client-secret"

# A future window so get_my_roster reads only /my/roster (no admin QUERY into the past).
_WINDOW_START = date(2035, 7, 1)
_WINDOW_END = date(2035, 7, 31)
_ROSTER_RECORD = {"Id": 1, "Date": "2035-07-22"}

# Comfortably future / past epoch seconds for expiry control (is_expired uses time.time()).
_FUTURE = 4_100_000_000.0
_PAST = 1_000_000_000.0


def _oauth_env(store_path: Path) -> dict[str, str]:
    """A DEPUTY_* env selecting OAuth mode with a given token-store path."""
    return {
        "DEPUTY_OAUTH_CLIENT_ID": _CLIENT_ID,
        "DEPUTY_OAUTH_CLIENT_SECRET": _CLIENT_SECRET,
        "DEPUTY_TOKEN_STORE": str(store_path),
        "DEPUTY_TIMEOUT": "5",
        "DEPUTY_CACHE_TTL": "0",
    }


def _seed_store(store_path: Path, *, access: str, refresh: str, expires_at: float) -> TokenStore:
    """Persist a starting token set and return the store bound to it."""
    store = TokenStore(store_path)
    store.save(
        OAuthTokens(
            access_token=access,
            refresh_token=refresh,
            expires_at=expires_at,
            base_url=_INSTALL_ORIGIN,
        )
    )
    return store


def _token_body(access: str, refresh: str) -> dict[str, object]:
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": 3600,
        "endpoint": _INSTALL_ORIGIN,
    }


async def test_oauth_mode_uses_stored_token_and_base_url(tmp_path: Path) -> None:
    """A live (non-expired) stored token backs requests; no refresh happens."""
    store_path = tmp_path / "token.json"
    _seed_store(store_path, access="live-access", refresh="live-refresh", expires_at=_FUTURE)

    with respx.mock(assert_all_called=False) as router:
        roster = router.get(_ROSTER_URL).mock(
            return_value=httpx.Response(200, json=[_ROSTER_RECORD])
        )
        token = router.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={}))
        async with DeputyClient.from_env(_oauth_env(store_path)) as client:
            assert client.mode == "api"  # OAuth resolves to the full API surface
            rosters = await client.get_my_roster(_WINDOW_START, _WINDOW_END)

    assert [r.Date for r in rosters] == ["2035-07-22"]
    assert token.call_count == 0  # a valid token is never refreshed
    assert roster.calls.last.request.headers["authorization"] == "Bearer live-access"


async def test_oauth_refreshes_on_401_persists_and_retries_once(tmp_path: Path) -> None:
    """A 401 mid-flight triggers one refresh, persists the new token, and retries once."""
    store_path = tmp_path / "token.json"
    store = _seed_store(store_path, access="old-access", refresh="old-refresh", expires_at=_FUTURE)

    with respx.mock(assert_all_called=False) as router:
        roster = router.get(_ROSTER_URL).mock(
            side_effect=[
                httpx.Response(401, json={"error": "expired"}),
                httpx.Response(200, json=[_ROSTER_RECORD]),
            ]
        )
        token = router.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json=_token_body("new-access", "new-refresh"))
        )
        async with DeputyClient.from_env(_oauth_env(store_path)) as client:
            rosters = await client.get_my_roster(_WINDOW_START, _WINDOW_END)

    assert [r.Date for r in rosters] == ["2035-07-22"]
    assert token.call_count == 1  # exactly one refresh
    assert roster.call_count == 2  # original + single retry
    # The retry carried the refreshed bearer, and the store now holds the new token.
    assert roster.calls[0].request.headers["authorization"] == "Bearer old-access"
    assert roster.calls[1].request.headers["authorization"] == "Bearer new-access"
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.access_token == "new-access"
    assert reloaded.refresh_token == "new-refresh"


async def test_oauth_pre_emptive_refresh_when_expired(tmp_path: Path) -> None:
    """An already-expired stored token is refreshed before the first request is sent."""
    store_path = tmp_path / "token.json"
    _seed_store(store_path, access="stale-access", refresh="stale-refresh", expires_at=_PAST)

    with respx.mock(assert_all_called=False) as router:
        roster = router.get(_ROSTER_URL).mock(
            return_value=httpx.Response(200, json=[_ROSTER_RECORD])
        )
        token = router.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json=_token_body("fresh-access", "fresh-refresh"))
        )
        async with DeputyClient.from_env(_oauth_env(store_path)) as client:
            rosters = await client.get_my_roster(_WINDOW_START, _WINDOW_END)

    assert [r.Date for r in rosters] == ["2035-07-22"]
    assert token.call_count == 1  # refreshed pre-emptively, not via a 401 round-trip
    assert roster.call_count == 1
    assert roster.calls.last.request.headers["authorization"] == "Bearer fresh-access"


async def test_oauth_refresh_failure_raises_actionable_auth_error(tmp_path: Path) -> None:
    """When refresh is rejected, the client raises DeputyAuthError pointing at `login`."""
    store_path = tmp_path / "token.json"
    _seed_store(store_path, access="stale-access", refresh="bad-refresh", expires_at=_PAST)

    with respx.mock(assert_all_called=False) as router:
        router.get(_ROSTER_URL).mock(return_value=httpx.Response(200, json=[_ROSTER_RECORD]))
        router.post(TOKEN_URL).mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"})
        )
        async with DeputyClient.from_env(_oauth_env(store_path)) as client:
            with pytest.raises(DeputyAuthError) as excinfo:
                await client.get_my_roster(_WINDOW_START, _WINDOW_END)

    message = str(excinfo.value).lower()
    assert "login" in message  # actionable: re-run deputy-mcp login
    assert "bad-refresh" not in message  # the refresh token is never leaked


async def test_oauth_without_token_store_fails_closed_asking_to_login(tmp_path: Path) -> None:
    """OAuth creds set but no stored token: a tool call fails closed, never crashes."""
    store_path = tmp_path / "does-not-exist.json"
    # Construction must not raise even though there is no token yet.
    async with DeputyClient.from_env(_oauth_env(store_path)) as client:
        assert client.mode == "api"
        with pytest.raises(DeputyError) as excinfo:
            await client.get_my_roster(_WINDOW_START, _WINDOW_END)

    assert "deputy-mcp login" in str(excinfo.value).lower()
