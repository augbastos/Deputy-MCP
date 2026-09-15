"""The confirmation gate's decision table, driven with a stand-in request context.

The end-to-end behaviour on both protocol eras is covered through a real FastMCP
client in ``tests/server/test_tools_write.py``; this file pins the edge cases a client
cannot easily produce: an approval replayed for a different action, and malformed or
negative answers on a 2026-07-28 connection.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from mcp.types import InputRequiredResult

from deputy_mcp.server._confirm import confirm


def _modern_ctx(*, answer: Any = None, state: str | None = None) -> Any:
    responses = None if answer is None else {"confirm": answer}
    return SimpleNamespace(
        request_context=SimpleNamespace(protocol_version="2026-07-28"),
        input_responses=responses,
        request_state=state,
    )


async def test_first_round_asks_and_binds_the_action() -> None:
    result = await confirm(_modern_ctx(), "Claim shift #1?", action_id="claim-open-shift:1")
    assert isinstance(result, InputRequiredResult)
    assert result.request_state == "claim-open-shift:1"
    request = result.input_requests["confirm"]
    assert request.params.message == "Claim shift #1?"


async def test_explicit_yes_for_the_same_action_is_approved() -> None:
    answer = SimpleNamespace(action="accept", content={"value": True})
    ctx = _modern_ctx(answer=answer, state="claim-open-shift:1")
    assert await confirm(ctx, "?", action_id="claim-open-shift:1") == "approved"


async def test_an_approval_for_another_action_asks_again() -> None:
    answer = SimpleNamespace(action="accept", content={"value": True})
    ctx = _modern_ctx(answer=answer, state="claim-open-shift:1")
    result = await confirm(ctx, "?", action_id="claim-open-shift:2")
    assert isinstance(result, InputRequiredResult)


@pytest.mark.parametrize(
    "answer",
    [
        SimpleNamespace(action="decline", content=None),
        SimpleNamespace(action="cancel", content=None),
        SimpleNamespace(action="accept", content={"value": False}),
        SimpleNamespace(action="accept", content={"value": "yes please"}),
        SimpleNamespace(action="accept", content=None),
    ],
)
async def test_anything_but_an_explicit_yes_is_declined(answer: Any) -> None:
    ctx = _modern_ctx(answer=answer, state="claim-open-shift:1")
    assert await confirm(ctx, "?", action_id="claim-open-shift:1") == "declined"
