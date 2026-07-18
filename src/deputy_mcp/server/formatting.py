"""Backwards-compatible re-export of the shared rendering core.

The dual-format (markdown / JSON) renderers now live in the fastmcp-free
:mod:`deputy_mcp.render` module so the MCP server layer and the CLI share ONE
renderer — and therefore one timezone behaviour (no UTC-vs-company-timezone
fork). This module re-exports that surface under its historical
``deputy_mcp.server.formatting`` import path, which the server tools and
resources still import from. There is no server-specific rendering logic here;
edit :mod:`deputy_mcp.render` instead.
"""

from __future__ import annotations

from deputy_mcp.render import (
    ResponseFormat,
    areas_by_id,
    employee_display,
    fmt_ts,
    render,
    render_areas,
    render_calendar_url,
    render_calendar_url_ical,
    render_colleagues,
    render_employee_list,
    render_next_shift,
    render_roster_list,
    render_timesheet_list,
    render_who_is_working,
    render_whoami,
    render_whoami_ical,
    resolve_timezone,
    to_json,
)

__all__ = [
    "ResponseFormat",
    "areas_by_id",
    "employee_display",
    "fmt_ts",
    "render",
    "render_areas",
    "render_calendar_url",
    "render_calendar_url_ical",
    "render_colleagues",
    "render_employee_list",
    "render_next_shift",
    "render_roster_list",
    "render_timesheet_list",
    "render_who_is_working",
    "render_whoami",
    "render_whoami_ical",
    "resolve_timezone",
    "to_json",
]
