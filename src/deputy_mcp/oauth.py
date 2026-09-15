"""OAuth 2.0 authorization-code (loopback) support for Deputy MCP.

Lets an ordinary Deputy employee — who cannot mint a permanent API token — run the
standard Authorization Code flow against a one-shot loopback redirect to obtain a
real ``access_token`` (+ long-life ``refresh_token``) bound to their own account.
The tokens unlock the same full ``/my/*`` surface the static-token ("api") mode
uses; manager-only tools keep degrading with a permission error for a non-manager.

Endpoints follow Deputy's "Using OAuth 2.0" guide: the browser authorizes and the code
is exchanged at ``once.deputy.com``; the token response names the user's install
(``endpoint``), and every later refresh goes to that install's own
``/oauth/access_token``. Deputy access tokens last 24 hours and each refresh rotates the
refresh token, so the new pair must be persisted every time (see
:mod:`deputy_mcp.token_store`).

Secrets discipline: no access token, refresh token, client secret, or authorization
code is ever logged, printed, or placed in an exception. :class:`OAuthTokens`
redacts its ``repr``; the loopback handler suppresses the stdlib request log (which
would echo the ``?code=`` query); and token-endpoint error bodies go through
:func:`deputy_mcp.sanitize.snippet` with every secret we hold before surfacing.
Response fields are parsed defensively, since Deputy publishes no response schema.
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING, Any, NoReturn, cast
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from deputy_mcp._util import DEFAULT_REDIRECT_PORT, normalize_base_url
from deputy_mcp.errors import (
    DeputyAPIError,
    DeputyAuthError,
    DeputyConfigError,
    DeputyError,
)
from deputy_mcp.sanitize import snippet
from deputy_mcp.token_store import OAuthTokens, default_config_dir

if TYPE_CHECKING:
    from deputy_mcp.config import DeputyConfig

#: Browser authorize endpoint (GET).
AUTHORIZE_URL = "https://once.deputy.com/my/oauth/login"
#: Code-exchange endpoint (POST form). Refreshes go to the install instead
#: (:func:`refresh_url`).
TOKEN_URL = "https://once.deputy.com/my/oauth/access_token"
#: Scope requesting a long-life refresh token alongside the access token.
SCOPE = "longlife_refresh_token"

#: Fallback access-token lifetime (seconds) when a response omits ``expires_in``.
#: Deputy documents 24 hours; assume a conservative hour so the client refreshes
#: early rather than trusting a stale token.
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
    "<!doctype html><meta charset='utf-8'><title>Deputy MCP</title>"
    "<body style='font-family:system-ui;margin:3rem'>"
    "<h1>Authorized</h1><p>Deputy MCP is now signed in. "
    "You can close this tab and return to the terminal.</p></body>"
)
_ERROR_HTML = (
    "<!doctype html><meta charset='utf-8'><title>Deputy MCP</title>"
    "<body style='font-family:system-ui;margin:3rem'>"
    "<h1>Sign-in failed</h1><p>Deputy MCP could not complete authorization. "
    "Return to the terminal for details and try 'deputy-mcp login' again.</p></body>"
)


def redirect_uri_for(port: int) -> str:
    """The loopback redirect URI registered on the Deputy OAuth app for ``port``."""
    return f"http://localhost:{port}/callback"


def refresh_url(base_url: str) -> str:
    """The install-scoped token endpoint Deputy requires for refreshes."""
    return f"{base_url}/oauth/access_token"


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
    *,
    allow_custom_host: bool = False,
) -> OAuthTokens:
    """Exchange an authorization ``code`` for an access/refresh token pair.

    ``allow_custom_host`` mirrors :attr:`~deputy_mcp.config.DeputyConfig.allow_custom_host`:
    the resolved ``base_url`` is rejected unless its host ends in ``.deputy.com``, same as
    static-token mode, unless the caller has explicitly opted in to a custom domain.
    """
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
    }
    return await _post_token(
        http, TOKEN_URL, data, scrub=(client_secret, code), allow_custom_host=allow_custom_host
    )


async def refresh(
    http: httpx.AsyncClient,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    *,
    base_url: str,
    redirect_uri: str,
    allow_custom_host: bool = False,
) -> OAuthTokens:
    """Mint a fresh access token from a ``refresh_token`` (grant_type=refresh_token).

    Deputy serves refreshes from the user's own install (``{base_url}/oauth/access_token``)
    rather than ``once.deputy.com``, and requires the app's ``redirect_uri``. The install
    host is checked against the same allowlist as a login before the client secret and
    refresh token are sent to it, so a tampered token store cannot redirect them. Deputy
    rotates the refresh token on every call; the caller must persist the returned pair.
    """
    require_deputy_host(base_url, allow_custom_host=allow_custom_host)
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
    }
    return await _post_token(
        http,
        refresh_url(base_url),
        data,
        scrub=(client_secret, refresh_token),
        fallback_refresh=refresh_token,
        fallback_base_url=base_url,
        allow_custom_host=allow_custom_host,
    )


async def _post_token(
    http: httpx.AsyncClient,
    url: str,
    data: dict[str, str],
    *,
    scrub: tuple[str, ...],
    fallback_refresh: str | None = None,
    fallback_base_url: str | None = None,
    allow_custom_host: bool = False,
) -> OAuthTokens:
    """POST a form to a token endpoint and parse the result defensively."""
    try:
        response = await http.post(url, data=data)
    except httpx.HTTPError as exc:
        msg = "Could not reach Deputy's OAuth token endpoint."
        raise DeputyError(msg, hint=_LOGIN_HINT) from exc

    if response.status_code >= 400:
        body = snippet(response.text, secrets=scrub)
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
    return _tokens_from_response(
        parsed,
        fallback_refresh=fallback_refresh,
        fallback_base_url=fallback_base_url,
        allow_custom_host=allow_custom_host,
    )


def _tokens_from_response(
    data: dict[str, Any],
    *,
    fallback_refresh: str | None = None,
    fallback_base_url: str | None = None,
    allow_custom_host: bool = False,
) -> OAuthTokens:
    """Build :class:`OAuthTokens` from a token-endpoint JSON body, tolerating gaps.

    A code exchange must name the install (``endpoint``). A refresh is already bound to
    one, so ``fallback_base_url`` keeps it when the response omits the field.
    """
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

    endpoint = _first_str(data, _ENDPOINT_FIELDS) or fallback_base_url
    if not endpoint:
        raise DeputyError(
            "Deputy's OAuth response did not include the install endpoint.",
            hint="The token response shape may have changed. " + _LOGIN_HINT,
        )
    base_url = normalize_base_url(endpoint)
    require_deputy_host(base_url, allow_custom_host=allow_custom_host)

    return OAuthTokens(
        access_token=access,
        refresh_token=refresh_value,
        expires_at=time.time() + expires_in,
        base_url=base_url,
    )


def require_deputy_host(base_url: str, *, allow_custom_host: bool) -> None:
    """Refuse an install host outside ``*.deputy.com`` unless custom hosts are allowed.

    The same fail-closed allowlist static-token mode enforces via
    ``DeputyConfig._validate_base_url``: an unexpected host would receive the bearer
    token (and, on refresh, the client secret) on a server we do not control.
    """
    host = urlparse(base_url).hostname or ""
    if host.endswith(".deputy.com") or allow_custom_host:
        return
    raise DeputyAuthError(
        f"Deputy's OAuth response named an install host '{host}' that is not a "
        "Deputy install ('{install}.{geo}.deputy.com'). Refusing to use it.",
        hint=(
            "If this is a legitimate enterprise custom domain, set "
            "DEPUTY_ALLOW_CUSTOM_HOST=true. " + _LOGIN_HINT
        ),
    )


async def run_login_flow(config: DeputyConfig, *, open_browser: bool = True) -> OAuthTokens:
    """Run the interactive loopback Authorization Code flow and return tokens.

    Starts a one-shot loopback server on ``config.redirect_port``, opens the browser
    to the authorize URL with a random ``state``, waits for the ``/callback`` GET,
    validates ``state`` (constant-time), exchanges the code and returns the tokens.
    Never prints a secret — only progress; the caller surfaces base_url/expiry.

    When ``open_browser`` is ``False`` the browser is not launched; the authorize URL
    is printed (flushed) and written to ``authorize_url.txt`` in ``~/.deputy-mcp`` (or
    beside an explicit file token store),
    so the sign-in can be completed manually or in a remote/headless setup.
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
    redirect_uri = redirect_uri_for(port)
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
        if open_browser:
            webbrowser.open(authorize_url)
            print("Opening your browser to authorize Deputy MCP. Waiting for sign-in...")
        else:
            store_dir = (
                config.token_store_path.parent
                if config.token_store_path is not None
                else default_config_dir()
            )
            url_file = store_dir / "authorize_url.txt"
            url_file.parent.mkdir(parents=True, exist_ok=True)
            url_file.write_text(authorize_url, encoding="utf-8")
            print(f"Authorize URL written to {url_file}", flush=True)
            print(authorize_url, flush=True)
            print("Waiting for sign-in...", flush=True)
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
        return await exchange_code(
            http,
            client_id,
            client_secret,
            result.code,
            redirect_uri,
            allow_custom_host=config.allow_custom_host,
        )


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
    "build_authorize_url",
    "exchange_code",
    "redirect_uri_for",
    "refresh",
    "refresh_url",
    "require_deputy_host",
    "run_login_flow",
]
