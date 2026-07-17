"""Reusable prompt templates for common Deputy workflows.

Prompts are user-invocable message templates. They do not call Deputy directly;
instead each returns instructions that tell the assistant which ``deputy_*`` tools
to call and how to shape the answer. That keeps the data live (fetched at use
time through the tools) and needs no client, so :func:`register` takes only the
FastMCP instance.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

__all__ = ["register"]


def register(mcp: FastMCP[Any]) -> None:
    """Register the prompt templates onto ``mcp``."""

    @mcp.prompt(
        name="summarize_my_week",
        description="Summarize the user's scheduled shifts and worked hours for the week.",
    )
    def summarize_my_week() -> str:
        return (
            "Summarize my working week using the Deputy tools.\n\n"
            "1. Call `deputy_get_my_roster` for the next 7 days to get my scheduled shifts.\n"
            "2. Call `deputy_get_my_timesheets` for the last 7 days to get my worked hours.\n\n"
            "Then give me a short summary that includes:\n"
            "- how many shifts I have coming up and their total planned hours,\n"
            "- my total hours actually worked in the last week,\n"
            "- any timesheet still in progress, and\n"
            "- anything notable (back-to-back days, an unusually long or short shift).\n"
            "Show times in the timezone the tools report and keep it concise."
        )

    @mcp.prompt(
        name="coverage_check",
        description="Check staffing coverage for a date: rostered staff, open shifts, who is on.",
    )
    def coverage_check(
        date: Annotated[str, Field(description="The date to check, ISO format YYYY-MM-DD.")],
    ) -> str:
        return (
            f"Check staffing coverage for {date} using the Deputy tools.\n\n"
            f"1. Call `deputy_get_team_roster` with date={date} to see who is scheduled.\n"
            f"2. Call `deputy_search_shifts` with open_only=true and "
            f"start_date={date}, end_date={date} to find unassigned open shifts.\n"
            "3. If the date is today, call `deputy_who_is_working` to compare the "
            "schedule against who is actually clocked in.\n\n"
            "Then tell me:\n"
            "- which areas are covered and which look understaffed,\n"
            "- how many open (unfilled) shifts remain and in which areas, and\n"
            "- any gap between who is scheduled and who is actually on (today only).\n"
            "Be specific about areas and times."
        )
