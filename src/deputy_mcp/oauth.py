"""OAuth 2.0 authorization-code (loopback) support for deputy-mcp.

Lets an ordinary Deputy employee — who cannot mint a permanent API token — run the
standard Authorization Code flow against a one-shot loopback redirect to obtain a
real ``access_token`` (+ long-life ``refresh_token``) bound to their own account.
The tokens unlock the same full ``/my/*`` surface the static-token ("api") mode
uses; manager-only tools keep degrading with a permission error for a non-manager.

Secrets discipline: no access token, refresh token, client secret, or authorization
code is ever logged, printed, or placed in an exception. :class:`OAuthTokens`
redacts its ``repr``; the loopback handler suppresses the stdlib request log (which
would echo the ``?code=`` query); and token-endpoint error bodies are scrubbed of
any secret we hold before surfacing. The live endpoints are smoke-test-pending, so
responses are parsed defensively (several install-endpoint field spellings).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import secrets
import stat
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from deputy_mcp.client.errors import (
    DeputyAPIError,
    DeputyAuthError,
    DeputyConfigError,
    DeputyError,
)
from deputy_mcp.config import _normalize_base_url

if TYPE_CHECKING:
    from deputy_mcp.config import DeputyConfig

#: Browser authorize endpoint (GET). Smoke-test-pending: the exact host may be
#: the install host rather than ``once.deputy.com`` — surface clear errors, do
#: not silently assume success.
AUTHORIZE_URL = "https://once.deputy.com/my/oauth/login"
#: Token endpoint (POST form) for both code exchange and refresh.
TOKEN_URL = "https://once.deputy.com/my/oauth/access_token"
#: Scope requesting a long-life refresh token alongside the access token.
SCOPE = "longlife_refresh_token"
#: Default loopback port for the redirect URI (overridable per install).
DEFAULT_REDIRECT_PORT = 8823

#: Fallback access-token lifetime (seconds) when the response omits ``expires_in``.
#: Token lifetime is a documented GAP; assume a conservative hour so the client
#: refreshes rather than trusting a stale token indefinitely.
_DEFAULT_EXPIRES_IN = 3600
#: How long the login flow waits for the browser callback before giving up.
_CALLBACK_TIMEOUT_S = 180.0
#: Response fields, in priority order, that may carry the install base URL.
_ENDPOINT_FIELDS = ("endpoint", "install", "Endpoint")

#: One actionable hint reused across auth failures (never contains a secret).
_LOGIN_HINT = (
    "Re-run 'deputy-mcp login'. Check DEPUTY_OAUTH_CLIENT_ID / "
    "DEPUTY_OAUTH_CLIENT_SECRET are correct and that the app's redirect URI "
    "matches exactly (default http://localhost:8823/callback)."
)

_SUCCESS_HTML = (
    "<!doctype html><meta charset='utf-8'><title>deputy-mcp</title>"
    "<body style='font-family:system-ui;margin:3rem'>"
    "<h1>Authorized</h1><p>deputy-mcp is now signed in. "
    "You can close this tab and return to the terminal.</p></body>"
)
_ERROR_HTML = (
    "<!doctype html><meta charset='utf-8'><title>deputy-mcp</title>"
    "<body style='font-family:system-ui;margin:3rem'>"
    "<h1>Sign-in failed</h1><p>deputy-mcp could not complete authorization. "
    "Return to the terminal for details and try 'deputy-mcp login' again.</p></body>"
)


@dataclass(frozen=True)
class OAuthTokens:
    """A resolved OAuth token set bound to one Deputy install.

    Attributes:
        access_token: Bearer token for ``/api/v1`` requests (SECRET).
        refresh_token: Long-life token used to mint new access tokens (SECRET).
        expires_at: Absolute expiry as epoch seconds (``time.time()`` scale).
        base_url: Normalized install origin, e.g. ``https://acme.eu.deputy.com``.

    The ``repr`` redacts both token values so the object is safe to log.
    """

    access_token: str
    refresh_token: str
    expires_at: float
    base_url: str

    def is_expired(self, skew: float = 60.0) -> bool:
        """Whether the token is expired, ``skew`` seconds early so refresh pre-empts it."""
        return time.time() >= (self.expires_at - skew)

    def __repr__(self) -> str:  # pragma: no cover - trivial redaction
        return (
            "OAuthTokens(access_token='***', refresh_token='***', "
            f"expires_at={self.expires_at!r}, base_url={self.base_url!r})"
        )


class TokenStore:
    """Load/save :class:`OAuthTokens` as JSON on disk, never logging values.

    The file holds live credentials, so it is written with owner-only ``0600``
    permissions (best effort — POSIX bits are a no-op on some Windows setups) and
    its location is gitignored. All load errors are swallowed to ``None`` so a
    missing/corrupt store degrades to "not logged in" rather than crashing.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """The on-disk location of the token store."""
        return self._path

    def load(self) -> OAuthTokens | None:
        """Return the stored tokens, or ``None`` if absent/unreadable/corrupt."""
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        access = data.get("access_token")
        refresh = data.get("refresh_token")
        base_url = data.get("base_url")
        expires_at = data.get("expires_at")
        if not (isinstance(access, str) and isinstance(refresh, str) and isinstance(base_url, str)):
            return None
        if not isinstance(expires_at, (int, float, str)):
            return None
        try:
            expires = float(expires_at)
        except (TypeError, ValueError):
            return None
        return OAuthTokens(
            access_token=access,
            refresh_token=refresh,
            expires_at=expires,
            base_url=base_url,
        )

    def save(self, tokens: OAuthTokens) -> None:
        """Persist ``tokens`` with best-effort ``0600`` permissions."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expires_at": tokens.expires_at,
            "base_url": tokens.base_url,
        }
        # Write then tighten perms. Create with restrictive perms up front where
        # supported so the secret is never briefly world-readable.
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        # POSIX bits may be unsupported (e.g. some Windows filesystems); the file
        # still lives in a gitignored, per-user location, so best-effort is enough.
        with contextlib.suppress(OSError):
            os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)

    def delete(self) -> bool:
        """Remove the token store if present. Returns whether a file was deleted."""
        try:
            self._path.unlink()
            return True
        except OSError:
            return False

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"TokenStore(path={self._path!r})"


def build_authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Build the browser authorize URL for the Authorization Code flow."""
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


