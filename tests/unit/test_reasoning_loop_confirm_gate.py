"""Safety verification: the confirm gate survives ``.as_tool()`` (W1 slice 2).

The reasoning loop wires each specialist as a callable ``agent.as_tool()`` instead
of a handoff target. ``.as_tool()`` runs the specialist as a NESTED
``Runner.run`` inside the orchestrator's tool call. A gated write (e.g. Gmail
send) calls ``confirm_or_abort`` from inside its own body, and the live chat path
surfaces the approval card via a per-run **emitter** bound with
``set_confirm_emitter`` (gotcha #4). That emitter is a ``ContextVar``.

The risk this file closes: does the emitter set before the OUTER run still reach
``confirm_or_abort`` when it fires deep inside the INNER (as_tool) sub-run? If it
did NOT, a gated write inside a specialist-tool would silently never show its
card and time out at 300s — a safety regression. These tests drive a REAL nested
``.as_tool()`` run with a stub model and prove:

* the gate surfaces via the emitter from inside the sub-run, and
* the side effect does NOT happen before approval and DOES after.

No real LLM is used — a minimal fake ``Model`` drives the tool calls.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from agents import Agent, ModelResponse, Runner, function_tool
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from helixis.agents import tools as agent_tools


class _ToolThenFinishModel(Model):
    """Stub model: call the one non-base tool it's offered once, then finish.

    Serves BOTH the orchestrator (which is offered the ``email_specialist``
    as_tool) and the inner specialist (offered ``gated_write``). It picks by the
    tool set it's handed, so a single instance drives the whole nested run. Once
    a ``function_call_output`` is present in that run's input, it emits a final
    message instead of calling again.
    """

    # Arguments each tool expects: the default ``.as_tool()`` wrapper takes an
    # ``input`` string; our gated write takes a ``recipient``.
    _ARGS = {
        "email_specialist": {"input": "email howard"},
        "gated_write": {"recipient": "howard@example.com"},
    }

    def __init__(self, tool_name: str) -> None:
        self._tool_name = tool_name

    async def get_response(
        self,
        system_instructions,
        input,
        model_settings,
        tools,
        output_schema,
        handoffs,
        tracing,
        *,
        previous_response_id,
        conversation_id,
        prompt,
    ) -> ModelResponse:
        tool_names = {t.name for t in tools}
        already_ran = isinstance(input, list) and any(
            (getattr(i, "type", None) == "function_call_output")
            or (isinstance(i, dict) and i.get("type") == "function_call_output")
            for i in input
        )
        if not already_ran and self._tool_name in tool_names:
            call = ResponseFunctionToolCall(
                type="function_call",
                call_id="call-1",
                name=self._tool_name,
                arguments=json.dumps(self._ARGS[self._tool_name]),
            )
            return ModelResponse(output=[call], usage=Usage(), response_id=None, request_id=None)
        msg = ResponseOutputMessage(
            id="msg-1",
            type="message",
            role="assistant",
            status="completed",
            content=[ResponseOutputText(type="output_text", text="done", annotations=[])],
        )
        return ModelResponse(output=[msg], usage=Usage(), response_id=None, request_id=None)

    async def stream_response(self, *args, **kwargs):  # pragma: no cover - unused
        raise NotImplementedError


async def _wait_until(predicate, *, timeout: float = 3.0) -> bool:
    deadline = timeout
    while deadline > 0:
        if predicate():
            return True
        await asyncio.sleep(0.01)
        deadline -= 0.01
    return predicate()


def _build_specialist_tool(sent: list[str]):
    """A specialist agent whose only tool is a gated write, exposed as_tool()."""

    @function_tool
    async def gated_write(recipient: str) -> str:
        # The side effect is gated: it must NOT run unless the user approves.
        approved = await agent_tools.confirm_or_abort(
            action="gmail_send_email",
            title="Send this email?",
            details=f"To: {recipient}",
            items=[recipient],
            timeout=5.0,
        )
        if not approved:
            return "cancelled — nothing was sent"
        sent.append(recipient)
        return f"sent to {recipient}"

    specialist = Agent(
        name="gmail_agent",
        instructions="Send the email the user asked for.",
        model=_ToolThenFinishModel("gated_write"),
        tools=[gated_write],
    )
    return specialist.as_tool(
        tool_name="email_specialist",
        tool_description="Delegate email tasks to the Gmail specialist.",
    )


@pytest.mark.asyncio
async def test_confirm_gate_emitter_fires_from_inside_as_tool_subrun() -> None:
    """The per-run emitter reaches confirm_or_abort even when it fires inside a
    nested ``.as_tool()`` sub-run — approval unblocks the write, which runs only
    after."""
    await agent_tools.drain_pending_confirmations()  # clear residue
    sent: list[str] = []
    emitted: list[dict] = []

    async def emitter(payload: dict) -> None:
        emitted.append(payload)

    orchestrator = Agent(
        name="Helixis",
        instructions="Route to the specialist.",
        model=_ToolThenFinishModel("email_specialist"),
        tools=[_build_specialist_tool(sent)],
    )

    token = agent_tools.set_confirm_emitter(emitter)
    try:
        run = asyncio.create_task(Runner.run(orchestrator, "email howard", max_turns=8))

        # The gate surfaced via the EMITTER (the live SSE path), not the fallback
        # queue — proving the contextvar-bound emitter crossed into the sub-run.
        assert await _wait_until(lambda: len(emitted) == 1), (
            "confirm gate never surfaced via the emitter from inside the as_tool sub-run"
        )
        assert emitted[0]["action"] == "gmail_send_email"
        confirm_id = emitted[0]["confirm_id"]

        # Emitter path must not also enqueue (that would double-render the card).
        assert await agent_tools.drain_pending_confirmations() == []

        # The side effect has NOT happened yet — the write is blocked on approval.
        assert sent == []
        assert not run.done()

        # Approve → the gated write runs and the whole nested run completes.
        assert await _wait_until(lambda: confirm_id in agent_tools._pending_confirmations)
        assert await agent_tools.resolve_confirmation(confirm_id, True) == (
            agent_tools.RESOLVE_RESOLVED
        )
        result = await asyncio.wait_for(run, timeout=5.0)
        assert sent == ["howard@example.com"], "approved write did not run"
        assert result.final_output  # orchestrator produced a reply
    finally:
        agent_tools.reset_confirm_emitter(token)


@pytest.mark.asyncio
async def test_denial_inside_as_tool_subrun_blocks_the_write() -> None:
    """Denying the gate inside the sub-run must leave the side effect un-done."""
    await agent_tools.drain_pending_confirmations()
    sent: list[str] = []
    emitted: list[dict] = []

    async def emitter(payload: dict) -> None:
        emitted.append(payload)

    orchestrator = Agent(
        name="Helixis",
        instructions="Route to the specialist.",
        model=_ToolThenFinishModel("email_specialist"),
        tools=[_build_specialist_tool(sent)],
    )

    token = agent_tools.set_confirm_emitter(emitter)
    try:
        run = asyncio.create_task(Runner.run(orchestrator, "email howard", max_turns=8))
        assert await _wait_until(lambda: len(emitted) == 1)
        confirm_id = emitted[0]["confirm_id"]
        assert await _wait_until(lambda: confirm_id in agent_tools._pending_confirmations)

        # DENY → the write must never run.
        assert await agent_tools.resolve_confirmation(confirm_id, False) == (
            agent_tools.RESOLVE_RESOLVED
        )
        await asyncio.wait_for(run, timeout=5.0)
        assert sent == [], "a denied gate must not perform the side effect"
    finally:
        agent_tools.reset_confirm_emitter(token)
