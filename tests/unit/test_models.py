"""Unit tests for :mod:`deputy_mcp.client.models`.

Covers parsing of realistic Deputy payloads, the ``extra='allow'`` policy (undocumented
fields survive), and the unix-to-UTC ``start_dt`` / ``end_dt`` computed properties,
including the in-progress-timesheet case where ``EndTime`` is null.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from deputy_mcp.client.models import (
    Company,
    Contact,
    DeputyModel,
    Employee,
    OperationalUnit,
    Roster,
    RosterSwap,
    Timesheet,
    Unavailability,
    WhoAmI,
)

# 2021-01-01T00:00:00Z and +8h (unix 1609459200 / 1609488000), matching the factories.
_START_DT = datetime(2021, 1, 1, 0, 0, 0, tzinfo=UTC)
_END_DT = datetime(2021, 1, 1, 8, 0, 0, tzinfo=UTC)


# -- timestamp properties ----------------------------------------------------


def test_roster_timestamps_are_utc_aware(
    make_roster: Callable[..., dict[str, Any]],
) -> None:
    roster = Roster.model_validate(make_roster())
    assert roster.start_dt == _START_DT
    assert roster.end_dt == _END_DT
    # Timezone-aware UTC, not naive.
    assert roster.start_dt is not None
    assert roster.start_dt.tzinfo is UTC


def test_timesheet_in_progress_has_no_end(
    make_timesheet: Callable[..., dict[str, Any]],
) -> None:
    ts = Timesheet.model_validate(make_timesheet(EndTime=None, IsInProgress=True))
    assert ts.start_dt == _START_DT
    assert ts.end_dt is None
    assert ts.IsInProgress is True


def test_unset_start_time_yields_none() -> None:
    roster = Roster.model_validate({"Id": 1})
    assert roster.start_dt is None
    assert roster.end_dt is None


def test_epoch_zero_maps_to_1970() -> None:
    roster = Roster.model_validate({"StartTime": 0})
    assert roster.start_dt == datetime(1970, 1, 1, 0, 0, 0, tzinfo=UTC)


# -- extra fields survive ----------------------------------------------------


def test_extra_fields_are_kept(make_roster: Callable[..., dict[str, Any]]) -> None:
    payload = make_roster(UndocumentedField="keep me", Slots=[{"start": 1}])
    roster = Roster.model_validate(payload)
    assert roster.model_extra is not None
    assert roster.model_extra["UndocumentedField"] == "keep me"
    # Extra fields are reachable as attributes too.
    assert roster.UndocumentedField == "keep me"


def test_declared_fields_are_typed(make_roster: Callable[..., dict[str, Any]]) -> None:
    roster = Roster.model_validate(make_roster())
    assert roster.Id == 9001
    assert roster.Employee == 101
    assert roster.OperationalUnit == 11
    assert roster.Open is False
    assert roster.Published is True


# -- other models parse ------------------------------------------------------


def test_employee_parses(sample_employees: list[dict[str, Any]]) -> None:
    alex, sam, jo = (Employee.model_validate(e) for e in sample_employees)
    assert alex.DisplayName == "Alex Rivera"
    assert sam.LastName == "O'Brien"
    assert jo.Active is False
    assert jo.TerminationDate == "2024-06-30"


def test_operational_unit_parses(
    make_operational_unit: Callable[..., dict[str, Any]],
) -> None:
    area = OperationalUnit.model_validate(make_operational_unit())
    assert area.OperationalUnitName == "Front of House"
    assert area.RosterActive is True


def test_company_parses_and_keeps_timezone(
    make_company: Callable[..., dict[str, Any]],
) -> None:
    company = Company.model_validate(make_company())
    assert company.CompanyName == "Cloud Nine Cafe"
    # Timezone is undocumented on the model but preserved via extra.
    assert company.model_extra is not None
    assert company.model_extra["Timezone"] == "Europe/Dublin"


def test_contact_parses(make_contact: Callable[..., dict[str, Any]]) -> None:
    contact = Contact.model_validate(make_contact())
    assert contact.Email1 == "alex.rivera@example.com"
    assert contact.PrimaryPhone == 1


def test_roster_swap_parses() -> None:
    swap = RosterSwap.model_validate(
        {"Id": 5, "SourceRoster": 9001, "Status": 4, "RequestMessage": "cover please"}
    )
    assert swap.Status == 4
    assert swap.SourceRoster == 9001


def test_unavailability_parses() -> None:
    unavail = Unavailability.model_validate({"Id": 3, "Type": 1, "Comment": "study"})
    assert unavail.Type == 1
    assert unavail.Comment == "study"


def test_whoami_keeps_everything_as_extra(
    make_whoami: Callable[..., dict[str, Any]],
) -> None:
    who = WhoAmI.model_validate(make_whoami())
    # WhoAmI declares no fields on purpose; the install-specific shape lives in extra.
    assert who.model_extra is not None
    assert who.model_extra["EmployeeId"] == 101
    assert who.model_extra["Name"] == "Alex Rivera"


def test_base_model_allows_extra() -> None:
    obj = DeputyModel.model_validate({"Anything": 1, "Nested": {"a": 2}})
    assert obj.model_extra == {"Anything": 1, "Nested": {"a": 2}}
