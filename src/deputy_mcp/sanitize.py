"""Central redaction for text that can reach a model, a terminal, or a log.

Deputy's error bodies are unstructured and undocumented, and an MCP tool result is read
by a language model — so a raw body must never be passed through verbatim. Everything
that renders a :class:`~deputy_mcp.errors.DeputyError` (MCP tools, resources, the CLI)
funnels its text through :func:`redact`, and every response-body excerpt goes through
:func:`snippet` first.

The goal is to keep what makes an error actionable (the HTTP status, Deputy's own error
wording, which host failed) and drop what is dangerous to echo: bearer/OAuth tokens,
client secrets, authorization codes, personal iCal feed URLs, query-string secrets,
email addresses, and oversized or HTML bodies.

This module is a leaf: it imports nothing from ``deputy_mcp`` (stdlib only), so the leaf
error hierarchy can depend on it without reintroducing an import cycle.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit

__all__ = ["REDACTED", "SNIPPET_LIMIT", "redact", "snippet"]

#: Replacement marker for any redacted value.
REDACTED = "[REDACTED]"
#: Maximum characters kept from a response body excerpt.
SNIPPET_LIMIT = 300

#: Keys whose values are secrets whenever they appear as ``key=value`` / ``"key": value``.
_SECRET_KEYS = (
    "access_token|refresh_token|id_token|client_secret|client_assertion|password|passwd|"
    "secret|api_?key|authorization|auth_token|token|calendar_?url|code|state"
)
#: ``code`` and ``state`` are also ordinary words in error payloads (``"code": 403``);
#: their values are only treated as secrets when they look like an opaque credential.
_AMBIGUOUS_KEYS = frozenset({"code", "state"})
#: Values that are never secrets on their own.
_NON_SECRET_VALUES = frozenset({"", "null", "none", "true", "false", "bearer", "basic"})

_KEY_VALUE = re.compile(
    rf"""(?ix)
    (?P<key>["']?\b(?:{_SECRET_KEYS})\b["']?\s*[:=]\s*)
    (?P<value>"[^"]*"|'[^']*'|[^\s,&;}}\]]+)
    """
)
_AUTH_SCHEME = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")
_URL = re.compile(r"(?i)\bhttps?://[^\s\"'<>()]+")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
#: Opaque credential-shaped runs: 32+ URL-safe characters mixing letters and digits.
_OPAQUE = re.compile(r"\b(?=[A-Za-z0-9_-]*[0-9])(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{32,}\b")
_HTML = re.compile(r"(?is)^\s*(<!doctype|<html|<head|<body)")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")


def redact(text: str, *, secrets: Iterable[str] = ()) -> str:
    """Return ``text`` with credentials and personal data replaced by :data:`REDACTED`.

    Args:
        text: Any text that may be shown to a model, printed, or logged.
        secrets: Exact secret values the caller holds (a client secret, a refresh
            token, an authorization code). Each occurrence is masked first, whatever
            shape it has.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    text = _URL.sub(_redact_url, text)
    text = _AUTH_SCHEME.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _JWT.sub(REDACTED, text)
    text = _KEY_VALUE.sub(_redact_key_value, text)
    text = _EMAIL.sub("[REDACTED_EMAIL]", text)
    return _OPAQUE.sub(REDACTED, text)


def snippet(
    body: str | None, *, secrets: Iterable[str] = (), limit: int = SNIPPET_LIMIT
) -> str | None:
    """Reduce a raw response body to a short, redacted, single-line excerpt.

    HTML bodies (proxy or gateway error pages) are summarised instead of quoted, control
    characters are dropped and whitespace is collapsed so a body cannot smuggle a
    multi-line block into the message. Returns ``None`` for an empty body.
    """
    if body is None or not body.strip():
        return None
    if _HTML.match(body):
        return "[HTML error page omitted]"
    cleaned = _WHITESPACE.sub(" ", _CONTROL.sub("", body)).strip()
    cleaned = redact(cleaned, secrets=secrets)
    if len(cleaned) > limit:
        return cleaned[:limit] + "..."
    return cleaned


def _redact_url(match: re.Match[str]) -> str:
    """Keep a URL's scheme, host and (non-feed) path; drop credentials and the query."""
    raw = match.group(0)
    try:
        parts = urlsplit(raw)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return REDACTED
    path = parts.path
    lowered = path.lower()
    if "ical" in lowered or lowered.endswith(".ics"):
        # A personal calendar feed carries its token in the path itself.
        path = f"/{REDACTED}"
    else:
        path = _OPAQUE.sub(REDACTED, path)
    suffix = f"?{REDACTED}" if parts.query or parts.fragment else ""
    return f"{parts.scheme}://{host}{port}{path}{suffix}"


def _redact_key_value(match: re.Match[str]) -> str:
    key, value = match.group("key"), match.group("value")
    bare = value.strip("\"'")
    name = key.strip("\"' :=\t").lower()
    if bare.lower() in _NON_SECRET_VALUES or bare == REDACTED:
        # Empty/null flags, or an auth scheme whose credential _AUTH_SCHEME already masked.
        return match.group(0)
    if name in _AMBIGUOUS_KEYS and (bare.isdigit() or len(bare) < 16):
        return match.group(0)
    quote = value[0] if value[:1] in {'"', "'"} else ""
    return f"{key}{quote}{REDACTED}{quote}"
