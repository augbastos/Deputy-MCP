"""Regression fence for the historical config/client import cycle.

The cycle ``config -> client.errors -> client/__init__ -> http -> config`` was for a
long time only survived by "import the client package first" ordering guards
(in ``tests/conftest.py`` and a scratchpad demo). Moving the error hierarchy to the
leaf :mod:`deputy_mcp.errors` and the shared helpers to :mod:`deputy_mcp._util`
broke the cycle so ``config`` no longer imports the client package.

These tests run a COLD interpreter (fresh ``sys.modules``) via subprocess and assert
that importing the two modules that used to sit on the cycle succeeds on its own —
so the cycle cannot silently return under some future import reordering. A subprocess
is essential: importing here would reuse this process's already-populated
``sys.modules`` and hide a reintroduced cycle.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", ["deputy_mcp.config", "deputy_mcp.oauth"])
def test_cold_import_has_no_cycle(module: str) -> None:
    """A cold interpreter can import the module standalone (exit 0, no cycle)."""
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"cold `import {module}` failed (exit {result.returncode}) — the import cycle "
        f"may have returned.\nstderr:\n{result.stderr}"
    )


def test_referencing_create_server_does_not_import_fastmcp() -> None:
    """`from deputy_mcp.server import create_server` must NOT eagerly import fastmcp.

    The client-only CLI's serve path binds this factory reference; the heavy fastmcp
    dependency must load only when the server is actually built, so the reference alone
    stays dependency-light. Run cold in a subprocess so a stale sys.modules can't hide a
    regression.
    """
    code = (
        "import sys\n"
        "from deputy_mcp.server import create_server\n"
        "assert 'fastmcp' not in sys.modules, 'fastmcp imported eagerly by referencing "
        "create_server'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"referencing create_server eagerly imported fastmcp (or failed).\nstderr:\n{result.stderr}"
    )