async def exchange_code(
    http: httpx.AsyncClient,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> OAuthTokens:
    """Exchange an authorization ``code`` for an access/refresh token pair."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
    }
    return await _post_token(http, data, scrub=(client_secret, code))


async def refresh(
    http: httpx.AsyncClient,
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> OAuthTokens:
    """Mint a fresh access token from a ``refresh_token`` (grant_type=refresh_token)."""
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": SCOPE,
    }
    return await _post_token(
        http,
        data,
        scrub=(client_secret, refresh_token),
        fallback_refresh=refresh_token,
    )


async def _post_token(
    http: httpx.AsyncClient,
    data: dict[str, str],
    *,
    scrub: tuple[str, ...],
    fallback_refresh: str | None = None,
) -> OAuthTokens:
    """POST a form to the token endpoint and parse the result defensively."""
    try:
        response = await http.post(TOKEN_URL, data=data)
    except httpx.HTTPError as exc:
        msg = "Could not reach Deputy's OAuth token endpoint."
        raise DeputyError(msg, hint=_LOGIN_HINT) from exc

    if response.status_code >= 400:
        body = _redact(response.text, scrub)
        message = f"Deputy rejected the OAuth token request (HTTP {response.status_code})."
        if body:
            message = f"{message} Response: {body}"
        if response.status_code in (400, 401):
            raise DeputyAuthError(message, hint=_LOGIN_HINT, status_code=response.status_code)
        raise DeputyAPIError(message, hint=_LOGIN_HINT, status_code=response.status_code)

    try:
        parsed = response.json()
    except ValueError as exc:
        msg = "Deputy's OAuth token response was not valid JSON."
        raise DeputyError(msg, hint=_LOGIN_HINT) from exc
    if not isinstance(parsed, dict):
        raise DeputyError("Deputy's OAuth token response was not a JSON object.", hint=_LOGIN_HINT)
    return _tokens_from_response(parsed, fallback_refresh=fallback_refresh)


def _tokens_from_response(
    data: dict[str, Any],
    *,
    fallback_refresh: str | None = None,
) -> OAuthTokens:
    """Build :class:`OAuthTokens` from a token-endpoint JSON body, tolerating gaps."""
    access = data.get("access_token")
    if not isinstance(access, str) or not access:
        raise DeputyAuthError(
            "Deputy's OAuth response contained no access_token.",
            hint=_LOGIN_HINT,
        )

    refresh_value = data.get("refresh_token")
    if not (isinstance(refresh_value, str) and refresh_value):
        refresh_value = fallback_refresh or ""

    expires_in = _coerce_float(data.get("expires_in"), _DEFAULT_EXPIRES_IN)

    endpoint = _first_str(data, _ENDPOINT_FIELDS)
    if not endpoint:
        raise DeputyError(
            "Deputy's OAuth response did not include the install endpoint.",
            hint="The token response shape may have changed. " + _LOGIN_HINT,
        )
    base_url = _normalize_base_url(endpoint)

    return OAuthTokens(
        access_token=access,
        refresh_token=refresh_value,
        expires_at=time.time() + expires_in,
        base_url=base_url,
    )


async def run_login_flow(config: DeputyConfig) -> OAuthTokens:
    """Run the interactive loopback Authorization Code flow and return tokens.

    Starts a one-shot loopback server on ``config.redirect_port``, opens the browser
    to the authorize URL with a random ``state``, waits for the ``/callback`` GET,
    validates ``state`` (constant-time), exchanges the code and returns the tokens.
    Never prints a secret — only progress; the caller surfaces base_url/expiry.
    """
    client_id = (config.oauth_client_id or "").strip()
    if not client_id or config.oauth_client_secret is None:
        raise DeputyConfigError(
            "OAuth login needs a registered app.",
            hint=(
                "Register an app at https://once.deputy.com/my/oauth_clients with "
                "redirect URI http://localhost:8823/callback, then set "
                "DEPUTY_OAUTH_CLIENT_ID and DEPUTY_OAUTH_CLIENT_SECRET."
            ),
        )
    client_secret = config.oauth_client_secret_value()
    port = config.redirect_port
    redirect_uri = f"http://localhost:{port}/callback"
    state = secrets.token_urlsafe(32)

    result = _CallbackResult()
    try:
        # Bind the same host the redirect URI names so the browser's callback lands
        # here regardless of how "localhost" resolves (IPv4/IPv6) on this machine.
        server = _CallbackServer(("localhost", port), _CallbackHandler)
    except OSError as exc:
        raise DeputyError(
            f"Could not start the loopback login server on port {port}.",
            hint=(
                "The port may be in use. Set DEPUTY_OAUTH_REDIRECT_PORT to a free "
                "port and register a matching redirect URI, then re-run "
                "'deputy-mcp login'."
            ),
        ) from exc
    server.expected_state = state
    server.result = result

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        authorize_url = build_authorize_url(client_id, redirect_uri, state)
        webbrowser.open(authorize_url)
        print("Opening your browser to authorize deputy-mcp. Waiting for sign-in...")
        # Block off the event loop thread so a slow browser cannot stall it.
        got = await asyncio.to_thread(result.event.wait, _CALLBACK_TIMEOUT_S)
        if not got:
            _fail_login("Timed out waiting for the Deputy authorization callback.")
        if result.error:
            _fail_login(f"Deputy returned an authorization error: {_safe_token(result.error)}.")
        if not hmac.compare_digest(result.state, state):
            _fail_login("The authorization callback state did not match (possible CSRF).")
        if not result.code:
            _fail_login("The authorization callback carried no code.")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    async with httpx.AsyncClient(timeout=config.timeout) as http:
        return await exchange_code(http, client_id, client_secret, result.code, redirect_uri)


@dataclass
class _CallbackResult:
    """Thread-safe holder for the values captured from the loopback callback."""

    event: threading.Event = field(default_factory=threading.Event)
    code: str = ""
    state: str = ""
    error: str = ""

    def record(self, *, code: str, state: str, error: str) -> None:
        self.code = code
        self.state = state
        self.error = error
        self.event.set()


class _CallbackServer(HTTPServer):
    """Loopback server carrying the expected state and the result sink."""

    expected_state: str
    result: _CallbackResult


class _CallbackHandler(BaseHTTPRequestHandler):
    """Handle the single ``GET /callback`` and serve a close-me page."""

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = parse_qs(parsed.query)
        code = _first_param(params, "code")
        state = _first_param(params, "state")
        error = _first_param(params, "error")
        server = cast(_CallbackServer, self.server)
        ok = bool(code) and not error and hmac.compare_digest(state, server.expected_state)
        server.result.record(code=code, state=state, error=error)
        body = (_SUCCESS_HTML if ok else _ERROR_HTML).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress the default stderr access log: the request line contains the
        # ?code=... query, i.e. the authorization code. Never emit it.
        return


def _fail_login(message: str) -> NoReturn:
    """Raise a login auth error carrying the shared re-login hint."""
    raise DeputyAuthError(message, hint=_LOGIN_HINT)


def _first_param(params: dict[str, list[str]], name: str) -> str:
    values = params.get(name)
    return values[0] if values else ""


def _first_str(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    """Return the first non-empty string value among ``keys`` in ``data``."""
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _coerce_float(value: Any, default: float) -> float:
    """Coerce a JSON value to float, falling back to ``default`` on failure."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _redact(text: str | None, secrets_to_scrub: tuple[str, ...]) -> str | None:
    """Trim a response body and mask any known secret substrings it echoes."""
    if not text:
        return None
    trimmed = text.strip()
    if not trimmed:
        return None
    for secret in secrets_to_scrub:
        if secret:
            trimmed = trimmed.replace(secret, "***")
    if len(trimmed) > 300:
        trimmed = trimmed[:300] + "..."
    return trimmed


def _safe_token(value: str) -> str:
    """Return a short, safe rendering of an OAuth error code from the provider."""
    cleaned = "".join(ch for ch in value if ch.isalnum() or ch in "-_ ").strip()
    return cleaned[:100] if cleaned else "unknown_error"


__all__ = [
    "AUTHORIZE_URL",
    "DEFAULT_REDIRECT_PORT",
    "SCOPE",
    "TOKEN_URL",
    "OAuthTokens",
    "TokenStore",
    "build_authorize_url",
    "exchange_code",
    "refresh",
    "run_login_flow",
]
