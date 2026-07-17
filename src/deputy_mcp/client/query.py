"""Deputy Resource-API QUERY DSL builder and pagination helper.

Deputy's ``POST /api/v1/resource/{Object}/QUERY`` is the primary read mechanism. This
module builds the exact JSON body documented in ``deputy-api-read.md`` and paginates
around the hard 500-record-per-response cap. It never touches ``httpx`` directly: the
transport is passed in and used through the small :class:`_HTTPRequester` protocol, so
this module stays reusable and cheap to unit-test.

QUERY body shape (all keys optional; an empty body returns the first 500 records)::

    {
      "search": { "s1": {"field": ..., "type": <op>, "data": ...}, "s2": {...} },
      "sort":   { "<Field>": "asc" | "desc" },
      "join":   ["<Association>"],
      "max":    <int <= 500>,
      "start":  <int offset>
    }

Search slots ``s1``, ``s2``, ... are AND-ed together (the raw API has no OR/nesting); a
range filter is two slots on the same field. Timestamp comparisons must send the unix
seconds as a string — that formatting is the caller's responsibility (this builder passes
``data`` through untouched, matching the documented examples that send strings, ints,
bools and lists).
"""

from __future__ import annotations

from typing import Any, Protocol

__all__ = ["VALID_OPS", "QueryBuilder", "query_all"]

#: Operator codes accepted by the QUERY ``type`` field, per the read-notes table.
#: (The design lists eleven; ``nk`` — NOT LIKE — is added to match the documented API.)
VALID_OPS: frozenset[str] = frozenset(
    {"eq", "ne", "gt", "ge", "lt", "le", "lk", "nk", "in", "nn", "is", "ns"}
)

#: Operators that take no ``data`` value ("is set" / "is not set").
_NULLARY_OPS: frozenset[str] = frozenset({"is", "ns"})

#: Operators whose ``data`` must be a list.
_LIST_OPS: frozenset[str] = frozenset({"in", "nn"})

#: Deputy's hard per-response record cap.
MAX_PAGE: int = 500

_UNSET: Any = object()


class _HTTPRequester(Protocol):
    """Structural type for the transport (implemented by ``client.http.DeputyHTTP``).

    Declared here so ``query.py`` need not import the transport (or ``httpx``) at runtime;
    any object exposing this ``request`` coroutine works, which keeps the helper testable
    with a trivial fake.
    """

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
        cacheable: bool = False,
    ) -> Any: ...


class QueryBuilder:
    """Fluent builder for a Deputy ``/QUERY`` request body.

    Example::

        body = (
            QueryBuilder()
            .where("StartTime", "le", "1663084585")
            .where("EndTime", "gt", "1663084585")
            .where("Open", "ne", True)
            .join("OperationalUnitObject")
            .sort("StartTime")
            .max(500)
            .build()
        )

    All ``where`` conditions are AND-ed. Call :meth:`build` to get the JSON-ready dict.
    """

    def __init__(self) -> None:
        # Each entry: (field, op, data, has_data).
        self._conditions: list[tuple[str, str, Any, bool]] = []
        self._sort: dict[str, str] = {}
        self._join: list[str] = []
        self._max: int | None = None
        self._start: int | None = None

    def where(self, field: str, op: str, value: Any = _UNSET) -> QueryBuilder:
        """Add an AND-ed search condition.

        ``op`` must be one of :data:`VALID_OPS`. ``in``/``nn`` require a list ``value``;
        ``is``/``ns`` take no value (any passed value is ignored). ``value`` is sent as
        ``data`` verbatim — pass unix timestamps as strings for time comparisons.
        """
        if not field:
            raise ValueError("field must be a non-empty string")
        if op not in VALID_OPS:
            raise ValueError(f"unsupported operator {op!r}; valid operators: {sorted(VALID_OPS)}")

        if op in _NULLARY_OPS:
            self._conditions.append((field, op, None, False))
            return self

        if value is _UNSET:
            raise ValueError(f"operator {op!r} requires a value")
        if op in _LIST_OPS:
            if not isinstance(value, list | tuple):
                raise ValueError(f"operator {op!r} requires a list value")
            data: Any = list(value)
        else:
            data = value
        self._conditions.append((field, op, data, True))
        return self

    def join(self, name: str) -> QueryBuilder:
        """Eager-load an association by name (discover valid names via the object INFO)."""
        if not name:
            raise ValueError("join association name must be non-empty")
        if name not in self._join:
            self._join.append(name)
        return self

    def sort(self, field: str, desc: bool = False) -> QueryBuilder:
        """Sort by ``field`` ascending (default) or descending."""
        if not field:
            raise ValueError("sort field must be non-empty")
        self._sort[field] = "desc" if desc else "asc"
        return self

    def max(self, n: int) -> QueryBuilder:
        """Set the page size (1..500; Deputy caps every response at 500 records)."""
        if n < 1 or n > MAX_PAGE:
            raise ValueError(f"max must be between 1 and {MAX_PAGE} (Deputy hard cap)")
        self._max = n
        return self

    def start(self, n: int) -> QueryBuilder:
        """Set the pagination offset (skip ``n`` records; must be >= 0)."""
        if n < 0:
            raise ValueError("start offset must be >= 0")
        self._start = n
        return self

    def build(self) -> dict[str, Any]:
        """Render the exact Deputy QUERY body. Omits keys that were never set."""
        body: dict[str, Any] = {}
        if self._conditions:
            search: dict[str, dict[str, Any]] = {}
            for index, (field, op, data, has_data) in enumerate(self._conditions, start=1):
                slot: dict[str, Any] = {"field": field, "type": op}
                if has_data:
                    slot["data"] = data
                search[f"s{index}"] = slot
            body["search"] = search
        if self._sort:
            body["sort"] = dict(self._sort)
        if self._join:
            body["join"] = list(self._join)
        if self._max is not None:
            body["max"] = self._max
        if self._start is not None:
            body["start"] = self._start
        return body


async def query_all(
    http: _HTTPRequester,
    object_name: str,
    builder: QueryBuilder,
    hard_limit: int = 2000,
) -> list[dict[str, Any]]:
    """Run a QUERY and auto-paginate past the 500-record cap.

    POSTs ``builder.build()`` to ``/resource/{object_name}/QUERY`` (cacheable), then walks
    ``start`` offsets in ``max``-sized pages until a short page is returned or ``hard_limit``
    records have been collected. Returns the flattened record list (never more than
    ``hard_limit``). Deputy QUERY responses are JSON arrays; a non-list response ends
    pagination defensively.
    """
    body = builder.build()
    page_size = min(int(body.get("max", MAX_PAGE)), MAX_PAGE)
    if page_size < 1:
        page_size = MAX_PAGE
    offset = int(body.get("start", 0))
    path = f"/resource/{object_name}/QUERY"

    results: list[dict[str, Any]] = []
    while len(results) < hard_limit:
        page_body = {**body, "max": page_size, "start": offset}
        page = await http.request("POST", path, json_body=page_body, cacheable=True)
        if not isinstance(page, list):
            break
        results.extend(record for record in page if isinstance(record, dict))
        if len(page) < page_size:
            break
        offset += page_size
    return results[:hard_limit]
