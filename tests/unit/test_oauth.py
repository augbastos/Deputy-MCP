"""Unit tests for the OAuth 2.0 primitives in :mod:`deputy_mcp.oauth`.

Covers the pure/parsing surface of OAuth mode without any browser or loopback:

* ``build_authorize_url`` emits the exact Authorization Code query parameters.
* ``exchange_code`` / ``refresh`` parse a (respx-mocked) token-endpoint response,
  tolerating the several install-endpoint field spellings the live API might use
  (``endpoint`` / ``install`` / ``Endpoint``) and normalizing it to a bare origin.
* :class:`TokenStore` round-trips tokens on disk with owner-only permissions and
  never leaks a token value in a ``repr``/``str`` or a raised exception.
* :class:`OAuthTokens` redacts its ``repr`` and expires with a configurable skew.

Every client id / secret / token here is FICTIONAL — not a real credential.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from deputy_mcp.client.errors import DeputyAPIError, DeputyAuthError, DeputyError
from deputy_mcp.oauth import (
    AUTHORIZE_URL,
    SCOPE,
    TOKEN_URL,
    OAuthTokens,
    TokenStore,
    build_authorize_url,
    exchange_code,
    refresh,
)

# Fictional install + credentials (never real secrets).
_INSTALL_ORIGIN = "https://acme.eu.deputy.com"
_CLIENT_ID = "fake-client-id-abc"
_CLIENT_SECRET = "fake-client-secret-xyz"  # fictional, not a real secret
_REDIRECT = "http://localhost:8823/callback"


# --------------------------------------------------------------------------- #
# build_authorize_url
# --------------------------------------------------------------------------- #
def test_build_authorize_url_carries_all_flow_params() -> None:
    url = build_authorize_url(_CLIENT_ID, _REDIRECT, "state-token-123")
    assert url.startswith(AUTHORIZE_URL + "?")
    query = parse_qs(urlparse(url).query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [_CLIENT_ID]
    assert query["redirect_uri"] == [_REDIRECT]
    assert query["scope"] == [SCOPE]
    assert query["state"] == ["state-token-123"]


def test_build_authorize_url_uses_longlife_refresh_scope() -> None:
    # The scope must request the long-life refresh token, or the flow yields no
    # refresh_token and the client cannot stay signed in.
    assert SCOPE == "longlife_refresh_token"
    query = parse_qs(urlparse(build_authorize_url(_CLIENT_ID, _REDIRECT, "s")).query)
    assert query["scope"] == ["longlife_refresh_token"]


# --------------------------------------------------------------------------- #
# exchange_code / refresh — token-endpoint parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("endpoint_field", ["endpoint", "install", "Endpoint"])
async def test_exchange_code_accepts_each_endpoint_spelling(endpoint_field: str) -> None:
    body = {
        "access_token": "acc-1",
        "refresh_token": "ref-1",
        "token_type": "Bearer",
        "expires_in": 3600,
        endpoint_field: f"{_INSTALL_ORIGIN}/",
    }
    with respx.mock(assert_all_called=False) as router:
        route = router.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=body))
        async with httpx.AsyncClient() as http:
            tokens = await exchange_code(http, _CLIENT_ID, _CLIENT_SECRET, "the-code", _REDIRECT)
    assert route.called
    assert tokens.access_token == "acc-1"
    assert tokens.refresh_token == "ref-1"
    # The trailing slash is stripped to the bare install origin.
    assert tokens.base_url == _INSTALL_ORIGIN


async def test_exchange_code_normalizes_endpoint_with_api_suffix() -> None:
    body = {
        "access_token": "acc-2",
        "refresh_token": "ref-2",
        "expires_in": 3600,
        # A full API URL (scheme-less + /api/v1 + trailing slash) still normalizes.
        "endpoint": "acme.eu.deputy.com/api/v1/",
    }
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=body))
        async with httpx.AsyncClient() as http:
            tokens = await exchange_code(http, _CLIENT_ID, _CLIENT_SECRET, "code", _REDIRECT)
    assert tokens.base_url == _INSTALL_ORIGIN


async def test_exchange_code_posts_authorization_code_grant() -> None:
    body = {"access_token": "a", "refresh_token": "r", "endpoint": _INSTALL_ORIGIN}
    with respx.mock(assert_all_called=False) as router:
        route = router.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=body))
        async with httpx.AsyncClient() as http:
            await exchange_code(http, _CLIENT_ID, _CLIENT_SECRET, "the-code", _REDIRECT)
    sent = parse_qs(route.calls.last.request.content.decode())
    assert sent["grant_type"] == ["authorization_code"]
    assert sent["code"] == ["the-code"]
    assert sent["client_id"] == [_CLIENT_ID]
    assert sent["redirect_uri"] == [_REDIRECT]
    assert sent["scope"] == [SCOPE]


async def test_refresh_posts_refresh_token_grant_and_parses() -> None:
    body = {
        "access_token": "acc-new",
        "refresh_token": "ref-new",
        "expires_in": 1800,
        "endpoint": _INSTALL_ORIGIN,
    }
    with respx.mock(assert_all_called=False) as router:
        route = router.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=body))
        async with httpx.AsyncClient() as http:
            tokens = await refresh(http, _CLIENT_ID, _CLIENT_SECRET, "ref-old")
    sent = parse_qs(route.calls.last.request.content.decode())
    assert sent["grant_type"] == ["refresh_token"]
    assert sent["refresh_token"] == ["ref-old"]
    assert tokens.access_token == "acc-new"
    assert tokens.refresh_token == "ref-new"


async def test_refresh_keeps_old_refresh_token_when_response_omits_it() -> None:
    # Deputy may rotate the access token without returning a new refresh token; the
    # existing long-life refresh token must be retained rather than lost.
    body = {"access_token": "acc-new", "expires_in": 900, "endpoint": _INSTALL_ORIGIN}
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=body))
        async with httpx.AsyncClient() as http:
            tokens = await refresh(http, _CLIENT_ID, _CLIENT_SECRET, "ref-keep-me")
    assert tokens.refresh_token == "ref-keep-me"


async def test_exchange_code_defaults_expiry_when_missing() -> None:
    # A response without expires_in must still yield a future-dated (non-expired) token.
    body = {"access_token": "a", "refresh_token": "r", "endpoint": _INSTALL_ORIGIN}
    before = time.time()
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=body))
        async with httpx.AsyncClient() as http:
            tokens = await exchange_code(http, _CLIENT_ID, _CLIENT_SECRET, "c", _REDIRECT)
    assert tokens.expires_at > before
    assert not tokens.is_expired()


# --------------------------------------------------------------------------- #
# exchange_code / refresh — error handling and secret redaction
# --------------------------------------------------------------------------- #
async def test_token_endpoint_400_raises_auth_error_scrubbing_secrets() -> None:
    # The error body echoes the client secret and code; neither may survive into the
    # raised exception's message.
    echoed = f"invalid_client secret={_CLIENT_SECRET} code=the-code"
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(return_value=httpx.Response(400, text=echoed))
        async with httpx.AsyncClient() as http:
            with pytest.raises(DeputyAuthError) as excinfo:
                await exchange_code(http, _CLIENT_ID, _CLIENT_SECRET, "the-code", _REDIRECT)
    text = str(excinfo.value)
    assert _CLIENT_SECRET not in text
    assert "the-code" not in text
    assert "deputy-mcp login" in text


async def test_token_endpoint_500_raises_api_error() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(return_value=httpx.Response(503, text="upstream down"))
        async with httpx.AsyncClient() as http:
            with pytest.raises(DeputyAPIError):
                await refresh(http, _CLIENT_ID, _CLIENT_SECRET, "ref")


async def test_token_response_without_endpoint_raises_without_leaking_access_token() -> None:
    # Missing install endpoint is a hard error, but the access token value must not
    # appear in the surfaced message.
    body = {"access_token": "leak-me-token", "refresh_token": "r"}
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=body))
        async with httpx.AsyncClient() as http:
            with pytest.raises(DeputyError) as excinfo:
                await exchange_code(http, _CLIENT_ID, _CLIENT_SECRET, "c", _REDIRECT)
    assert "leak-me-token" not in str(excinfo.value)


async def test_token_response_without_access_token_raises() -> None:
    body = {"refresh_token": "r", "endpoint": _INSTALL_ORIGIN}
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=body))
        async with httpx.AsyncClient() as http:
            with pytest.raises(DeputyAuthError):
                await exchange_code(http, _CLIENT_ID, _CLIENT_SECRET, "c", _REDIRECT)


# --------------------------------------------------------------------------- #
# OAuthTokens — expiry + redaction
# --------------------------------------------------------------------------- #
def test_is_expired_uses_default_skew() -> None:
    # Within the default 60s skew window -> treated as expired so refresh pre-empts a 401.
    soon = OAuthTokens("a", "r", expires_at=time.time() + 30, base_url=_INSTALL_ORIGIN)
    assert soon.is_expired() is True
    # Comfortably in the future -> not expired.
    later = OAuthTokens("a", "r", expires_at=time.time() + 3600, base_url=_INSTALL_ORIGIN)
    assert later.is_expired() is False


def test_is_expired_respects_custom_skew() -> None:
    tokens = OAuthTokens("a", "r", expires_at=time.time() + 30, base_url=_INSTALL_ORIGIN)
    # With zero skew a token 30s from expiry is still valid...
    assert tokens.is_expired(skew=0) is False
    # ...but a generous skew pulls the effective deadline earlier.
    assert tokens.is_expired(skew=120) is True


def test_oauth_tokens_repr_redacts_both_tokens() -> None:
    tokens = OAuthTokens(
        "super-secret-access",
        "super-secret-refresh",
        expires_at=1_800_000_000.0,
        base_url=_INSTALL_ORIGIN,
    )
    rendered = repr(tokens)
    assert "super-secret-access" not in rendered
    assert "super-secret-refresh" not in rendered
    assert "***" in rendered
    # str() falls back to repr for a dataclass, so it must be redacted too.
    assert "super-secret-access" not in str(tokens)
    assert _INSTALL_ORIGIN in rendered  # base_url is not a secret and stays visible


# --------------------------------------------------------------------------- #
# TokenStore — round-trip, permissions, redaction, delete
# --------------------------------------------------------------------------- #
def _tokens() -> OAuthTokens:
    return OAuthTokens(
        access_token="stored-access-secret",
        refresh_token="stored-refresh-secret",
        expires_at=1_900_000_000.0,
        base_url=_INSTALL_ORIGIN,
    )


def test_token_store_round_trip(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "token.json")
    store.save(_tokens())
    loaded = store.load()
    assert loaded is not None
    assert loaded.access_token == "stored-access-secret"
    assert loaded.refresh_token == "stored-refresh-secret"
    assert loaded.expires_at == 1_900_000_000.0
    assert loaded.base_url == _INSTALL_ORIGIN


def test_token_store_creates_parent_directory(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "nested" / "dir" / "token.json")
    store.save(_tokens())
    assert store.path.is_file()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits are best-effort on Windows")
def test_token_store_writes_owner_only_permissions(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "token.json")
    store.save(_tokens())
    mode = stat.S_IMODE(os.stat(store.path).st_mode)
    assert mode == 0o600


def test_token_store_missing_file_loads_none(tmp_path: Path) -> None:
    assert TokenStore(tmp_path / "absent.json").load() is None


def test_token_store_corrupt_file_loads_none(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert TokenStore(path).load() is None


def test_token_store_incomplete_payload_loads_none(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    # Missing refresh_token / base_url -> not a usable token set.
    path.write_text(json.dumps({"access_token": "a"}), encoding="utf-8")
    assert TokenStore(path).load() is None


def test_token_store_repr_only_shows_path_not_tokens(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "token.json")
    store.save(_tokens())
    rendered = repr(store)
    assert "stored-access-secret" not in rendered
    assert "stored-refresh-secret" not in rendered


def test_token_store_delete(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "token.json")
    store.save(_tokens())
    assert store.delete() is True
    assert store.load() is None
    # Deleting an absent store is a no-op that reports nothing was removed.
    assert store.delete() is False
