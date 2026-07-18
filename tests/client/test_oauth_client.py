"""Client-level tests for :class:`deputy_mcp.client.DeputyClient` in OAuth mode.

OAuth mode is a full ``api``-surface client whose bearer is a stored OAuth access
token (base URL recovered from the stored install endpoint). These tests drive the
transport's auto-refresh behaviour against a respx-mocked install:

* a ``401`` triggers a single refresh + persist + retry, and the retry carries the
  new access token;
* a failed refresh surfaces an actionable :class:`DeputyAuthError` (points at
  ``deputy-mcp login``);
* the happy path reads ``/my/roster`` with the stored token and never refreshes;
* an OAuth config with no token store yet fails a tool call with the "run login"
  error rather than crashing.

Every credential and token here is FICTIONAL (see ``tests/conftest.py``).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from deputy_mcp.client import DeputyAuthError, DeputyClient, DeputyError
from deputy_mcp.config import DeputyConfig
from deputy_mcp.oauth import TOKEN_URL, OAuthTokens, TokenStore

_INSTALL = "https://cloud-nine-cafe.eu.deputy.com"
_API_BASE = f"{_INSTALL}/api/v1"
_ROSTER_URL = f"{_API_BASE}/my/roster"
_CLIENT_ID = "fake-oauth-client-id"
_CLIENT_SECRET = "fake-oauth-client-secret"  # fictional, not a real secret


def _write_tokens(
    path: Path,
    *,
    access: str = "stored-access",
    refresh: str = "stored-refresh",
    expires_at: float | None = None,
) -> None:
    """Persist a fictional token set (default: valid for another hour) to ``path``."""
    TokenStore(path).save(
        OAuthTokens(
            access_token=access,
            refresh_token=refresh,
            expires_at=time.time() + 3600 if expires_at is None else expires_at,
            base_url=_INSTALL,
        )
    )


def _oauth_config(store_path: Path) -> DeputyConfig:
    return DeputyConfig.from_env(
        {
            "DEPUTY_OAUTH_CLIENT_ID": _CLIENT_ID,
            "DEPUTY_OAUTH_CLIENT_SECRET": _CLIENT_SECRET,
            "DEPUTY_TOKEN_STORE": str(store_path),
            "DEPUTY_CACHE_TTL": "0",  # disable caching so each request hits respx
        }
    )


def _today_window() -> tuple[Any, Any]:
    """A future-only [start, end] window (start == today UTC -> the /my/roster path)."""
    today = datetime.now(UTC).date()
    return today, today + timedelta(days=7)


async def test_oauth_client_refreshes_on_401_and_persists(
    tmp_path: Path, make_roster: Callable[..., dict[str, Any]]
) -> None:
    store_path = tmp_path / "token.json"
    _write_tokens(store_path, access="old-access", refresh="refresh-1")
    start, end = _today_window()
    roster = make_roster(Date=start.isoformat())
    refreshed = {
        "access_token": "new-access",
        "refresh_token": "refresh-2",
        "expires_in": 3600,
        "endpoint": _INSTALL,
    }

    config = _oauth_config(store_path)
    client = DeputyClient(config)
    with respx.mock(assert_all_called=False) as router:
        roster_route = router.get(_ROSTER_URL).mock(
            side_effect=[
                httpx.Response(401, json={"error": "token_expired"}),
                httpx.Response(200, json=[roster]),
            ]
        )
        token_route = router.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=refreshed))
        try:
            rosters = await client.get_my_roster(start, end)
        finally:
            await client.aclose()

    assert len(rosters) == 1
    # Exactly one refresh, and the roster request was replayed exactly once.
    assert token_route.call_count == 1
    assert roster_route.call_count == 2
    # The new token set was persisted to the store for reuse next run.
    reloaded = TokenStore(store_path).load()
    assert reloaded is not None
    assert reloaded.access_token == "new-access"
    assert reloaded.refresh_token == "refresh-2"
    # The first attempt used the stale token; the retry carried the refreshed one.
    assert roster_route.calls[0].request.headers["authorization"] == "Bearer old-access"
    assert roster_route.calls[1].request.headers["authorization"] == "Bearer new-access"


async def test_oauth_client_refresh_failure_raises_actionable_error(tmp_path: Path) -> None:
    store_path = tmp_path / "token.json"
    _write_tokens(store_path, access="old-access", refresh="refresh-1")
    start, end = _today_window()

    config = _oauth_config(store_path)
    client = DeputyClient(config)
    with respx.mock(assert_all_called=False) as router:
        router.get(_ROSTER_URL).mock(return_value=httpx.Response(401, json={"error": "expired"}))
        router.post(TOKEN_URL).mock(return_value=httpx.Response(400, text="invalid_grant"))
        try:
            with pytest.raises(DeputyAuthError) as excinfo:
                await client.get_my_roster(start, end)
        finally:
            await client.aclose()

    # Actionable: the message tells the user to sign in again.
    assert "deputy-mcp login" in str(excinfo.value)


async def test_oauth_client_get_my_roster_happy_path(
    tmp_path: Path, make_roster: Callable[..., dict[str, Any]]
) -> None:
    store_path = tmp_path / "token.json"
    _write_tokens(store_path, access="stored-access")
    start, end = _today_window()
    roster = make_roster(Date=start.isoformat())

    config = _oauth_config(store_path)
    client = DeputyClient(config)
    assert client.mode == "api"  # OAuth is a full-surface api-mode client
    with respx.mock(assert_all_called=False) as router:
        roster_route = router.get(_ROSTER_URL).mock(return_value=httpx.Response(200, json=[roster]))
        token_route = router.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={}))
        try:
            rosters = await client.get_my_roster(start, end)
        finally:
            await client.aclose()

    assert len(rosters) == 1
    # A valid, unexpired token never triggers a refresh.
    assert token_route.call_count == 0
    assert roster_route.calls.last.request.headers["authorization"] == "Bearer stored-access"


async def test_oauth_client_without_token_store_raises_run_login_error(tmp_path: Path) -> None:
    # OAuth creds are set but no token has been minted yet: a tool call must fail with
    # the actionable "run deputy-mcp login" error, not crash at construction.
    store_path = tmp_path / "absent.json"  # never written
    start, end = _today_window()

    config = _oauth_config(store_path)
    client = DeputyClient(config)
    assert client.mode == "api"
    try:
        with pytest.raises(DeputyError) as excinfo:
            await client.get_my_roster(start, end)
    finally:
        await client.aclose()

    assert "deputy-mcp login" in str(excinfo.value)
