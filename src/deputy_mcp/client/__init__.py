"""Deputy API client package.

Public entry point for talking to a Deputy install without any MCP dependency,
so the same client is reusable in scripts, webhooks and the CLI. The read and
write surfaces live in separate mixins (:class:`~deputy_mcp.client.reads.ReadsMixin`,
:class:`~deputy_mcp.client.writes.WritesMixin`) that :class:`DeputyClient`
composes over the shared HTTP transport.

This module imports nothing from :mod:`deputy_mcp.server` — the client layer is
MCP-free by design.
"""

from __future__ import annotations

from types import TracebackType

from deputy_mcp.client.errors import (
    DeputyAPIError,
    DeputyAuthError,
    DeputyConfigError,
    DeputyError,
    DeputyNotFoundError,
    DeputyPermissionError,
    DeputyRateLimitError,
    DeputyRegionError,
    DeputyWritesDisabledError,
)
from deputy_mcp.client.http import DeputyHTTP
from deputy_mcp.client.models import (
    Company,
    Contact,
    DeputyModel,
    Employee,
    OperationalUnit,
    Roster,
    RosterSwap,
    Timesheet,
    Unavailability,
    WhoAmI,
)
from deputy_mcp.client.reads import ReadsMixin
from deputy_mcp.client.writes import WritesMixin
from deputy_mcp.config import DeputyConfig

__all__ = [
    "Company",
    "Contact",
    "DeputyAPIError",
    "DeputyAuthError",
    "DeputyClient",
    "DeputyConfig",
    "DeputyConfigError",
    "DeputyError",
    "DeputyModel",
    "DeputyNotFoundError",
    "DeputyPermissionError",
    "DeputyRateLimitError",
    "DeputyRegionError",
    "DeputyWritesDisabledError",
    "Employee",
    "OperationalUnit",
    "Roster",
    "RosterSwap",
    "Timesheet",
    "Unavailability",
    "WhoAmI",
]


class DeputyClient(ReadsMixin, WritesMixin):
    """A high-level, async Deputy API client.

    Composes the read and write mixins over a single :class:`DeputyHTTP`
    transport bound to one install. Construct from explicit config, or from the
    environment with :meth:`from_env`, and use it as an async context manager so
    the underlying HTTP client is always closed::

        async with DeputyClient.from_env() as client:
            me = await client.whoami()

    The caller's own employee id (resolved from ``whoami``) is cached after the
    first lookup and reused by ``next_shift``/write operations that act "as me".
    """

    def __init__(self, config: DeputyConfig) -> None:
        """Build a client for one install from validated configuration."""
        self._config = config
        self._http = DeputyHTTP(config)
        self._own_employee_id: int | None = None

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> DeputyClient:
        """Build a client from ``DEPUTY_*`` environment variables (fails closed)."""
        return cls(DeputyConfig.from_env(environ))

    @property
    def config(self) -> DeputyConfig:
        """The configuration this client was built from."""
        return self._config

    async def aclose(self) -> None:
        """Close the underlying HTTP transport."""
        await self._http.aclose()

    async def __aenter__(self) -> DeputyClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
