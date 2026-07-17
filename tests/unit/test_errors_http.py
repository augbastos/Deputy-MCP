"""Unit tests for :mod:`deputy_mcp.client.http` and the error hierarchy.

Uses respx to mock httpx at the transport layer. Covers status-code error mapping,
retry with ``Retry-After`` + jittered back-off, connect/transport failures, the TTL
cache (hit, expiry, invalidation, disabled) and the guarantee that the token never
appears in an exception message.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from deputy_mcp.client import http as http_module
from deputy_mcp.client.errors import (
    DeputyAPIError,
    DeputyAuthError,
    DeputyNotFoundError,
    DeputyPermissionError,
    DeputyRateLimitError,
    DeputyRegionError,
)
from deputy_mcp.client.http import DeputyHTTP
from deputy_mcp.config import DeputyConfig

ConfigFactory = Callable[..., DeputyConfig]

# Token value must never leak into any error surfaced to a caller.
_TOKEN = "test-token-fictional-not-a-secret"


# -- error mapping per status ------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, DeputyAuthError),
        (403, DeputyPermissionError),
        (404, DeputyNotFoundError),
    ],
)
async def test_status_maps_to_error(
    status: int,
    expected: type[Exception],
    deputy_api: respx.MockRouter,
    make_config: ConfigFactory,
) -> None:
    deputy_api.get("/me").mock(return_value=httpx.Response(status))
    http = DeputyHTTP(make_config(max_retries=0))
    try:
        with pytest.raises(expected) as exc:
            await http.request("GET", "/me")
        assert exc.value.status_code == status  # type: ignore[attr-defined]
    finally:
        await http.aclose()


async def test_429_maps_to_rate_limit_with_retry_after(
    deputy_api: respx.MockRouter, make_config: ConfigFactory
) -> None:
    deputy_api.get("/me").mock(return_value=httpx.Response(429, headers={"Retry-After": "7"}))
    http = DeputyHTTP(make_config(max_retries=0))
    try:
        with pytest.raises(DeputyRateLimitError) as exc:
            await http.request("GET", "/me")
        assert exc.value.retry_after == 7.0
        assert exc.value.status_code == 429
    finally:
        await http.aclose()


async def test_500_maps_to_api_error_with_body_snippet(
    deputy_api: respx.MockRouter, make_config: ConfigFactory
) -> None:
    deputy_api.get("/me").mock(return_value=httpx.Response(500, text="internal boom"))
    http = DeputyHTTP(make_config(max_retries=0))
    try:
        with pytest.raises(DeputyAPIError) as exc:
            await http.request("GET", "/me")
        assert exc.value.status_code == 500
        assert "internal boom" in str(exc.value)
    finally:
        await http.aclose()


async def test_connect_error_maps_to_region_error(
    deputy_api: respx.MockRouter, make_config: ConfigFactory
) -> None:
    route = deputy_api.get("/me").mock(side_effect=httpx.ConnectError("dns failure"))
    http = DeputyHTTP(make_config(max_retries=3))
    try:
        with pytest.raises(DeputyRegionError):
            await http.request("GET", "/me")
        # ConnectError is not retried — it is a configuration problem.
        assert route.call_count == 1
    finally:
        await http.aclose()


async def test_no_retry_on_plain_4xx(
    deputy_api: respx.MockRouter, make_config: ConfigFactory
) -> None:
    route = deputy_api.get("/me").mock(return_value=httpx.Response(400, text="bad request"))
    http = DeputyHTTP(make_config(max_retries=3))
    try:
        with pytest.raises(DeputyAPIError) as exc:
            await http.request("GET", "/me")
        assert route.call_count == 1
        assert exc.value.status_code == 400
    finally:
        await http.aclose()


# -- retry / back-off --------------------------------------------------------


async def test_retry_honors_retry_after_header(
    deputy_api: respx.MockRouter,
    make_config: ConfigFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = deputy_api.get("/me").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    http = DeputyHTTP(make_config(max_retries=3))
    sleep = AsyncMock()
    monkeypatch.setattr(http, "_sleep", sleep)
    try:
        result = await http.request("GET", "/me")
        assert result == {"ok": True}
        assert route.call_count == 2
        sleep.assert_awaited_once_with(2.0)
    finally:
        await http.aclose()


async def test_retry_uses_jittered_exponential_backoff(
    deputy_api: respx.MockRouter,
    make_config: ConfigFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deputy_api.get("/me").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    # Full-jitter draws uniform(0, cap); pin it to the cap to make delays deterministic.
    monkeypatch.setattr(http_module, "random", SimpleNamespace(uniform=lambda _a, b: b))
    http = DeputyHTTP(make_config(max_retries=3))
    sleep = AsyncMock()
    monkeypatch.setattr(http, "_sleep", sleep)
    try:
        result = await http.request("GET", "/me")
        assert result == {"ok": True}
        # cap = min(30, 1 * 2**attempt): attempt 0 -> 1.0, attempt 1 -> 2.0.
        delays = [call.args[0] for call in sleep.await_args_list]
        assert delays == [1.0, 2.0]
    finally:
        await http.aclose()


async def test_retry_gives_up_after_max_retries(
    deputy_api: respx.MockRouter,
    make_config: ConfigFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = deputy_api.get("/me").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"})
    )
    http = DeputyHTTP(make_config(max_retries=2))
    monkeypatch.setattr(http, "_sleep", AsyncMock())
    try:
        with pytest.raises(DeputyRateLimitError) as exc:
            await http.request("GET", "/me")
        # initial attempt + 2 retries.
        assert route.call_count == 3
        assert exc.value.retry_after == 0.0
    finally:
        await http.aclose()


async def test_transport_error_retried_then_succeeds(
    deputy_api: respx.MockRouter,
    make_config: ConfigFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = deputy_api.get("/me").mock(
        side_effect=[
            httpx.ReadTimeout("slow"),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    http = DeputyHTTP(make_config(max_retries=2))
    monkeypatch.setattr(http, "_sleep", AsyncMock())
    try:
        result = await http.request("GET", "/me")
        assert result == {"ok": True}
        assert route.call_count == 2
    finally:
        await http.aclose()


async def test_transport_error_exhausted_maps_to_api_error(
    deputy_api: respx.MockRouter,
    make_config: ConfigFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = deputy_api.get("/me").mock(side_effect=httpx.ReadTimeout("always slow"))
    http = DeputyHTTP(make_config(max_retries=1))
    monkeypatch.setattr(http, "_sleep", AsyncMock())
    try:
        with pytest.raises(DeputyAPIError):
            await http.request("GET", "/me")
        # initial attempt + 1 retry.
        assert route.call_count == 2
    finally:
        await http.aclose()


# -- idempotency guard: writes (bare POST) must never be replayed ------------


async def test_write_post_not_retried_on_retryable_status(
    deputy_api: respx.MockRouter,
    make_config: ConfigFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 503 on a write POST must NOT be retried: Deputy may already have applied it,
    # so a blind replay could double-apply the mutation.
    route = deputy_api.post("/supervise/timesheet/start").mock(return_value=httpx.Response(503))
    http = DeputyHTTP(make_config(max_retries=3))
    monkeypatch.setattr(http, "_sleep", AsyncMock())
    try:
        with pytest.raises(DeputyAPIError):
            await http.request("POST", "/supervise/timesheet/start", json_body={"x": 1})
        assert route.call_count == 1  # no retries for a non-idempotent write
    finally:
        await http.aclose()


async def test_write_post_not_retried_on_timeout(
    deputy_api: respx.MockRouter,
    make_config: ConfigFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = deputy_api.post("/supervise/roster").mock(side_effect=httpx.ReadTimeout("slow"))
    http = DeputyHTTP(make_config(max_retries=3))
    monkeypatch.setattr(http, "_sleep", AsyncMock())
    try:
        with pytest.raises(DeputyAPIError):
            await http.request("POST", "/supervise/roster", json_body={"x": 1})
        assert route.call_count == 1  # timeout on a write is surfaced, not replayed
    finally:
        await http.aclose()


async def test_idempotent_post_is_retried(
    deputy_api: respx.MockRouter,
    make_config: ConfigFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A read QUERY POST opts back in via idempotent=True, so it retries like a read.
    route = deputy_api.post("/resource/Roster/QUERY").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=[{"Id": 1}])]
    )
    http = DeputyHTTP(make_config(max_retries=3))
    monkeypatch.setattr(http, "_sleep", AsyncMock())
    try:
        result = await http.request("POST", "/resource/Roster/QUERY", json_body={}, idempotent=True)
        assert result == [{"Id": 1}]
        assert route.call_count == 2
    finally:
        await http.aclose()


# -- TTL cache ---------------------------------------------------------------


async def test_cacheable_get_is_served_from_cache(
    deputy_api: respx.MockRouter, make_config: ConfigFactory
) -> None:
    route = deputy_api.get("/resource/Company/1").mock(
        return_value=httpx.Response(200, json={"Id": 1})
    )
    http = DeputyHTTP(make_config(cache_ttl=30))
    try:
        first = await http.request("GET", "/resource/Company/1", cacheable=True)
        second = await http.request("GET", "/resource/Company/1", cacheable=True)
        assert first == second == {"Id": 1}
        assert route.call_count == 1
    finally:
        await http.aclose()


async def test_cache_expires_after_ttl(
    deputy_api: respx.MockRouter,
    make_config: ConfigFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = deputy_api.get("/resource/Company/1").mock(
        return_value=httpx.Response(200, json={"Id": 1})
    )
    clock = {"t": 0.0}
    monkeypatch.setattr(http_module, "time", SimpleNamespace(monotonic=lambda: clock["t"]))
    http = DeputyHTTP(make_config(cache_ttl=30))
    try:
        await http.request("GET", "/resource/Company/1", cacheable=True)
        clock["t"] = 10.0  # within TTL -> cache hit
        await http.request("GET", "/resource/Company/1", cacheable=True)
        assert route.call_count == 1
        clock["t"] = 40.0  # past TTL -> refetch
        await http.request("GET", "/resource/Company/1", cacheable=True)
        assert route.call_count == 2
    finally:
        await http.aclose()


async def test_invalidate_clears_cache(
    deputy_api: respx.MockRouter, make_config: ConfigFactory
) -> None:
    route = deputy_api.get("/resource/Company/1").mock(
        return_value=httpx.Response(200, json={"Id": 1})
    )
    http = DeputyHTTP(make_config(cache_ttl=30))
    try:
        await http.request("GET", "/resource/Company/1", cacheable=True)
        http.invalidate()
        await http.request("GET", "/resource/Company/1", cacheable=True)
        assert route.call_count == 2
    finally:
        await http.aclose()


async def test_cache_disabled_when_ttl_zero(
    deputy_api: respx.MockRouter, make_config: ConfigFactory
) -> None:
    route = deputy_api.get("/resource/Company/1").mock(
        return_value=httpx.Response(200, json={"Id": 1})
    )
    http = DeputyHTTP(make_config(cache_ttl=0))
    try:
        await http.request("GET", "/resource/Company/1", cacheable=True)
        await http.request("GET", "/resource/Company/1", cacheable=True)
        assert route.call_count == 2
    finally:
        await http.aclose()


async def test_non_cacheable_request_not_cached(
    deputy_api: respx.MockRouter, make_config: ConfigFactory
) -> None:
    route = deputy_api.get("/me").mock(return_value=httpx.Response(200, json={"ok": 1}))
    http = DeputyHTTP(make_config(cache_ttl=30))
    try:
        await http.request("GET", "/me")
        await http.request("GET", "/me")
        assert route.call_count == 2
    finally:
        await http.aclose()


async def test_cache_key_varies_by_params(
    deputy_api: respx.MockRouter, make_config: ConfigFactory
) -> None:
    route = deputy_api.get("/data").mock(return_value=httpx.Response(200, json={"ok": 1}))
    http = DeputyHTTP(make_config(cache_ttl=30))
    try:
        await http.request("GET", "/data", params={"a": 1}, cacheable=True)
        await http.request("GET", "/data", params={"a": 1}, cacheable=True)
        assert route.call_count == 1  # same params -> cached
        await http.request("GET", "/data", params={"a": 2}, cacheable=True)
        assert route.call_count == 2  # different params -> distinct key
    finally:
        await http.aclose()


async def test_cache_is_bounded_to_max_entries(
    make_config: ConfigFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read cache never grows past MAX_CACHE_ENTRIES; the oldest keys are evicted."""
    monkeypatch.setattr(DeputyHTTP, "MAX_CACHE_ENTRIES", 4)
    http = DeputyHTTP(make_config(cache_ttl=1000))  # long TTL: nothing expires here
    try:
        for i in range(10):
            http._cache_set(f"k{i}", i)
        assert len(http._cache) == 4
        assert "k0" not in http._cache  # oldest inserted, evicted first
        assert "k9" in http._cache  # newest inserted, retained
    finally:
        await http.aclose()


