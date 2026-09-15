"""Human confirmation for high-impact write tools, using MCP elicitation on both eras.

``DEPUTY_ALLOW_WRITES`` decides whether write tools exist at all. This is the second,
narrower gate for the actions whose effect a person should see before it happens: the
tool asks the *human* through the MCP client instead of trusting the model's intent.

MCP offers this natively, but differently per protocol era, and this module is a thin
bridge over what FastMCP 4 already provides:

* **handshake era (<= 2025-11-25)** — the server sends an ``elicitation/create`` request
  mid-call (:meth:`fastmcp.Context.elicit`).
* **2026-07-28** — there is no back-channel. The tool returns an ``InputRequiredResult``
  describing the question; the client asks the user and re-invokes the tool with the
  answer in ``input_responses`` (SEP-2322). The approval is bound to the exact action
  through ``request_state``, which FastMCP seals, so an answer given for one shift can
  never approve another.

The gate fails closed: a declined or cancelled prompt, an unparseable answer, or a client
without elicitation support all mean the action does not run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from fastmcp import Context
    from mcp.types import InputRequiredResult

__all__ = ["Decision", "confirm"]

#: Outcome of a confirmation round that did not need another round-trip.
Decision = Literal["approved", "declined", "unsupported"]

#: Key of the single confirmation question in ``input_requests`` / ``input_responses``.
_CONFIRM_KEY = "confirm"
_TITLE = "Confirm"


async def confirm(ctx: Context, message: str, *, action_id: str) -> Decision | InputRequiredResult:
    """Ask the human to approve ``message``; return the decision or the next-round result.

    Args:
        ctx: The FastMCP request context of the tool call.
        message: What will happen, in plain language, shown to the user by the client.
        action_id: A stable identifier of the exact action (e.g. ``"claim:9001"``) that
            the approval is bound to on 2026-07-28 connections.

    Returns:
        ``"approved"`` only for an explicit yes. An ``InputRequiredResult`` means the tool
        must return it unchanged so the client can ask the user and call again.
    """
    from fastmcp.server.elicitation import handle_elicit_accept, parse_elicit_response_type
    from mcp.shared.exceptions import MCPError
    from mcp.types import ElicitRequest, ElicitRequestFormParams, InputRequiredResult
    from mcp_types.version import MODERN_PROTOCOL_VERSIONS

    config = parse_elicit_response_type(bool, response_title=_TITLE)
    request = ctx.request_context
    if request is not None and request.protocol_version in MODERN_PROTOCOL_VERSIONS:
        responses = ctx.input_responses
        answer = responses.get(_CONFIRM_KEY) if responses is not None else None
        if answer is None or ctx.request_state != action_id:
            return InputRequiredResult(
                result_type="input_required",
                input_requests={
                    _CONFIRM_KEY: ElicitRequest(
                        method="elicitation/create",
                        params=ElicitRequestFormParams(
                            message=message, requested_schema=config.schema
                        ),
                    )
                },
                request_state=action_id,
            )
        if getattr(answer, "action", None) != "accept":
            return "declined"
        try:
            accepted = handle_elicit_accept(config, getattr(answer, "content", None))
        except ValueError:
            return "declined"
        return "approved" if accepted.data is True else "declined"

    try:
        result = await ctx.elicit(message, response_type=bool, response_title=_TITLE)
    except MCPError:
        # The client declared no elicitation support (or rejected the request).
        return "unsupported"
    if result.action == "accept" and getattr(result, "data", None) is True:
        return "approved"
    return "declined"
