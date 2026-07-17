"""Fake Deputy API for the deputy-mcp demo -- DEMO / TESTING ONLY.

Serves a tiny slice of the Deputy Resource API on http://127.0.0.1:8765 using
only the Python standard library, so ``deputy-mcp`` (whoami / roster / who /
next / areas / timesheets) can be exercised end-to-end with no real Deputy
account and no network egress. The data is FICTIONAL (Cloud Nine Cafe; Alex
Rivera, Sam O'Brien, Jo Murphy) and mirrors the shapes in ``tests/fixtures/``.

It implements just enough of the API for the read CLI:

* ``GET  /api/v1/resource/Account/WhoAmI`` (and ``/api/v1/me`` fallback)
* ``GET  /api/v1/resource/Employee/{id}``
* ``POST /api/v1/resource/{Object}/QUERY`` -- the search/sort/join/paginate DSL

Shift and timesheet times are generated relative to "now" at startup so that
"who is working" and "next shift" return live, meaningful results whenever it
runs. NOT a Deputy implementation, NOT secure, NOT for production.

Run it directly::

    python examples/mock_deputy.py        # serves until Ctrl+C

then, in another shell::

    DEPUTY_BASE_URL=http://127.0.0.1:8765 \
    DEPUTY_ALLOW_CUSTOM_HOST=true \
    DEPUTY_API_TOKEN=demo-token \
    deputy-mcp who
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from urllib.parse import urlparse

HOST = "127.0.0.1"
PORT = 8765


# --------------------------------------------------------------------------- #
# Fictional dataset (Cloud Nine Cafe), timestamps anchored to "now" at startup
# --------------------------------------------------------------------------- #
def _build_dataset() -> dict[str, list[dict[str, Any]]]:
    """Build the in-memory demo dataset with times relative to the current instant."""
    now = datetime.now(UTC)
    today = now.date()
    tomorrow = today + timedelta(days=1)
    day_after = today + timedelta(days=2)

    def u(moment: datetime) -> int:
        """Unix UTC seconds, the way Deputy stores StartTime/EndTime."""
        return int(moment.timestamp())

    def at(day: Any, hour: int) -> datetime:
        return datetime.combine(day, time(hour, 0), tzinfo=UTC)

    employees = [
        _employee(101, "Alex", "Rivera", active=True, start="2023-04-01", role=45),
        _employee(102, "Sam", "O'Brien", active=True, start="2022-11-14", role=45),
        _employee(103, "Jo", "Murphy", active=False, start="2021-06-01", role=44),
    ]
    operational_units = [
        _opunit(11, "Front of House"),
        _opunit(12, "Kitchen"),
    ]
    company = [
        {
            "Id": 1,
            "ParentCompany": None,
            "CompanyName": "Cloud Nine Cafe",
            "TradingName": "Cloud Nine Cafe",
            "Address": 301,
            "Timezone": "Europe/Dublin",
        }
    ]
    rosters = [
        # Alex is on now (started an hour ago, ends in three) -> rostered + my roster.
        _roster(9001, u(now - timedelta(hours=1)), u(now + timedelta(hours=3)),
                today, 101, 11, published=True, open_=False, comment="Morning shift"),
        # Sam is also on now.
        _roster(9002, u(now - timedelta(hours=2)), u(now + timedelta(hours=2)),
                today, 102, 12, published=True, open_=False, comment="Kitchen prep"),
        # An unassigned open cover shift later today.
        _roster(9003, u(now + timedelta(hours=4)), u(now + timedelta(hours=8)),
                today, 0, 12, published=True, open_=True, comment="Open cover shift"),
        # Alex's next shift is tomorrow 09:00-17:00.
        _roster(9004, u(at(tomorrow, 9)), u(at(tomorrow, 17)),
                tomorrow, 101, 11, published=True, open_=False, comment="Morning shift"),
        # Sam works the day after tomorrow.
        _roster(9005, u(at(day_after, 12)), u(at(day_after, 20)),
                day_after, 102, 12, published=True, open_=False, comment="Evening shift"),
    ]
    timesheets = [
        # Alex is clocked in right now (no EndTime yet).
        _timesheet(7001, 101, 9001, 11, today, u(now - timedelta(hours=1)), None,
                   total=0.0, in_progress=True),
        # Sam clocked a completed earlier block today.
        _timesheet(7002, 102, 9002, 12, today, u(now - timedelta(hours=4)),
                   u(now - timedelta(hours=2)), total=2.0, in_progress=False),
    ]
    return {
        "Employee": employees,
        "OperationalUnit": operational_units,
        "Company": company,
        "Roster": rosters,
        "Timesheet": timesheets,
    }


def _employee(
    eid: int, first: str, last: str, *, active: bool, start: str, role: int
) -> dict[str, Any]:
    return {
        "Id": eid, "Company": 1, "FirstName": first, "LastName": last,
        "DisplayName": f"{first} {last}", "OtherName": None, "Contact": 400 + eid,
        "User": 100 + eid, "Active": active, "StartDate": start,
        "TerminationDate": None, "Role": role,
        "Created": f"{start}T09:00:00", "Modified": "2024-01-15T12:00:00",
    }


def _opunit(uid: int, name: str) -> dict[str, Any]:
    return {
        "Id": uid, "Company": 1, "ParentOperationalUnit": None,
        "OperationalUnitName": name, "Active": True, "RosterActive": True,
        "ShowOnRoster": True, "Address": 301, "Contact": 400 + uid,
    }


def _roster(rid: int, start: int, end: int, day: Any, employee: int, unit: int,
            *, published: bool, open_: bool, comment: str) -> dict[str, Any]:
    return {
        "Id": rid, "StartTime": start, "EndTime": end, "Date": day.isoformat(),
        "Employee": employee, "OperationalUnit": unit, "MatchedByTimesheet": None,
        "Comment": comment, "Warning": None, "TotalTime": round((end - start) / 3600, 2),
        "Cost": 0.0, "Published": published, "Open": open_, "ApprovalRequired": False,
        "ConfirmStatus": 0, "SwapStatus": 0, "Creator": 1,
        "Created": "2024-01-01T10:00:00", "Modified": "2024-01-01T10:00:00",
    }


def _timesheet(tid: int, employee: int, roster: int, unit: int, day: Any,
               start: int, end: int | None, *, total: float, in_progress: bool) -> dict[str, Any]:
    return {
        "Id": tid, "Employee": employee, "Roster": roster, "OperationalUnit": unit,
        "Date": day.isoformat(), "StartTime": start, "EndTime": end,
        "TotalTime": total, "Cost": round(total * 15, 2), "IsInProgress": in_progress,
        "RealTime": True, "TimeApproved": not in_progress,
        "PayRuleApproved": not in_progress, "Discarded": False,
    }


WHOAMI = {
    "UserId": 201, "EmployeeId": 101, "Name": "Alex Rivera",
    "Company": 1, "CompanyName": "Cloud Nine Cafe", "Permissions": {},
}


# --------------------------------------------------------------------------- #
# Minimal implementation of the Deputy /QUERY search DSL
# --------------------------------------------------------------------------- #
def _cmp(record_value: Any, filter_value: Any) -> int | None:
    """Three-way compare, numeric when both sides parse as numbers, else string."""
    if record_value is None:
        return None
    try:
        a, b = float(record_value), float(filter_value)
    except (TypeError, ValueError):
        a, b = str(record_value), str(filter_value)  # type: ignore[assignment]
    return (a > b) - (a < b)


def _match(record: dict[str, Any], slot: dict[str, Any]) -> bool:
    """Return whether ``record`` satisfies one QUERY search slot."""
    field, op = slot["field"], slot["type"]
    data = slot.get("data")
    value = record.get(field)
    if op == "is":
        return value is not None
    if op == "ns":
        return value is None
    if op in ("in", "nn"):
        members = data or []
        return (value in members) if op == "in" else (value not in members)
    if op in ("lk", "nk"):
        needle = str(data).strip("%").lower()
        inside = needle in ("" if value is None else str(value)).lower()
        return inside if op == "lk" else not inside
    result = _cmp(value, data)
    if result is None:
        return op == "ne"
    return {
        "eq": result == 0, "ne": result != 0, "gt": result > 0,
        "ge": result >= 0, "lt": result < 0, "le": result <= 0,
    }[op]


def _sort_key(value: Any) -> tuple[int, float, str]:
    """Type-stable sort key so mixed/None values never raise during sorting."""
    if value is None:
        return (0, 0.0, "")
    try:
        return (1, float(value), "")
    except (TypeError, ValueError):
        return (2, 0.0, str(value))


def run_query(obj: str, body: dict[str, Any], data: dict[str, list[dict[str, Any]]],
              employees_by_id: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply a QUERY body (search/sort/join/max/start) to the object's records."""
    records = list(data.get(obj, []))
    for slot in (body.get("search") or {}).values():
        records = [r for r in records if _match(r, slot)]
    for field, direction in (body.get("sort") or {}).items():
        records.sort(key=lambda r, f=field: _sort_key(r.get(f)), reverse=(direction == "desc"))
        break  # the client only ever sorts by a single field
    if "EmployeeObject" in (body.get("join") or []):
        joined = []
        for record in records:
            emp = employees_by_id.get(record.get("Employee"))
            joined.append({**record, "EmployeeObject": emp} if emp else record)
        records = joined
    start = int(body.get("start", 0) or 0)
    records = records[start:]
    if body.get("max"):
        records = records[: int(body["max"])]
    return records


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    dataset: ClassVar[dict[str, list[dict[str, Any]]]] = {}
    employees_by_id: ClassVar[dict[int, dict[str, Any]]] = {}

    def log_message(self, *args: Any) -> None:
        # Silence the default per-request access log so demo output stays clean.
        pass

    def _send(self, payload: Any, status: int = 200) -> None:
        blob = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self) -> None:  # method name fixed by BaseHTTPRequestHandler
        path = urlparse(self.path).path
        if path in ("/api/v1/resource/Account/WhoAmI", "/api/v1/me"):
            self._send(WHOAMI)
            return
        prefix = "/api/v1/resource/Employee/"
        if path.startswith(prefix) and path[len(prefix):].isdigit():
            emp = self.employees_by_id.get(int(path[len(prefix):]))
            self._send(emp if emp else {"error": "not found"}, 200 if emp else 404)
            return
        self._send({"error": f"no route for GET {path}"}, 404)

    def do_POST(self) -> None:  # method name fixed by BaseHTTPRequestHandler
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        parts = path.strip("/").split("/")
        if parts[:3] == ["api", "v1", "resource"] and len(parts) == 5 and parts[4] == "QUERY":
            self._send(run_query(parts[3], body, self.dataset, self.employees_by_id))
            return
        self._send({"error": f"no route for POST {path}"}, 404)


def main() -> None:
    """Start the demo server (blocks until interrupted)."""
    dataset = _build_dataset()
    _Handler.dataset = dataset
    _Handler.employees_by_id = {e["Id"]: e for e in dataset["Employee"]}
    server = ThreadingHTTPServer((HOST, PORT), _Handler)
    print(f"mock_deputy: DEMO/TESTING ONLY -- serving fictional Deputy data on "
          f"http://{HOST}:{PORT} (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
