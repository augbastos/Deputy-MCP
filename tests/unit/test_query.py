"""Unit tests for :mod:`deputy_mcp.client.query`.

Covers the exact QUERY body shape documented in ``deputy-api-read.md`` (search slots,
sort, join, max, start), operator/argument validation, and ``query_all`` pagination
around the hard 500-record cap using a lightweight fake transport.
"""

from __future__ import annotations

from typing import Any

import pytest

from deputy_mcp.client.query import MAX_PAGE, VALID_OPS, QueryBuilder, query_all

# -- build(): exact body shape ----------------------------------------------


def test_empty_builder_produces_empty_body() -> None:
    assert QueryBuilder().build() == {}


def test_build_matches_documented_shape() -> None:
    body = (
        QueryBuilder()
        .where("StartTime", "le", "1663084585")
        .where("EndTime", "gt", "1663084585")
        .where("Open", "ne", True)
        .join("OperationalUnitObject")
        .sort("StartTime")
        .max(500)
        .start(0)
        .build()
    )
    assert body == {
        "search": {
            "s1": {"field": "StartTime", "type": "le", "data": "1663084585"},
            "s2": {"field": "EndTime", "type": "gt", "data": "1663084585"},
            "s3": {"field": "Open", "type": "ne", "data": True},
        },
        "join": ["OperationalUnitObject"],
        "sort": {"StartTime": "asc"},
        "max": 500,
        "start": 0,
    }


def test_range_query_is_two_slots_on_same_field() -> None:
    body = (
        QueryBuilder().where("Date", "gt", "2022-05-01").where("Date", "lt", "2022-05-08").build()
    )
    assert body["search"] == {
        "s1": {"field": "Date", "type": "gt", "data": "2022-05-01"},
        "s2": {"field": "Date", "type": "lt", "data": "2022-05-08"},
    }


def test_slots_are_numbered_sequentially() -> None:
    builder = QueryBuilder()
    for i in range(4):
        builder.where(f"F{i}", "eq", i)
    assert list(builder.build()["search"].keys()) == ["s1", "s2", "s3", "s4"]


def test_data_passed_through_verbatim() -> None:
    # A string timestamp must not be coerced to an int (matches the documented sample).
    body = QueryBuilder().where("StartTime", "gt", "1663084585").build()
    assert body["search"]["s1"]["data"] == "1663084585"
    assert isinstance(body["search"]["s1"]["data"], str)


def test_sort_desc() -> None:
    assert QueryBuilder().sort("Id", desc=True).build()["sort"] == {"Id": "desc"}


def test_join_deduplicates_preserving_order() -> None:
    body = QueryBuilder().join("A").join("B").join("A").build()
    assert body["join"] == ["A", "B"]


# -- nullary / list operators ------------------------------------------------


@pytest.mark.parametrize("op", ["is", "ns"])
def test_nullary_ops_emit_no_data_key(op: str) -> None:
    slot = QueryBuilder().where("EndTime", op).build()["search"]["s1"]
    assert slot == {"field": "EndTime", "type": op}
    assert "data" not in slot


@pytest.mark.parametrize("op", ["in", "nn"])
def test_list_ops_accept_list_and_tuple(op: str) -> None:
    from_list = QueryBuilder().where("Id", op, [1, 2, 3]).build()["search"]["s1"]
    assert from_list["data"] == [1, 2, 3]
    from_tuple = QueryBuilder().where("Id", op, (1, 2)).build()["search"]["s1"]
    assert from_tuple["data"] == [1, 2]


@pytest.mark.parametrize("op", ["in", "nn"])
def test_list_ops_reject_scalar(op: str) -> None:
    with pytest.raises(ValueError, match="requires a list"):
        QueryBuilder().where("Id", op, 5)


# -- validation --------------------------------------------------------------


def test_valid_ops_cover_documented_table() -> None:
    documented = {"eq", "ne", "gt", "ge", "lt", "le", "lk", "nk", "in", "nn", "is", "ns"}
    assert documented <= VALID_OPS


def test_unsupported_operator_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported operator"):
        QueryBuilder().where("Id", "like", 1)


def test_missing_value_for_binary_op_rejected() -> None:
    with pytest.raises(ValueError, match="requires a value"):
        QueryBuilder().where("Id", "eq")


def test_empty_field_rejected() -> None:
    with pytest.raises(ValueError, match="field"):
        QueryBuilder().where("", "eq", 1)


@pytest.mark.parametrize("bad", [0, MAX_PAGE + 1, -1])
def test_max_out_of_range_rejected(bad: int) -> None:
    with pytest.raises(ValueError, match="max must be"):
        QueryBuilder().max(bad)


