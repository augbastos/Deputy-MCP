"""Leaf helpers shared by :mod:`deputy_mcp.config` and :mod:`deputy_mcp.oauth`.

This module imports **nothing** from the ``deputy_mcp`` package (stdlib only), so
both ``config`` and ``oauth`` can share ``normalize_base_url`` and the default
OAuth redirect port without importing each other. Previously ``oauth`` reached
into ``config._normalize_base_url`` (a private cross-module import) and the port
constant was duplicated in both modules; hoisting both here removes the private
import and single-sources the constant.
"""

from __future__ import annotations

#: API path suffixes stripped from a user-supplied base URL during normalization.
_API_SUFFIXES = ("/api/v1", "/api/v2")

#: Default OAuth loopback redirect port for the Authorization Code flow. Single
#: source of truth; both ``config`` and ``oauth`` import this name.
DEFAULT_REDIRECT_PORT = 8823


def normalize_base_url(raw: str) -> str:
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


__all__ = ["DEFAULT_REDIRECT_PORT", "normalize_base_url"]
