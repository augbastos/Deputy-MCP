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
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
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

    Attributes:
        api_token: Permanent token or OAuth access token (redacted in output).
        allow_custom_host: Allow a ``base_url`` host outside ``*.deputy.com`` (opt-in
            escape hatch for enterprise custom domains). Off by default: an unknown
            host fails closed rather than silently talking to an unintended server.
        base_url: Normalized install origin, e.g. ``https://acme.eu.deputy.com``.
        allow_writes: Whether write operations are permitted (opt-in).
        cache_ttl: Read-cache lifetime in seconds; ``0`` disables caching.
        timeout: Per-request timeout in seconds.
        max_retries: Maximum retries for throttling/transient transport errors.
    """

    model_config = ConfigDict(frozen=True)

    api_token: SecretStr
    # Declared before ``base_url`` so the base-URL validator can read it via
    # ``info.data`` to decide whether a non-deputy.com host is allowed.
    allow_custom_host: bool = False
    base_url: str
    allow_writes: bool = False
    cache_ttl: int = Field(default=30, ge=0)
    timeout: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=3, ge=0)

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str, info: ValidationInfo) -> str:
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

    @property
    def api_url(self) -> str:
        """The versioned API base used by the HTTP transport."""
        return f"{self.base_url}/api/v1"

    def token(self) -> str:
        """Return the raw token value. Never log or print the result."""
        return self.api_token.get_secret_value()

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> DeputyConfig:
        """Build a config from environment variables, failing closed.

        Args:
            environ: Optional mapping to read instead of ``os.environ`` (tests).

        Raises:
            DeputyConfigError: If a required variable is missing/blank or a
                value cannot be parsed.
        """
        env = os.environ if environ is None else environ

        token = (env.get("DEPUTY_API_TOKEN") or "").strip()
        if not token:
            raise DeputyConfigError(
                "DEPUTY_API_TOKEN is not set.",
                hint=(
                    "Create a permanent token in Deputy under Business settings "
                    "-> Integrations -> API access (New OAuth Client -> Get an "
                    "Access Token; shown once), then set it as DEPUTY_API_TOKEN."
                ),
            )

        base_url = (env.get("DEPUTY_BASE_URL") or "").strip()
        if not base_url:
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
                api_token=SecretStr(token),
                allow_custom_host=allow_custom_host,
                base_url=base_url,
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
