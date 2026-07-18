"""Tests for the interactive loopback login flow (``deputy_mcp.oauth.run_login_flow``).

The browser step is simulated: ``webbrowser.open`` is monkeypatched to a function
that reads the ``redirect_uri`` and ``state`` out of the authorize URL and fires the
real ``GET /callback`` against the one-shot loopback server that ``run_login_flow``
started — exactly what a browser would do after the user approves. The callback is
sent with ``urllib`` (NOT httpx) so it reaches the actual loopback server instead of
being captured by respx; only the token-endpoint exchange is respx-mocked.

Covered: the happy path returns the exchanged tokens; a mismatched ``state`` is
rejected (CSRF guard); a callback that never arrives times out cleanly; and a config
without client credentials cannot start a login. All values are FICTIONAL.
"""

from __future__ import annotations

import socket
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
import respx

from deputy_mcp import oauth
from deputy_mcp.client.errors import DeputyAuthError, DeputyConfigError
from deputy_mcp.config import DeputyConfig
from deputy_mcp.oauth import TOKEN_URL, OAuthTokens, TokenStore, run_login_flow

_INSTALL_ORIGIN = "https://acme.eu.deputy.com"
_CLIENT_ID = "fake-oauth-client-id"
_CLIENT_SECRET = "fake-oauth-client-secret"  # fictional, not a real secret


def _free_port() -> int:
    """Reserve and release an ephemeral local port to minimize bind clashes."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("localhost", 0))
        return int(sock.getsockname()[1])


def _oauth_config(port: int, store_path: Path) -> DeputyConfig:
    return DeputyConfig.from_env(
        {
            "DEPUTY_OAUTH_CLIENT_ID": _CLIENT_ID,
            "DEPUTY_OAUTH_CLIENT_SECRET": _CLIENT_SECRET,
            "DEPUTY_OAUTH_REDIRECT_PORT": str(port),
            "DEPUTY_TOKEN_STORE": str(store_path),
            "DEPUTY_TIMEOUT": "5",
        }
    )


def _callback_firer(state_override: str | None = None) -> Callable[[str], bool]:
    """Build a fake ``webbrowser.open`` that drives the loopback callback.

    Parses the ``redirect_uri`` + ``state`` from the authorize URL and issues the
    ``GET /callback?code=..&state=..`` a browser would. ``state_override`` forces a
    wrong state for the CSRF-rejection test.
    """

    def _open(authorize_url: str) -> bool:
        query = parse_qs(urlparse(authorize_url).query)
        redirect_uri = query["redirect_uri"][0]
        state = state_override if state_override is not None else query["state"][0]
        callback = f"{redirect_uri}?{urlencode({'code': 'fake-auth-code', 'state': state})}"
        with urllib.request.urlopen(callback, timeout=5) as response:  # localhost callback
            response.read()
        return True

    return _open


async def test_run_login_flow_success_returns_exchanged_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_path = tmp_path / "token.json"
    config = _oauth_config(_free_port(), store_path)
    monkeypatch.setattr("webbrowser.open", _callback_firer())

    token_body = {
        "access_token": "flow-access",
        "refresh_token": "flow-refresh",
        "token_type": "Bearer",
        "expires_in": 3600,
        "endpoint": _INSTALL_ORIGIN,
    }
    with respx.mock(assert_all_called=False) as router:
        token_route = router.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=token_body))
        tokens = await run_login_flow(config)

    # run_login_flow exchanges the code and returns the tokens; persistence to the
    # store is the CLI `login` command's step (TokenStore round-trip is in test_oauth.py).
    assert token_route.call_count == 1
    assert tokens.access_token == "flow-access"
    assert tokens.refresh_token == "flow-refresh"
    assert tokens.base_url == _INSTALL_ORIGIN
    # The exchanged tokens are persistable: saving and reloading round-trips them.
    TokenStore(store_path).save(tokens)
    reloaded = TokenStore(store_path).load()
    assert reloaded is not None
    assert reloaded.access_token == "flow-access"


async def test_run_login_flow_rejects_state_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _oauth_config(_free_port(), tmp_path / "token.json")
    monkeypatch.setattr("webbrowser.open", _callback_firer(state_override="attacker-state"))

    # No token route is registered: the flow must reject the callback before exchanging.
    with pytest.raises(DeputyAuthError) as excinfo:
        await run_login_flow(config)
    assert "state" in str(excinfo.value).lower()


async def test_run_login_flow_times_out_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _oauth_config(_free_port(), tmp_path / "token.json")
    # A browser that never returns a callback: shorten the wait so the test is fast.
    monkeypatch.setattr(oauth, "_CALLBACK_TIMEOUT_S", 0.3)
    monkeypatch.setattr("webbrowser.open", lambda _url: True)

    with pytest.raises(DeputyAuthError) as excinfo:
        await run_login_flow(config)
    assert "timed out" in str(excinfo.value).lower()


async def test_run_login_flow_requires_client_credentials(tmp_path: Path) -> None:
    # A config with a stored token but no client id/secret cannot start a fresh login.
    store_path = tmp_path / "token.json"
    TokenStore(store_path).save(
        OAuthTokens("a", "r", expires_at=1_900_000_000.0, base_url=_INSTALL_ORIGIN)
    )
    config = DeputyConfig.from_env({"DEPUTY_TOKEN_STORE": str(store_path)})
    with pytest.raises(DeputyConfigError):
        await run_login_flow(config)
