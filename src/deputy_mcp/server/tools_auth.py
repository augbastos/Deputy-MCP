"""The ``deputy_login`` tool: OAuth sign-in from inside an MCP client.

``deputy-mcp login`` needs a terminal, which a Claude Desktop bundle user may never
open. This tool runs the same loopback Authorization Code flow from the server: it
opens the browser, waits for Deputy's callback, and stores the tokens where the CLI
would. The client reads the token store on every request, so the next tool call is
signed in without a restart.

It is registered only when OAuth client credentials are configured. The flow prints
progress with ``print``; on stdio that would corrupt the protocol channel, so stdout
is redirected to stderr for its duration. No token is ever returned to the model.
"""

from __future__ import annotations

import contextlib
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from deputy_mcp import oauth
from deputy_mcp.client.errors import DeputyError
from deputy_mcp.sanitize import redact
from deputy_mcp.token_store import resolve_token_store

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from deputy_mcp.config import DeputyConfig

__all__ = ["register"]


def register(mcp: FastMCP[Any], config: DeputyConfig) -> None:
    """Register ``deputy_login`` onto ``mcp``."""
    from mcp.types import ToolAnnotations

    @mcp.tool(
        name="deputy_login",
        annotations=ToolAnnotations(
            title="Sign in to Deputy",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    )
    async def deputy_login() -> str:
        """Sign the user in to Deputy with OAuth by opening their browser.

        Use when another tool reports that no Deputy token is stored, or that the
        stored token expired and could not be refreshed. The user approves access in
        the browser window that opens; the call waits for that and then stores the
        token in the OS keychain. Do not call it pre-emptively.

        Returns a short confirmation with the token expiry, or why sign-in failed.
        """
        try:
            with contextlib.redirect_stdout(sys.stderr):
                tokens = await oauth.run_login_flow(config, open_browser=True)
            store = resolve_token_store(config.token_store_path)
            store.save(tokens)
        except DeputyError as exc:
            lines = [f"Deputy sign-in did not complete: {exc.message}"]
            if exc.hint:
                lines.append(f"Hint: {exc.hint}")
            return redact("\n".join(lines))
        expires = datetime.fromtimestamp(tokens.expires_at, tz=UTC)
        return (
            f"Signed in to Deputy ({tokens.base_url}). Token stored in {store.location}; "
            f"it expires {expires:%Y-%m-%d %H:%M} UTC and refreshes automatically."
        )
