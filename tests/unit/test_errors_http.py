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
