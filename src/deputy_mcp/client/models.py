"""Pydantic models for Deputy resource objects.

Deputy returns fields in PascalCase (``Id``, ``FirstName``, ``StartTime``) and
frequently ships extra fields that are not documented. Every model therefore uses
``extra="allow"`` so nothing is silently dropped, and declares only the fields whose
names and types are documented in ``deputy-api-read.md`` / ``deputy-api-write.md``.
Anything undocumented remains reachable via the model's extra attributes.

Timestamp semantics (see the read notes, "Date/time & timezone semantics"):

* ``StartTime`` / ``EndTime`` / ``ConfirmTime`` are unix timestamps in **integer UTC
  seconds**. ``EndTime`` is ``None`` while a timesheet is still in progress.
* ``Date`` is a calendar date string (``YYYY-MM-DD``) for the shift's **local business
  day** in the install/area timezone.

The ``start_dt`` / ``end_dt`` computed properties convert the unix-second fields to
timezone-aware UTC ``datetime`` objects (or ``None`` when the underlying field is unset).
Render them in the company's local timezone at the presentation layer, never here.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

__all__ = [
    "Company",
    "Contact",
    "DeputyModel",
    "Employee",
    "OperationalUnit",
    "Roster",
    "RosterSwap",
    "Timesheet",
    "Unavailability",
    "WhoAmI",
]


def _utc_from_unix(value: int | None) -> datetime | None:
    """Convert Deputy unix UTC seconds to a timezone-aware UTC datetime.

    Returns ``None`` when ``value`` is ``None`` (e.g. an in-progress timesheet's
    ``EndTime``) so callers can distinguish "not set" from the epoch.
    """
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=UTC)


class DeputyModel(BaseModel):
    """Base for all Deputy objects.

    ``extra="allow"`` keeps undocumented fields Deputy sends; ``populate_by_name`` lets
    a field be populated by its declared (PascalCase) name regardless of alias config.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class _ShiftLike(DeputyModel):
    """Shared shape for objects with a start/end time window (Roster, Timesheet)."""

    Id: int | None = None
    StartTime: int | None = None
    EndTime: int | None = None
    Date: str | None = None
    Employee: int | None = None
    OperationalUnit: int | None = None

    @property
    def start_dt(self) -> datetime | None:
        """Shift/timesheet start as a timezone-aware UTC datetime (``None`` if unset)."""
        return _utc_from_unix(self.StartTime)

    @property
    def end_dt(self) -> datetime | None:
        """Shift/timesheet end as a timezone-aware UTC datetime.

        ``None`` while a timesheet is in progress (Deputy leaves ``EndTime`` null).
        """
        return _utc_from_unix(self.EndTime)


class Roster(_ShiftLike):
    """A single scheduled shift (``/resource/Roster``).

    An *open* shift has ``Open`` true and no assigned ``Employee``. FK fields:
    ``Employee`` -> Employee.Id, ``OperationalUnit`` -> OperationalUnit.Id,
    ``MatchedByTimesheet`` -> Timesheet.Id.
    """

    MatchedByTimesheet: int | None = None
    Comment: str | None = None
    Warning: str | None = None
    TotalTime: float | None = None
    Cost: float | None = None
    Published: bool | None = None
    Open: bool | None = None
    ApprovalRequired: bool | None = None
    ConfirmStatus: int | None = None
    SwapStatus: int | None = None
    Creator: int | None = None
    Created: str | None = None
    Modified: str | None = None


class Timesheet(_ShiftLike):
    """Actual worked time / clock in-out record (``/resource/Timesheet``).

    ``IsInProgress`` true means the employee is clocked in and has not clocked out
    (``EndTime`` is null). FK fields: ``Employee`` -> Employee.Id,
    ``Roster`` -> Roster.Id, ``OperationalUnit`` -> OperationalUnit.Id.
    """

    Roster: int | None = None
    LeaveId: int | None = None
    TotalTime: float | None = None
    Cost: float | None = None
    IsInProgress: bool | None = None
    RealTime: bool | None = None
    TimeApproved: bool | None = None
    PayRuleApproved: bool | None = None
    Discarded: bool | None = None


