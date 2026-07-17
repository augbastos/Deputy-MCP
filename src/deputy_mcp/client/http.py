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
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from types import TracebackType
from typing import Any

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

#: HTTP statuses that are safe to retry (throttling + transient upstream).
_RETRY_STATUSES = frozenset({429, 502, 503, 504})

#: Sentinel distinguishing a cache miss from a cached ``None`` value.
_MISS: Any = object()


class DeputyHTTP:
    """Authenticated, retrying, cache-aware transport for one Deputy install."""

    #: Base back-off in seconds for the first retry (full-jitter window).
    BASE_BACKOFF = 1.0
    #: Upper bound (seconds) on any single back-off wait.
    MAX_BACKOFF = 30.0

    def __init__(self, config: DeputyConfig) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.api_url,
            headers={
                "Authorization": f"Bearer {config.token()}",
                "Accept": "application/json",
                "User-Agent": "deputy-mcp",
            },
            timeout=config.timeout,
        )
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
    ) -> Any:
        """Perform a request and return parsed JSON (or ``None`` if empty).

        Args:
            method: HTTP method, e.g. ``"GET"`` or ``"POST"``.
            path: Path relative to the ``/api/v1`` base, e.g. ``"/me"``.
            json_body: Optional JSON-serializable request body.
            params: Optional query-string parameters.
            cacheable: When true (read paths), serve from / store in the TTL
                cache. Ignored when ``cache_ttl`` is 0.

        Raises:
            DeputyError: A mapped subclass on any non-2xx response, transport
                failure, or connection/DNS error.
        """
        use_cache = cacheable and self._config.cache_ttl > 0
        key = _cache_key(method, path, params, json_body) if use_cache else None
        if key is not None:
            hit = self._cache_get(key)
            if hit is not _MISS:
                return hit

        attempt = 0
        while True:
            try:
                response = await self._client.request(method, path, json=json_body, params=params)
            except httpx.ConnectError as exc:
                # DNS failure / refused connection: almost always a wrong
                # install name or geo. Surface it as a region error, no retry.
                raise DeputyRegionError() from exc
            except httpx.TransportError as exc:
                # Timeouts and other network blips: retry, then give up.
                if attempt < self._config.max_retries:
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

            if status in _RETRY_STATUSES and attempt < self._config.max_retries:
                await self._sleep(self._backoff_delay(attempt, _retry_after(response)))
                attempt += 1
                continue

            raise _map_error(response)

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

    def _cache_set(self, key: str, value: Any) -> None:
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