def test_max_boundary_accepted() -> None:
    assert QueryBuilder().max(MAX_PAGE).build()["max"] == MAX_PAGE
    assert QueryBuilder().max(1).build()["max"] == 1


def test_negative_start_rejected() -> None:
    with pytest.raises(ValueError, match="start offset"):
        QueryBuilder().start(-1)


def test_empty_join_name_rejected() -> None:
    with pytest.raises(ValueError, match="association name"):
        QueryBuilder().join("")


# -- query_all pagination ----------------------------------------------------


class _FakeRequester:
    """Records requests and returns paginated dict lists based on the ``start`` offset."""

    def __init__(self, pages: dict[int, list[dict[str, Any]]]) -> None:
        self._pages = pages
        self.calls: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
        cacheable: bool = False,
        idempotent: bool = False,
    ) -> Any:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "body": json_body,
                "cacheable": cacheable,
                "idempotent": idempotent,
            }
        )
        offset = int(json_body["start"]) if json_body else 0
        return self._pages.get(offset, [])


def _records(n: int, start_id: int = 0) -> list[dict[str, Any]]:
    return [{"Id": start_id + i} for i in range(n)]


async def test_query_all_paginates_past_500() -> None:
    pages = {
        0: _records(MAX_PAGE, 0),
        500: _records(MAX_PAGE, 500),
        1000: _records(10, 1000),  # short page ends pagination
    }
    fake = _FakeRequester(pages)
    result = await query_all(fake, "Roster", QueryBuilder())
    assert len(result) == 1010
    # Three POSTs to the QUERY endpoint, walking start offsets.
    assert [c["path"] for c in fake.calls] == ["/resource/Roster/QUERY"] * 3
    assert [c["method"] for c in fake.calls] == ["POST", "POST", "POST"]
    assert [c["body"]["start"] for c in fake.calls] == [0, 500, 1000]
    assert all(c["body"]["max"] == MAX_PAGE for c in fake.calls)
    assert all(c["cacheable"] for c in fake.calls)


async def test_query_all_stops_on_short_first_page() -> None:
    fake = _FakeRequester({0: _records(3)})
    result = await query_all(fake, "Employee", QueryBuilder())
    assert len(result) == 3
    assert len(fake.calls) == 1


async def test_query_all_respects_hard_limit() -> None:
    pages = {0: _records(MAX_PAGE, 0), 500: _records(MAX_PAGE, 500)}
    fake = _FakeRequester(pages)
    result = await query_all(fake, "Roster", QueryBuilder(), hard_limit=700)
    assert len(result) == 700
    # Second full page is fetched, then the hard limit stops the loop.
    assert len(fake.calls) == 2


async def test_query_all_marks_query_idempotent() -> None:
    # QUERY is a POST but reads nothing, so it must be replay-safe (retryable).
    fake = _FakeRequester({0: _records(3)})
    await query_all(fake, "Roster", QueryBuilder())
    assert fake.calls[0]["method"] == "POST"
    assert fake.calls[0]["idempotent"] is True


async def test_query_all_warns_when_truncated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Deputy keeps returning full pages while we hit the hard limit -> truncation.
    pages = {0: _records(MAX_PAGE, 0), 500: _records(MAX_PAGE, 500)}
    fake = _FakeRequester(pages)
    with caplog.at_level("WARNING", logger="deputy_mcp.client.query"):
        result = await query_all(fake, "Roster", QueryBuilder(), hard_limit=700)
    assert len(result) == 700
    assert any("truncated" in rec.message.lower() for rec in caplog.records)


async def test_query_all_no_warning_when_complete(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A short final page means the set is complete -> no truncation warning.
    fake = _FakeRequester({0: _records(3)})
    with caplog.at_level("WARNING", logger="deputy_mcp.client.query"):
        await query_all(fake, "Employee", QueryBuilder())
    assert not caplog.records


async def test_query_all_honors_builder_page_size() -> None:
    fake = _FakeRequester({0: _records(50)})
    await query_all(fake, "Timesheet", QueryBuilder().max(50))
    assert fake.calls[0]["body"]["max"] == 50


async def test_query_all_stops_on_non_list_response() -> None:
    class _BadRequester(_FakeRequester):
        async def request(self, *args: Any, **kwargs: Any) -> Any:
            await super().request(*args, **kwargs)
            return {"error": "not a list"}

    fake = _BadRequester({})
    result = await query_all(fake, "Roster", QueryBuilder())
    assert result == []
    assert len(fake.calls) == 1


async def test_query_all_skips_non_dict_records() -> None:
    fake = _FakeRequester({0: [{"Id": 1}, "junk", {"Id": 2}]})  # type: ignore[list-item]
    result = await query_all(fake, "Roster", QueryBuilder())
    assert result == [{"Id": 1}, {"Id": 2}]
