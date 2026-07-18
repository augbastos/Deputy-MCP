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

from datetime import date
from types import TracebackType
from typing import Any, Literal

from deputy_mcp.client.errors import (
    DeputyAPIError,
    DeputyAuthError,
    DeputyConfigError,
    DeputyError,
    DeputyFeedError,
    DeputyNotFoundError,
    DeputyPermissionError,
    DeputyRateLimitError,
    DeputyRegionError,
    DeputyWritesDisabledError,
)
from deputy_mcp.client.http import DeputyHTTP
from deputy_mcp.client.ical import IcalRosterSource, RosterEvent
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
from deputy_mcp.client.reads import EMPLOYEE_JOIN, ReadsMixin
from deputy_mcp.client.writes import WritesMixin
from deputy_mcp.config import DeputyConfig
from deputy_mcp.oauth import TokenStore

__all__ = [
    "EMPLOYEE_JOIN",
    "Company",
    "Contact",
    "DeputyAPIError",
    "DeputyAuthError",
    "DeputyClient",
    "DeputyConfig",
    "DeputyConfigError",
    "DeputyError",
    "DeputyFeedError",
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

    The client operates in one of two modes (see :attr:`DeputyConfig.mode`):

    * **api** — an authenticated :class:`~deputy_mcp.client.http.DeputyHTTP` transport
      backs every tool (identical to before iCal mode existed).
    * **ical** — no API token; an :class:`~deputy_mcp.client.ical.IcalRosterSource`
      reads the caller's own roster from their personal Deputy iCal feed. Only
      :meth:`get_my_roster` and :meth:`next_shift` (for self) work; every other,
      API-only method raises a clear :class:`~deputy_mcp.client.errors.DeputyError`
      naming the token that would unlock it. This is realised by ``_http`` raising when
      no transport exists, so any method that reaches for the API fails closed with one
      consistent message.
    """

    def __init__(self, config: DeputyConfig) -> None:
        """Build a client for one install from validated configuration.

        The credential path (:attr:`DeputyConfig.auth_kind`) decides the wiring:

        * **static** — the authenticated HTTP transport is created eagerly from the
          permanent token and base URL.
        * **oauth** — the token store is loaded; if it holds tokens the transport is
          built from them (with the store, so it can refresh + persist on expiry). If
          no store exists yet the transport is left unbuilt and ``_http`` raises a
          "run deputy-mcp login first" error on use — a tool call fails cleanly rather
          than crashing at construction.
        * **ical** — no transport (and no token) exists; an :class:`IcalRosterSource`
          is held instead and ``_http`` raises the iCal-only error on use.
        """
        self._config = config
        self._own_employee_id: int | None = None
        self._transport: DeputyHTTP | None = None
        self._ical_source: IcalRosterSource | None = None
        self._needs_login = False
        if config.auth_kind == "static":
            self._http = DeputyHTTP(config)
        elif config.auth_kind == "oauth":
            store = TokenStore(config.token_store_path)
            tokens = store.load()
            if tokens is None:
                # OAuth creds are set but no token has been minted yet. Defer the
                # failure to first use so constructing the client never crashes.
                self._needs_login = True
            else:
                self._http = DeputyHTTP(config, oauth_tokens=tokens, token_store=store)
        else:
            self._ical_source = IcalRosterSource(
                config.calendar_url_value(),
                timeout=config.timeout,
                max_retries=config.max_retries,
                cache_ttl=config.cache_ttl,
            )

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> DeputyClient:
        """Build a client from ``DEPUTY_*`` environment variables (fails closed)."""
        return cls(DeputyConfig.from_env(environ))

    @property
    def config(self) -> DeputyConfig:
        """The configuration this client was built from."""
        return self._config

    @property
    def mode(self) -> Literal["api", "ical"]:
        """The resolved operating mode (``api`` or ``ical``)."""
        return self._config.mode

    @property
    def _http(self) -> DeputyHTTP:
        """The authenticated transport, or a clear failure in iCal mode.

        Every API-only read/write reaches the network through this property. In iCal mode
        there is no transport, so accessing it raises the single, actionable "needs an API
        token" error — that is how the whole API surface fails closed without duplicating a
        guard in each method.
        """
        if self._transport is None:
            if self._needs_login:
                raise self._login_required_error()
            raise self._ical_only_error()
        return self._transport

    @_http.setter
    def _http(self, value: DeputyHTTP) -> None:
        self._transport = value

    def _login_required_error(self) -> DeputyError:
        """The error raised when the API is used in OAuth mode before ``login`` ran."""
        return DeputyError(
            "not signed in to Deputy: no OAuth token found — run 'deputy-mcp login' first",
            hint=(
                "Register an app at https://once.deputy.com/my/oauth_clients (redirect "
                "http://localhost:8823/callback), set DEPUTY_OAUTH_CLIENT_ID and "
                "DEPUTY_OAUTH_CLIENT_SECRET, then run 'deputy-mcp login'."
            ),
        )

    def _ical_only_error(self) -> DeputyError:
        """The error raised when an API-only capability is used in iCal mode."""
        return DeputyError(
            "this needs a Deputy API token (DEPUTY_API_TOKEN); iCal mode only exposes your roster",
            hint=(
                "Set DEPUTY_API_TOKEN (and DEPUTY_BASE_URL) to use the full Deputy API. "
                "iCal mode (DEPUTY_CALENDAR_URL) is read-only and limited to your own "
                "roster: get_my_roster and next_shift (for yourself)."
            ),
        )

    async def get_my_roster(self, start: date, end: date) -> list[Roster]:
        """Return the caller's own shifts within ``[start, end]`` (inclusive).

        In iCal mode this reads the personal calendar feed and maps each event to the same
        :class:`~deputy_mcp.client.models.Roster` shape the tools render, so output is
        identical regardless of source. In api mode it defers to the API-backed
        implementation.
        """
        source = self._ical_source
        if source is not None:
            events = await source.get_roster(start, end)
            return [_roster_from_event(event) for event in events]
        return await super().get_my_roster(start, end)

    async def next_shift(self, employee_id: int | None = None) -> Roster | None:
        """Return the next upcoming shift for an employee (self if ``employee_id`` is None).

        In iCal mode only *self* is available (the feed is the caller's own roster): a
        request for another employee raises the API-token error. In api mode it defers to
        the API-backed implementation.
        """
        source = self._ical_source
        if source is not None:
            if employee_id is not None:
                raise self._ical_only_error()
            event = await source.next()
            return _roster_from_event(event) if event is not None else None
        return await super().next_shift(employee_id)

    async def aclose(self) -> None:
        """Close whichever source backs this client (HTTP transport or iCal feed)."""
        if self._transport is not None:
            await self._transport.aclose()
        if self._ical_source is not None:
            await self._ical_source.aclose()

    async def __aenter__(self) -> DeputyClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


#: Seconds per hour, for deriving a shift's ``TotalTime`` from its start/end.
_SECONDS_PER_HOUR = 3600


def _roster_from_event(event: RosterEvent) -> Roster:
    """Map an iCal :class:`RosterEvent` onto the :class:`Roster` shape the tools render.

    The feed carries only start/end/title/location, so the derived Roster mirrors what
    ``/api/v1/my/roster`` returns as closely as the source allows: unix ``StartTime`` /
    ``EndTime``, the local business ``Date``, a computed ``TotalTime`` in hours, and the
    shift title kept in ``Comment`` and embedded as ``OperationalUnitObject`` (the area
    label) so renderers reading either survive. ``Open`` is ``False`` — an iCal event is
    an assigned shift on the caller's own calendar. Because a personal feed is always the
    subscriber's own roster, the employee join is set to "You" so renderers read the owner
    rather than "Unassigned". The stable ``UID`` is preserved as extra for traceability.
    """
    start_unix = int(event.start.timestamp())
    end_unix = int(event.end.timestamp()) if event.end is not None else None
    total_hours = (
        round((end_unix - start_unix) / _SECONDS_PER_HOUR, 2) if end_unix is not None else None
    )
    area_label = event.title or event.location
    data: dict[str, Any] = {
        "StartTime": start_unix,
        "EndTime": end_unix,
        "Date": event.day.isoformat(),
        "TotalTime": total_hours,
        "Open": False,
        "Comment": event.title or None,
        # A personal iCal feed is always the subscriber's own roster; label the person
        # "You" so the renderer never shows "Unassigned" for the caller's own shifts.
        EMPLOYEE_JOIN: {"DisplayName": "You"},
    }
    if area_label:
        data["OperationalUnitObject"] = {"OperationalUnitName": area_label}
    if event.uid:
        data["ICalUID"] = event.uid
    return Roster.model_validate(data)
