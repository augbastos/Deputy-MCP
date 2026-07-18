"""Error hierarchy for the Deputy client (leaf module, stdlib only).

Every failure surfaced to callers is a :class:`DeputyError` (or a subclass), so
MCP tools and the CLI can translate them into a single, actionable message
instead of leaking a raw traceback. Errors never carry request headers or the
API token; response bodies are truncated to a short snippet.

This module imports **nothing** from the ``deputy_mcp`` package. It is the leaf
that :mod:`deputy_mcp.config` (and, transitively, the client package) depends on,
so the error hierarchy can be imported without pulling in the client package —
that is what keeps ``config`` from importing ``client`` and breaks the historical
``config -> client.errors -> client -> http -> config`` import cycle.
"""

from __future__ import annotations

#: Maximum number of characters kept from an API response body on an error.
BODY_SNIPPET_LIMIT = 300


class DeputyError(Exception):
    """Base class for every error raised by the Deputy client.

    Args:
        message: Human-readable description of what went wrong.
        hint: Optional actionable next step (e.g. which env var to fix).
        status_code: Optional HTTP status associated with the failure.
    """

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message
        self.hint = hint
        self.status_code = status_code
        super().__init__(message)

    def __str__(self) -> str:
        if self.hint:
            return f"{self.message} {self.hint}"
        return self.message


class DeputyConfigError(DeputyError):
    """Configuration is missing or invalid (fail closed at startup)."""


class DeputyAuthError(DeputyError):
    """HTTP 401 — the token is invalid or expired."""

    def __init__(
        self,
        message: str = "Deputy rejected the API token (HTTP 401).",
        *,
        hint: str | None = (
            "The token is invalid or expired. Generate a new one under Business "
            "settings -> Integrations -> API access, then update DEPUTY_API_TOKEN."
        ),
        status_code: int | None = 401,
    ) -> None:
        super().__init__(message, hint=hint, status_code=status_code)


class DeputyPermissionError(DeputyError):
    """HTTP 403 — the authenticated user lacks permission for the action."""

    def __init__(
        self,
        message: str = "The Deputy user lacks permission for this action (HTTP 403).",
        *,
        hint: str | None = (
            "The token inherits the permissions of the Deputy user who created it. "
            "Ask an administrator to raise that user's access level."
        ),
        status_code: int | None = 403,
    ) -> None:
        super().__init__(message, hint=hint, status_code=status_code)


class DeputyNotFoundError(DeputyError):
    """HTTP 404 — the requested resource does not exist."""

    def __init__(
        self,
        message: str = "The requested Deputy resource was not found (HTTP 404).",
        *,
        hint: str | None = None,
        status_code: int | None = 404,
    ) -> None:
        super().__init__(message, hint=hint, status_code=status_code)


class DeputyRateLimitError(DeputyError):
    """HTTP 429 — Deputy is throttling requests.

    Deputy documents no rate limits, so this is discovered empirically. When the
    response carries a ``Retry-After`` header its value (in seconds) is exposed
    as :attr:`retry_after`.
    """

    def __init__(
        self,
        message: str = "Deputy is rate limiting requests (HTTP 429).",
        *,
        retry_after: float | None = None,
        hint: str | None = "Retry after a short back-off; reduce request concurrency.",
        status_code: int | None = 429,
    ) -> None:
        self.retry_after = retry_after
        super().__init__(message, hint=hint, status_code=status_code)


class DeputyRegionError(DeputyError):
    """DNS/connection failure — usually a wrong install name or geo."""

    def __init__(
        self,
        message: str = "Could not connect to the Deputy install.",
        *,
        hint: str | None = (
            "Check the install name and region in DEPUTY_BASE_URL. It must match "
            "the URL you see when logged in to Deputy, e.g. "
            "https://your-company.eu.deputy.com."
        ),
        status_code: int | None = None,
    ) -> None:
        super().__init__(message, hint=hint, status_code=status_code)


class DeputyWritesDisabledError(DeputyError):
    """A write was attempted while ``DEPUTY_ALLOW_WRITES`` is false."""

    def __init__(
        self,
        message: str = "Write operations are disabled.",
        *,
        hint: str | None = (
            "Set DEPUTY_ALLOW_WRITES=true to enable shift, timesheet and "
            "availability changes. Writes are opt-in for safety."
        ),
        status_code: int | None = None,
    ) -> None:
        super().__init__(message, hint=hint, status_code=status_code)


class DeputyFeedError(DeputyError):
    """The iCal roster feed (``DEPUTY_CALENDAR_URL``) could not be fetched.

    Raised in iCal mode when the personal calendar feed cannot be retrieved (network
    failure, or an HTTP error from Deputy). The feed URL carries a token and is NEVER
    included in the message, hint, or body.
    """

    def __init__(
        self,
        message: str = "Could not read your Deputy iCal roster feed.",
        *,
        hint: str | None = (
            "Check DEPUTY_CALENDAR_URL is your current Deputy calendar link "
            "(Deputy -> My Schedule -> subscribe/export calendar). If you regenerated "
            "the link, copy the new one; the old feed URL stops working."
        ),
        status_code: int | None = None,
    ) -> None:
        super().__init__(message, hint=hint, status_code=status_code)


class DeputyAPIError(DeputyError):
    """Any other 4xx/5xx response.

    Carries a truncated snippet of the response body (never the request headers
    or token) to aid debugging.
    """

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        status_code: int | None = None,
        body: str | None = None,
    ) -> None:
        self.body = _truncate_body(body)
        if self.body:
            message = f"{message} Response: {self.body}"
        super().__init__(message, hint=hint, status_code=status_code)


def _truncate_body(body: str | None) -> str | None:
    """Trim a response body to :data:`BODY_SNIPPET_LIMIT` characters."""
    if body is None:
        return None
    trimmed = body.strip()
    if not trimmed:
        return None
    if len(trimmed) > BODY_SNIPPET_LIMIT:
        return trimmed[:BODY_SNIPPET_LIMIT] + "..."
    return trimmed


__all__ = [
    "BODY_SNIPPET_LIMIT",
    "DeputyAPIError",
    "DeputyAuthError",
    "DeputyConfigError",
    "DeputyError",
    "DeputyFeedError",
    "DeputyNotFoundError",
    "DeputyPermissionError",
    "DeputyRateLimitError",
    "DeputyRegionError",
    "DeputyWritesDisabledError",
]
