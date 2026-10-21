"""``deputy_login``: present only with OAuth credentials, stdout-clean, stores tokens."""

from __future__ import annotations

import time

import pytest
from fastmcp import Client

from deputy_mcp import oauth
from deputy_mcp.client.errors import DeputyAuthError
from deputy_mcp.config import DeputyConfig
from deputy_mcp.server import create_server
from deputy_mcp.token_store import OAuthTokens, resolve_token_store

from . import tool_text

_ORIGIN = "https://acme.eu.deputy.com"


def _store_path():  # type: ignore[no-untyped-def]
    return DeputyConfig.from_env().token_store_path


@pytest.fixture
def oauth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEPUTY_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("DEPUTY_OAUTH_CLIENT_SECRET", "client-secret")


async def _tool_names() -> set[str]:
    async with Client(create_server()) as client:
        return {t.name for t in await client.list_tools()}


async def test_login_tool_absent_without_oauth_credentials(deputy_env: dict[str, str]) -> None:
    assert "deputy_login" not in await _tool_names()


async def test_login_tool_stores_tokens_and_keeps_stdout_clean(
    oauth_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tokens = OAuthTokens("acc-new", "ref-new", time.time() + 3600, base_url=_ORIGIN)

    async def fake_flow(config: object, *, open_browser: bool = True) -> OAuthTokens:
        assert open_browser
        print("Opening your browser to authorize Deputy MCP.")
        return tokens

    monkeypatch.setattr(oauth, "run_login_flow", fake_flow)
    async with Client(create_server()) as client:
        assert "deputy_login" in {t.name for t in await client.list_tools()}
        text = tool_text(await client.call_tool("deputy_login", {}))

    assert "Signed in to Deputy" in text
    assert "acc-new" not in text and "ref-new" not in text
    stored = resolve_token_store(_store_path(), "client-id").load()
    assert stored is not None and stored.access_token == "acc-new"
    captured = capsys.readouterr()
    assert "Opening your browser" not in captured.out
    assert "Opening your browser" in captured.err


async def test_login_tool_reports_failure_without_raising(
    oauth_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_flow(config: object, *, open_browser: bool = True) -> OAuthTokens:
        raise DeputyAuthError("Timed out waiting for the Deputy authorization callback.")

    monkeypatch.setattr(oauth, "run_login_flow", failing_flow)
    async with Client(create_server()) as client:
        text = tool_text(await client.call_tool("deputy_login", {}))

    assert text.startswith("Deputy sign-in did not complete: Timed out")
    assert resolve_token_store(_store_path(), "client-id").load() is None