class Employee(DeputyModel):
    """An employee record (``/resource/Employee``).

    FK fields: ``Company`` -> Company.Id (primary location),
    ``Contact`` -> Contact.Id (phones/emails), ``User`` -> User.Id (login account).
    ``Active`` true means the employee is not archived.
    """

    Id: int | None = None
    Company: int | None = None
    FirstName: str | None = None
    LastName: str | None = None
    DisplayName: str | None = None
    OtherName: str | None = None
    Contact: int | None = None
    User: int | None = None
    Active: bool | None = None
    StartDate: str | None = None
    TerminationDate: str | None = None
    DateOfBirth: str | None = None
    Role: int | None = None
    Created: str | None = None
    Modified: str | None = None


class OperationalUnit(DeputyModel):
    """An "Area" — a work location/department (``/resource/OperationalUnit``).

    FK fields: ``Company`` -> Company.Id (parent location),
    ``ParentOperationalUnit`` -> OperationalUnit.Id (self-referential),
    ``Contact`` -> Contact.Id, ``Address`` -> Address.Id.
    """

    Id: int | None = None
    Company: int | None = None
    ParentOperationalUnit: int | None = None
    OperationalUnitName: str | None = None
    Active: bool | None = None
    RosterActive: bool | None = None
    ShowOnRoster: bool | None = None
    Address: int | None = None
    Contact: int | None = None


class Company(DeputyModel):
    """A Location (``/resource/Company``).

    Deputy overloads "Company" to mean a location; ``Employee.Company`` and
    ``OperationalUnit.Company`` both point here. ``ParentCompany`` -> Company.Id (self).
    """

    Id: int | None = None
    ParentCompany: int | None = None
    CompanyName: str | None = None
    TradingName: str | None = None
    Address: int | None = None


class Contact(DeputyModel):
    """Phone/email contact details (``/resource/Contact``).

    Referenced by ``Employee.Contact`` and ``OperationalUnit.Contact``.
    """

    Id: int | None = None
    Phone1: str | None = None
    Phone2: str | None = None
    Email1: str | None = None
    Email2: str | None = None
    PrimaryPhone: int | None = None
    PrimaryEmail: int | None = None


class RosterSwap(DeputyModel):
    """A shift-swap request (``/resource/RosterSwap``).

    ``Status`` codes: 0 Not required, 1 Pending Out, 2 Pending In, 3 Pending In Out,
    4 Pending Approval (manager gate), 5 Approved, 6 Cancelled, 7 Declined. FK fields:
    ``SourceRoster``/``TargetRoster`` -> Roster.Id, ``Employee`` -> Employee.Id.
    """

    Id: int | None = None
    SourceRoster: int | None = None
    TargetRoster: int | None = None
    Employee: int | None = None
    Status: int | None = None
    RequestMessage: str | None = None
    ResponseMessage: str | None = None


class Unavailability(DeputyModel):
    """An employee unavailability record.

    Created via ``POST /supervise/unavail``; listed via the ``EmployeeAvailability``
    resource. Only a few response fields are documented: ``Id``, ``Type`` (0 one-off /
    1 recurring), and ``MaxDateRecurringGenerated``. The remaining
    ``EmployeeAvailability`` field names are not documented in the API notes, so they are
    intentionally left to the model's extra attributes rather than invented here.
    """

    Id: int | None = None
    Type: int | None = None
    Comment: str | None = None
    MaxDateRecurringGenerated: str | None = None


class WhoAmI(DeputyModel):
    """The authenticated user's account/employee info (``/resource/Account/WhoAmI``).

    Deputy's "Hello World" call. The exact response schema is install-dependent and not
    fully documented, so no fields are declared here on purpose; all values are available
    through the model's extra attributes (``model_extra``) without inventing key names.
    """
