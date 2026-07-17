"""Executable entry point for ``python -m deputy_mcp`` and the console script.

The ``deputy-mcp`` console script (see ``pyproject.toml``:
``deputy-mcp = "deputy_mcp.__main__:main"``) resolves to :func:`main`, which is
re-exported here from :mod:`deputy_mcp.cli`. Running the module directly forwards
the CLI's exit code to the process.
"""

from __future__ import annotations

import sys

from deputy_mcp.cli import main

__all__ = ["main"]


if __name__ == "__main__":
    sys.exit(main())
