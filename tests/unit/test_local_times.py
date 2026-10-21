"""Local record offsets, DST boundaries and honest fallback labels."""

from datetime import UTC

import pytest

from deputy_mcp.client.models import Company, Roster, Timesheet, WhoAmI
from deputy_mcp.render import (
    company_timezone_label,
    fmt_record_time,
    render_next_shift,
    render_roster_list,
    render_timesheet_list,
    render_who_is_working,
    render_whoami,
    resolve_timezone,
)


def shift() -> Roster:
    return Roster(
        StartTime=1792659600,
        EndTime=1792688400,
        StartTimeLocalized="2026-10-22T10:00:00+01:00",
        EndTimeLocalized="2026-10-22T18:00:00+01:00",
    )


@pytest.mark.parametrize("localized", [None, "", "invalid", "2026-10-22T10:00:00"])
def test_fallback(localized: str | None) -> None:
    assert fmt_record_time(1792659600, localized, UTC) == "2026-10-22 09:00"
    roster = Roster(StartTime=1792659600, StartTimeLocalized=localized)
    assert "_Times shown in UTC._" in render_roster_list([roster], UTC, "UTC")


def test_local_renderers_and_dst() -> None:
    roster = shift()
    for text in (
        render_roster_list([roster], UTC, "UTC"),
        render_next_shift(roster, UTC, "UTC"),
        render_timesheet_list([Timesheet(**{**roster.model_dump(), "TotalTime": 8})], UTC, "UTC"),
    ):
        assert "2026-10-22 10:00 - 2026-10-22 18:00" in text
        assert "the workplace's local time (UTC+01:00)" in text
    winter = Roster(
        StartTimeLocalized="2026-10-29T09:00:00+00:00",
        EndTimeLocalized="2026-10-29T17:00:00+00:00",
    )
    assert "(UTC+01:00 / UTC+00:00)" in render_roster_list([roster, winter], UTC, "UTC")


def test_mixed_fallback_is_identified_per_timestamp() -> None:
    partial = shift().model_copy(update={"EndTimeLocalized": None})
    text = render_roster_list([partial, Roster(StartTime=1792659600)], UTC, "UTC")
    assert "2026-10-22 10:00 - 2026-10-22 17:00 [UTC]" in text
    assert "2026-10-22 09:00 [UTC]" in text
    assert "fallback timestamps marked [UTC]" in text


def test_who_is_working_header_and_live_timesheet() -> None:
    timesheet = Timesheet(**shift().model_dump(exclude={"EndTime"}), EndTime=None)
    text = render_who_is_working(
        {"at": "2026-10-22T09:30:00+00:00", "clocked_in": [timesheet]}, UTC, "UTC"
    )
    assert "2026-10-22T09:30:00+00:00; local: 2026-10-22T10:30:00+01:00" in text
    assert "2026-10-22 10:00 - in progress" in text
    assert "fallback" not in text
    assert render_who_is_working({"at": "now"}, UTC, "UTC").startswith(
        "### Who is working (now)\n\n_Times shown in UTC._"
    )


def test_whoami_and_company_timezone() -> None:
    company = Company(CompanyName="Example")
    assert company_timezone_label(company) is None
    assert "Timezone: not exposed by Deputy (shift times use each shift's local time)" in (
        render_whoami(WhoAmI(), company, "UTC")
    )
    explicit = Company(Timezone="Europe/Dublin")
    assert company_timezone_label(explicit) == "Europe/Dublin"
    tz, label = resolve_timezone(explicit)
    assert label == "Europe/Dublin"
    assert fmt_record_time(1792659600, None, tz) == "2026-10-22 10:00"
    assert company_timezone_label(Company(Timezone="UTC")) == "UTC"
