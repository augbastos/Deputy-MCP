"""Deputy client error hierarchy — re-export of the leaf :mod:`deputy_mcp.errors`.

The classes now live in :mod:`deputy_mcp.errors`, a leaf module that imports
nothing from the ``deputy_mcp`` package. Keeping this module as a thin re-export
preserves every historical import path (``deputy_mcp.client.errors.X`` and
``deputy_mcp.client.X``) while letting :mod:`deputy_mcp.config` depend on the leaf
instead of the client package — which is what breaks the old import cycle.
"""

from __future__ import annotations

from deputy_mcp.errors import (
    BODY_SNIPPET_LIMIT,
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
    _truncate_body,
)

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
    "_truncate_body",
]
