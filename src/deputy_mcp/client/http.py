"""Async HTTP transport for the Deputy API.

:class:`DeputyHTTP` wraps a single :class:`httpx.AsyncClient` bound to the
install's ``/api/v1`` base and the ``Authorization: Bearer`` header. It adds:

* status-code error mapping to the :mod:`deputy_mcp.client.errors` hierarchy;
* defensive retry with exponential back-off and full jitter on throttling
  (429) and transient upstream/transport failures, honoring ``Retry-After``
  when Deputy sends it (Deputy documents no rate limits, so we are conservative);
* a small in-memory TTL cache for read paths that every write invalidates.

The token is only ever placed in the request header — it is never logged, put
in an exception, or included in a cache key.

In OAuth mode the transport is instead bound to a stored ``access_token`` and the
install ``base_url`` recovered from the token store, read off the event loop on the first
request and checked against the host allowlist. When that access token is (or
is about to be) expired, the transport transparently mints a fresh one from the
refresh token, persists it, updates the ``Authorization`` header and retries the
request once. Static-token mode never enters that path, so its behaviour is
unchanged. Neither the access token, refresh token nor client secret is ever
logged or placed in an exception.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from types import TracebackType
from typing import TYPE_CHECKING, Any

import httpx

from deputy_mcp.client.errors import (
    DeputyAPIError,
    DeputyAuthError,
    DeputyError,
    DeputyNotFoundError,
    DeputyPermissionError,
    DeputyRateLimitError,
    DeputyRegionError,
)
from deputy_mcp.config import DeputyConfig

if TYPE_CHECKING:
    from deputy_mcp.token_store import OAuthTokens, TokenStore

#: HTTP statuses that are safe to retry (throttling + transient upstream).
_RETRY_STATUSES = frozenset({429, 502, 503, 504})

#: Methods that are idempotent by definition, so replaying them cannot double-apply a
#: mutation. Deputy models every write as a POST, so a bare POST is treated as
#: non-idempotent and is never retried; read POSTs (the ``/QUERY`` DSL) opt back in by
#: passing ``idempotent=True`` explicitly.
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Sentinel distinguishing a cache miss from a cached ``None`` value.
_MISS: Any = object()


class DeputyHTTP:
    """Authenticated, retrying, cache-aware transport for one Deputy install."""

    #: Base back-off in seconds for the first retry (full-jitter window).
    BASE_BACKOFF = 1.0
    #: Upper bound (seconds) on any single back-off wait.
    MAX_BACKOFF = 30.0
    #: Hard cap on cached entries so a long-lived server cannot grow the cache
    #: without bound (many distinct QUERY bodies over a session).
    MAX_CACHE_ENTRIES = 512

    def __init__(
        self,
        config: DeputyConfig,
        *,
        oauth_tokens: OAuthTokens | None = None,
        token_store: TokenStore | None = None,
    ) -> None:
        """Build a transport for one install.

        In static-token mode (neither ``oauth_tokens`` nor ``token_store``) the base URL
        and bearer token come straight from ``config``. In OAuth mode they come from
        ``oauth_tokens`` or, when only ``token_store`` is given, from the store on the
        first request; the transport refreshes and persists them on expiry. Either way
        the install host is checked against the allowlist before it is used.
        """
        self._config = config
        self._oauth_tokens: OAuthTokens | None = None
        self._token_store = token_store
        # Serialize token loading and refresh so concurrent requests read the store once
        # and concurrent 401s mint (and persist) only one new token.
        self._refresh_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            headers={"Accept": "application/json", "User-Agent": "deputy-mcp"},
            timeout=config.timeout,
        )
        if oauth_tokens is not None:
            self._apply_tokens(oauth_tokens)
        elif token_store is None:
            self._client.base_url = httpx.URL(config.api_url)
            self._client.headers["Authorization"] = f"Bearer {config.token()}"
        self._cache: dict[str, tuple[float, Any]] = {}

    @property
    def config(self) -> DeputyConfig:
        """The configuration this transport was built from."""
        return self._config

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
        cacheable: bool = False,
        idempotent: bool = False,
    ) -> Any:
        """Perform a request and return parsed JSON (or ``None`` if empty).

        Args:
            method: HTTP method, e.g. ``"GET"`` or ``"POST"``.
            path: Path relative to the ``/api/v1`` base, e.g. ``"/me"``.
            json_body: Optional JSON-serializable request body.
            params: Optional query-string parameters.
            cacheable: When true (read paths), serve from / store in the TTL
                cache. Ignored when ``cache_ttl`` is 0.
            idempotent: When true, the request is safe to replay, so it is retried
                on throttling/transient failures even if it is a POST (used by the
                read-only ``/QUERY`` DSL). ``GET``/``HEAD``/``OPTIONS`` are always
                treated as idempotent; a bare write POST is not, so it is never
                retried (retrying a write could double-apply the mutation).

        Raises:
            DeputyError: A mapped subclass on any non-2xx response, transport
                failure, or connection/DNS error.
        """
        retryable = idempotent or method.upper() in _IDEMPOTENT_METHODS
        use_cache = cacheable and self._config.cache_ttl > 0
        key = _cache_key(method, path, params, json_body) if use_cache else None
        if key is not None:
            hit = self._cache_get(key)
            if hit is not _MISS:
                return hit

        # OAuth only: load the stored tokens on first use, then pre-emptively refresh a
        # token that is expired (or within the skew window) so we do not spend a
        # round-trip earning a predictable 401.
        if self._token_store is not None and self._oauth_tokens is None:
            await self._load_stored_tokens()
        tokens = self._oauth_tokens
        if tokens is not None and tokens.is_expired():
            await self._refresh_tokens(tokens.access_token)

        try:
            return await self._send_with_retries(method, path, json_body, params, retryable, key)
        except DeputyAuthError:
            # Static mode: a 401 is a genuine bad/expired token — surface it as-is.
            current = self._oauth_tokens
            if current is None:
                raise
            # OAuth: the access token may have expired mid-flight. Refresh once and
            # replay the request exactly once. A second 401 propagates (no loop).
            await self._refresh_tokens(current.access_token)
            return await self._send_with_retries(method, path, json_body, params, retryable, key)

    async def _send_with_retries(
        self,
        method: str,
        path: str,
        json_body: Any | None,
        params: dict[str, Any] | None,
        retryable: bool,
        key: str | None,
    ) -> Any:
        """Send one logical request, retrying throttling/transient transport errors.

        This is the unchanged static-mode request loop, extracted so the OAuth
        refresh-and-replay wrapper in :meth:`request` can invoke it twice at most.
        """
        attempt = 0
        while True:
            try:
                response = await self._client.request(method, path, json=json_body, params=params)
            except httpx.ConnectError as exc:
                # DNS failure / refused connection: almost always a wrong
                # install name or geo. Surface it as a region error, no retry.
                raise DeputyRegionError() from exc
            except httpx.TransportError as exc:
                # Timeouts and other network blips: retry idempotent requests, then
                # give up. A non-idempotent write is NOT retried -- the timeout may
                # have masked a response for a mutation Deputy already applied, so a
                # blind replay could create a duplicate (e.g. a second clock-in).
                if retryable and attempt < self._config.max_retries:
                    await self._sleep(self._backoff_delay(attempt, None))
                    attempt += 1
                    continue
                raise DeputyAPIError(
                    "Network error while contacting Deputy.",
                    body=str(exc),
                ) from exc

            status = response.status_code
            if status < 400:
                data = _parse_response(response)
                if key is not None:
                    self._cache_set(key, data)
                return data

            # Only replay idempotent requests on a retryable status. A write POST
            # that draws a 429/503 mid-processing must not be re-sent blindly, since
            # Deputy may already have applied it (duplicate mutation otherwise).
            if status in _RETRY_STATUSES and retryable and attempt < self._config.max_retries:
                await self._sleep(self._backoff_delay(attempt, _retry_after(response)))
                attempt += 1
                continue

            raise _map_error(response)

    async def _load_stored_tokens(self) -> None:
        """Adopt the token store's tokens, read off the event loop (OAuth mode only).

        A keychain read is a blocking OS call, so it runs in a worker thread. The store is
        consulted again on every request until a token exists, which is what lets a
        ``deputy-mcp login`` performed while the server runs take effect immediately.
        """
        store = self._token_store
        if store is None:
            return
        async with self._refresh_lock:
            if self._oauth_tokens is not None:
                return  # a concurrent request loaded them first
            stored = await asyncio.to_thread(store.load)
            if stored is None:
                raise DeputyAuthError(
                    "Not signed in to Deputy: no OAuth token is stored. Run "
                    "'deputy-mcp login' first.",
                    hint=(
                        "Register an app at https://once.deputy.com/my/oauth_clients "
                        "(redirect http://localhost:8823/callback), set "
                        "DEPUTY_OAUTH_CLIENT_ID and DEPUTY_OAUTH_CLIENT_SECRET, then run "
                        "'deputy-mcp login'."
                    ),
                    status_code=None,
                )
            self._apply_tokens(stored)

    async def _refresh_tokens(self, stale_access_token: str) -> None:
        """Mint, persist and apply a fresh OAuth access token (OAuth mode only).

        Serialized by ``_refresh_lock``: if another task already rotated the token
        since ``stale_access_token`` was captured, this is a no-op so a single 401
        storm does not burn several refresh-token round-trips.

        Deputy rotates the refresh token on every refresh, so two *processes* sharing
        one store (the MCP server and a CLI call, or a fresh ``deputy-mcp login``) must
        not both spend the same refresh token. Before refreshing, the store is re-read:
        a newer, unexpired token another process saved is adopted instead. On failure a
        :class:`DeputyAuthError` is raised pointing the user at ``deputy-mcp login``;
        no token or secret is ever included in the message.
        """
        from deputy_mcp import oauth

        async with self._refresh_lock:
            current = self._oauth_tokens
            if current is None or current.access_token != stale_access_token:
                # Not OAuth, or a concurrent refresh already replaced the token.
                return
            if self._token_store is not None:
                stored = await asyncio.to_thread(self._token_store.load)
                if (
                    stored is not None
                    and stored.access_token != current.access_token
                    and stored.base_url == current.base_url
                    and not stored.is_expired()
                ):
                    self._apply_tokens(stored)
                    return
            client_id = (self._config.oauth_client_id or "").strip()
            if not client_id or self._config.oauth_client_secret is None:
                raise DeputyAuthError(
                    "Cannot refresh the Deputy OAuth token: client credentials are missing.",
                    hint=(
                        "Set DEPUTY_OAUTH_CLIENT_ID and DEPUTY_OAUTH_CLIENT_SECRET, then "
                        "run 'deputy-mcp login' again."
                    ),
                )
            client_secret = self._config.oauth_client_secret_value()
            try:
                async with httpx.AsyncClient(timeout=self._config.timeout) as http:
                    new_tokens = await oauth.refresh(
                        http,
                        client_id,
                        client_secret,
                        current.refresh_token,
                        base_url=current.base_url,
                        redirect_uri=oauth.redirect_uri_for(self._config.redirect_port),
                        allow_custom_host=self._config.allow_custom_host,
                    )
            except DeputyAuthError:
                raise
            except DeputyError as exc:
                # A non-auth failure (network/shape): still actionable via re-login.
                raise DeputyAuthError(
                    f"Could not refresh the Deputy OAuth token: {exc.message}",
                    hint="Run 'deputy-mcp login' again.",
                ) from exc
            # The old refresh token is already spent: adopt the new pair in memory first so
            # this process keeps working even if persisting it then fails (that failure
            # still propagates, because the next process would otherwise be signed out).
            self._apply_tokens(new_tokens)
            if self._token_store is not None:
                await asyncio.to_thread(self._token_store.save, new_tokens)

    def _apply_tokens(self, tokens: OAuthTokens) -> None:
        """Switch the transport to ``tokens`` for every subsequent request.

        The single place an OAuth install host becomes the request target, so it is also
        where that host is checked against the ``*.deputy.com`` allowlist: tokens read
        from a tampered keychain entry or token file cannot send the bearer elsewhere.
        """
        from deputy_mcp.oauth import require_deputy_host

        require_deputy_host(tokens.base_url, allow_custom_host=self._config.allow_custom_host)
        self._oauth_tokens = tokens
        self._client.base_url = httpx.URL(f"{tokens.base_url}/api/v1")
        self._client.headers["Authorization"] = f"Bearer {tokens.access_token}"

    def invalidate(self) -> None:
        """Clear the entire read cache (called after every write)."""
        self._cache.clear()

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> DeputyHTTP:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # -- retry / back-off ---------------------------------------------------

    async def _sleep(self, delay: float) -> None:
        """Sleep for ``delay`` seconds (separated for test seams)."""
        await asyncio.sleep(delay)

    def _backoff_delay(self, attempt: int, retry_after: float | None) -> float:
        """Compute the next back-off wait.

        Honors a server-provided ``Retry-After`` (capped), otherwise uses
        exponential back-off with full jitter: a random point in ``[0, cap]``
        where ``cap = min(MAX_BACKOFF, BASE_BACKOFF * 2**attempt)``.
        """
        if retry_after is not None:
            return min(max(retry_after, 0.0), self.MAX_BACKOFF)
        cap = min(self.MAX_BACKOFF, self.BASE_BACKOFF * (2**attempt))
        return random.uniform(0.0, cap)

    # -- cache --------------------------------------------------------------

    def _cache_get(self, key: str) -> Any:
        entry = self._cache.get(key)
        if entry is None:
            return _MISS
        stored_at, value = entry
        if time.monotonic() - stored_at >= self._config.cache_ttl:
            self._cache.pop(key, None)
            return _MISS
        return value

    def _sweep_expired(self) -> None:
        """Drop every cache entry whose TTL has already elapsed."""
        ttl = self._config.cache_ttl
        now = time.monotonic()
        for key in [k for k, (stored_at, _) in self._cache.items() if now - stored_at >= ttl]:
            del self._cache[key]

    def _cache_set(self, key: str, value: Any) -> None:
        # Bound memory: when at capacity and inserting a NEW key, reclaim room by
        # first sweeping expired entries, then evicting the oldest by insertion order
        # (dict preserves insertion order). Overwriting an existing key never grows it.
        if key not in self._cache and len(self._cache) >= self.MAX_CACHE_ENTRIES:
            self._sweep_expired()
            while len(self._cache) >= self.MAX_CACHE_ENTRIES:
                del self._cache[next(iter(self._cache))]
        self._cache[key] = (time.monotonic(), value)


def _cache_key(
    method: str,
    path: str,
    params: dict[str, Any] | None,
    json_body: Any | None,
) -> str:
    """Build a stable cache key from the request shape (never the token)."""
    payload = json.dumps(
        {"m": method.upper(), "p": path, "q": params, "b": json_body},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_response(response: httpx.Response) -> Any:
    """Parse a successful response body as JSON, tolerating empty/non-JSON."""
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def _retry_after(response: httpx.Response) -> float | None:
    """Read a ``Retry-After`` header as seconds, ignoring HTTP-date form."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw.strip())
    except ValueError:
        return None


def _body_snippet(response: httpx.Response) -> str | None:
    """Return a short, header-free snippet of the response body for context."""
    text = (response.text or "").strip()
    return text or None


def _map_error(response: httpx.Response) -> DeputyError:
    """Map a non-2xx response to the appropriate :class:`DeputyError`."""
    status = response.status_code
    if status == 401:
        return DeputyAuthError()
    if status == 403:
        return DeputyPermissionError()
    if status == 404:
        return DeputyNotFoundError()
    if status == 429:
        return DeputyRateLimitError(retry_after=_retry_after(response))
    return DeputyAPIError(
        f"Deputy API returned HTTP {status}.",
        status_code=status,
        body=_body_snippet(response),
    )


__all__ = ["DeputyHTTP"]
