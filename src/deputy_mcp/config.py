"""Environment-driven configuration for the Deputy client and MCP server.

Configuration is loaded once at startup from ``DEPUTY_*`` environment variables
via :meth:`DeputyConfig.from_env`. Loading fails closed: a missing token or base
URL raises :class:`DeputyConfigError` with a message that says exactly which
variable to set and where to find its value. The API token is stored as a
:class:`pydantic.SecretStr` so it is redacted from any ``repr``/``str`` and can
never be accidentally logged.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from deputy_mcp.client.errors import DeputyConfigError

#: Env values (case-insensitive) that enable a boolean flag.
_TRUTHY = frozenset({"true", "1", "yes"})

#: API path suffixes stripped from a user-supplied base URL during normalization.
_API_SUFFIXES = ("/api/v1", "/api/v2")


def _normalize_base_url(raw: str) -> str:
    """Normalize a Deputy base URL to the bare install origin.

    Accepts the value with or without a scheme, trailing slash, or ``/api/v1``
    (``/api/v2``) suffix and returns just ``https://{install}.{geo}.deputy.com``.
    A missing scheme defaults to ``https``.
    """
    url = raw.strip()
    if "://" not in url:
        url = f"https://{url}"
    url = url.rstrip("/")
    lowered = url.lower()
    for suffix in _API_SUFFIXES:
        if lowered.endswith(suffix):
            url = url[: -len(suffix)]
            break
    return url.rstrip("/")


class DeputyConfig(BaseModel):
    """Validated runtime configuration.

    Two mutually-supportive credentials decide the operating :attr:`mode`:

    * ``api_token`` (+ ``base_url``) → **api** mode: the full authenticated client, every
      tool. Behaviour identical to before iCal mode existed.
    * ``calendar_url`` only → **ical** mode: the caller has no API token (an ordinary
      employee cannot mint one) but supplies their personal Deputy iCal feed, which needs
      no token and exposes only their own roster. ``base_url`` is not required here.

    At least one of the two must be present; :meth:`from_env` (and the after-validator)
    fail closed naming both paths when neither is.

    Attributes:
        api_token: Permanent token or OAuth access token (redacted in output). ``None`` in
            iCal mode.
        allow_custom_host: Allow a ``base_url`` host outside ``*.deputy.com`` (opt-in
            escape hatch for enterprise custom domains). Off by default: an unknown
            host fails closed rather than silently talking to an unintended server.
        base_url: Normalized install origin, e.g. ``https://acme.eu.deputy.com``. ``None``
            in iCal mode (the feed URL is self-contained).
        calendar_url: Personal Deputy iCal feed URL (the ``CalendarURL`` field of
            ``GET /api/v1/me``). It carries its own feed token, so it is a SECRET, stored
            as :class:`~pydantic.SecretStr` and never logged. ``None`` in api mode.
        allow_writes: Whether write operations are permitted (opt-in). Ignored in iCal
            mode, which is read-only by construction.
        cache_ttl: Read-cache lifetime in seconds; ``0`` disables caching.
        timeout: Per-request timeout in seconds.
        max_retries: Maximum retries for throttling/transient transport errors.
    """

    model_config = ConfigDict(frozen=True)

    api_token: SecretStr | None = None
    # Declared before ``base_url`` so the base-URL validator can read it via
    # ``info.data`` to decide whether a non-deputy.com host is allowed.
    allow_custom_host: bool = False
    base_url: str | None = None
    calendar_url: SecretStr | None = None
    allow_writes: bool = False
    cache_ttl: int = Field(default=30, ge=0)
    timeout: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=3, ge=0)

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        normalized = _normalize_base_url(value)
        host = urlparse(normalized).hostname or ""
        if not host.endswith(".deputy.com"):
            # Fail closed on an unexpected host: a typo or wrong value could point the
            # token at an unintended server. Legitimate enterprise custom domains opt
            # back in via DEPUTY_ALLOW_CUSTOM_HOST, which downgrades this to a warning.
            if not info.data.get("allow_custom_host", False):
                raise ValueError(
                    f"DEPUTY_BASE_URL host '{host}' is not a Deputy install "
                    "('{install}.{geo}.deputy.com'). If this is a legitimate "
                    "enterprise custom domain, set DEPUTY_ALLOW_CUSTOM_HOST=true to "
                    "allow it; otherwise correct DEPUTY_BASE_URL to the address you "
                    "see when logged in to Deputy."
                )
            warnings.warn(
                f"DEPUTY_BASE_URL host '{host}' does not look like a Deputy install "
                "('{install}.{geo}.deputy.com'); allowed because "
                "DEPUTY_ALLOW_CUSTOM_HOST is set.",
                stacklevel=2,
            )
        return normalized

    @model_validator(mode="after")
    def _require_a_credential(self) -> DeputyConfig:
        """Fail closed unless a usable credential set is present (defence in depth).

        :meth:`from_env` already gives friendlier messages, but constructing the model
        directly must never yield an unusable config: at least one of ``api_token`` or
        ``calendar_url`` is required, and api mode additionally needs ``base_url``.
        """
        if self.api_token is None and self.calendar_url is None:
            raise ValueError(
                "no Deputy credentials: set DEPUTY_API_TOKEN (api mode) or "
                "DEPUTY_CALENDAR_URL (iCal mode)"
            )
        if self.api_token is not None and self.base_url is None:
            raise ValueError("DEPUTY_BASE_URL is required when DEPUTY_API_TOKEN is set (api mode)")
        return self

    @property
    def mode(self) -> Literal["api", "ical"]:
        """The resolved operating mode.

        ``api`` whenever an API token is present (it is primary even if a calendar URL is
        also set, so the full tool surface stays available); otherwise ``ical``. An
        after-validator guarantees at least one credential exists, so these two are
        exhaustive.
        """
        return "api" if self.api_token is not None else "ical"

    @property
    def api_url(self) -> str:
        """The versioned API base used by the HTTP transport (api mode only)."""
        if self.base_url is None:
            raise DeputyConfigError(
                "No DEPUTY_BASE_URL is configured (this client is in iCal mode).",
                hint="Set DEPUTY_API_TOKEN and DEPUTY_BASE_URL to use the full Deputy API.",
            )
        return f"{self.base_url}/api/v1"

    def token(self) -> str:
        """Return the raw token value (api mode). Never log or print the result."""
        if self.api_token is None:
            raise DeputyConfigError(
                "No DEPUTY_API_TOKEN is configured (this client is in iCal mode).",
                hint="Set DEPUTY_API_TOKEN and DEPUTY_BASE_URL to use the full Deputy API.",
            )
        return self.api_token.get_secret_value()

    def calendar_url_value(self) -> str:
        """Return the raw iCal feed URL (iCal mode). Never log or print the result.

        The value carries a feed token, so it is treated exactly like the API token:
        referenced by accessor, never rendered into logs, errors or output.
        """
        if self.calendar_url is None:
            raise DeputyConfigError(
                "No DEPUTY_CALENDAR_URL is configured.",
                hint=(
                    "Copy your personal calendar link from Deputy (My Schedule -> "
                    "subscribe/export calendar) and set it as DEPUTY_CALENDAR_URL."
                ),
            )
        return self.calendar_url.get_secret_value()

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> DeputyConfig:
        """Build a config from environment variables, failing closed.

        Values may also come from a dotenv file: ``DEPUTY_ENV_FILE`` names one
        explicitly, otherwise a ``.env`` in the current directory is used when
        present. Real environment variables always take precedence over the file.

        Args:
            environ: Optional mapping to read instead of ``os.environ`` (tests).

        Raises:
            DeputyConfigError: If a required variable is missing/blank or a
                value cannot be parsed.
        """
        env = os.environ if environ is None else environ
        env = _with_env_file(env, allow_cwd_default=environ is None)

        token = (env.get("DEPUTY_API_TOKEN") or "").strip()
        calendar_url = (env.get("DEPUTY_CALENDAR_URL") or "").strip()

        # Fail closed naming BOTH legitimate paths: either an API token (full client) or a
        # personal iCal feed URL (roster-only, no token needed). An ordinary employee who
        # cannot mint a token still has a valid path via DEPUTY_CALENDAR_URL.
        if not token and not calendar_url:
            raise DeputyConfigError(
                "No Deputy credentials found: set DEPUTY_API_TOKEN or DEPUTY_CALENDAR_URL.",
                hint=(
                    "Full access (all tools): create a token in Deputy under Business "
                    "settings -> Integrations -> API access (New OAuth Client -> Get an "
                    "Access Token; shown once) and set DEPUTY_API_TOKEN plus DEPUTY_BASE_URL. "
                    "No API token (ordinary employee)? Open Deputy -> My Schedule -> "
                    "subscribe/export calendar, copy the iCal link and set it as "
                    "DEPUTY_CALENDAR_URL for roster-only, read-only access."
                ),
            )

        base_url = (env.get("DEPUTY_BASE_URL") or "").strip()
        # base_url is required only in api mode; iCal mode's feed URL is self-contained.
        if token and not base_url:
            raise DeputyConfigError(
                "DEPUTY_BASE_URL is not set.",
                hint=(
                    "Use the URL shown in your browser when logged in to Deputy, "
                    "e.g. https://your-company.eu.deputy.com. Set it as "
                    "DEPUTY_BASE_URL."
                ),
            )

        allow_writes = (env.get("DEPUTY_ALLOW_WRITES") or "").strip().lower() in _TRUTHY
        allow_custom_host = (env.get("DEPUTY_ALLOW_CUSTOM_HOST") or "").strip().lower() in _TRUTHY
        cache_ttl = _parse_int(env, "DEPUTY_CACHE_TTL", 30)
        timeout = _parse_float(env, "DEPUTY_TIMEOUT", 30.0)
        max_retries = _parse_int(env, "DEPUTY_MAX_RETRIES", 3)

        try:
            return cls(
                api_token=SecretStr(token) if token else None,
                allow_custom_host=allow_custom_host,
                base_url=base_url or None,
                calendar_url=SecretStr(calendar_url) if calendar_url else None,
                allow_writes=allow_writes,
                cache_ttl=cache_ttl,
                timeout=timeout,
                max_retries=max_retries,
            )
        except ValidationError as exc:
            raise DeputyConfigError(
                "Invalid Deputy configuration.",
                hint=_summarize_validation_error(exc),
            ) from exc


def _with_env_file(env: Mapping[str, str], *, allow_cwd_default: bool) -> Mapping[str, str]:
    """Overlay ``DEPUTY_*`` values from an optional dotenv file; real env vars win.

    ``DEPUTY_ENV_FILE`` names the file explicitly (missing file = config error, the
    user clearly intended it). Without it, a ``.env`` in the current directory is
    picked up — but only when reading the real process environment
    (``allow_cwd_default``), so tests passing an explicit mapping stay hermetic and
    can never silently absorb a developer's local credentials.
    """
    explicit = (env.get("DEPUTY_ENV_FILE") or "").strip()
    candidate = Path(explicit) if explicit else Path(".env")
    if explicit and not candidate.is_file():
        raise DeputyConfigError(
            f"DEPUTY_ENV_FILE points to '{candidate}', which does not exist.",
            hint="Create the file (see .env.example) or unset DEPUTY_ENV_FILE.",
        )
    if not explicit and (not allow_cwd_default or not candidate.is_file()):
        return env
    file_values = {
        key: value
        for key, value in dotenv_values(candidate).items()
        if value is not None and key.startswith("DEPUTY_")
    }
    if not file_values:
        return env
    merged: dict[str, str] = dict(file_values)
    merged.update({key: value for key, value in env.items() if key.startswith("DEPUTY_")})
    return merged


def _parse_int(env: Mapping[str, str], name: str, default: int) -> int:
    """Parse an integer env var, raising a clear config error on bad input."""
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise DeputyConfigError(
            f"{name} must be an integer, got '{raw}'.",
            hint=f"Unset {name} to use the default ({default}) or set a whole number.",
        ) from exc


def _parse_float(env: Mapping[str, str], name: str, default: float) -> float:
    """Parse a float env var, raising a clear config error on bad input."""
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise DeputyConfigError(
            f"{name} must be a number, got '{raw}'.",
            hint=f"Unset {name} to use the default ({default}) or set a number of seconds.",
        ) from exc


def _summarize_validation_error(exc: ValidationError) -> str:
    """Turn a pydantic validation error into a short, safe hint (no token)."""
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(item) for item in err.get("loc", ())) or "value"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts) if parts else "check the DEPUTY_* environment variables."


__all__ = ["DeputyConfig"]