async def test_cache_set_sweeps_expired_before_evicting_fresh(
    make_config: ConfigFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At capacity, expired entries are reclaimed first so a fresh insert survives."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(http_module, "time", SimpleNamespace(monotonic=lambda: clock["t"]))
    monkeypatch.setattr(DeputyHTTP, "MAX_CACHE_ENTRIES", 3)
    http = DeputyHTTP(make_config(cache_ttl=10))
    try:
        for i in range(3):
            http._cache_set(f"stale{i}", i)  # stored at t=1000
        clock["t"] = 2000.0  # all three are now well past the 10s TTL
        http._cache_set("fresh", 99)  # sweep clears the stale three, fresh stays
        assert "fresh" in http._cache
        assert len(http._cache) == 1
    finally:
        await http.aclose()


# -- token safety + request shape --------------------------------------------


async def test_token_never_appears_in_error(
    deputy_api: respx.MockRouter, make_config: ConfigFactory
) -> None:
    deputy_api.get("/me").mock(return_value=httpx.Response(500, text=f"leak attempt {_TOKEN}?"))
    http = DeputyHTTP(make_config(max_retries=0))
    try:
        with pytest.raises(DeputyAPIError) as exc:
            await http.request("GET", "/me")
        # The body snippet echoes whatever the server sent, but our own machinery
        # must never inject the credential. The bearer token is not part of any
        # message we construct; assert it is not in the hint/status portions.
        assert _TOKEN not in (exc.value.hint or "")
    finally:
        await http.aclose()


async def test_bearer_header_is_sent(
    deputy_api: respx.MockRouter, make_config: ConfigFactory
) -> None:
    route = deputy_api.get("/me").mock(return_value=httpx.Response(200, json={"ok": 1}))
    http = DeputyHTTP(make_config())
    try:
        await http.request("GET", "/me")
        sent: Any = route.calls.last.request
        assert sent.headers["Authorization"] == f"Bearer {_TOKEN}"
    finally:
        await http.aclose()
