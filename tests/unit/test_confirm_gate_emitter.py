"""F1 regression — the confirm gate must surface while the tool is blocked.

The live streaming path drains the confirm-emission queue only BETWEEN the
orchestrator's SDK stream events. But ``confirm_or_abort`` enqueues its payload
and then blocks inside the SAME tool coroutine, so no further stream event
arrives and the drain never runs — the gate was never shown and the tool timed
out after 300s (the "email send hangs, no gate" bug).

The fix binds a per-run *emitter* that publishes the gate straight to the SSE
bus the instant it's raised. These tests prove the emitter surfaces the gate
WITHOUT anything draining the queue, that it doesn't double-enqueue, that a
missing emitter still falls back to the queue, and that an emitter failure
falls back too.
"""

from __future__ import annotations

import asyncio

import pytest

from helixis.agents import tools as agent_tools


async def _wait_until(predicate, *, timeout: float = 2.0) -> bool:
    """Poll ``predicate`` (returns bool) until true or timeout."""
    deadline = timeout
    step = 0.01
    while deadline > 0:
        if predicate():
            return True
        await asyncio.sleep(step)
        deadline -= step
    return predicate()


@pytest.mark.asyncio
async def test_emitter_surfaces_gate_while_tool_blocks_without_any_drain() -> None:
    await agent_tools.drain_pending_confirmations()  # clear residue

    emitted: list[dict] = []

    async def emitter(payload: dict) -> None:
        emitted.append(payload)

    token = agent_tools.set_confirm_emitter(emitter)
    try:
        task = asyncio.create_task(
            agent_tools.confirm_or_abort(
                action="gmail_send_email",
                title="Send this email?",
                details="To: howard@example.com",
                items=[],
                timeout=5.0,
            )
        )
        # The gate surfaces via the emitter as the tool blocks — no drain ran.
        assert await _wait_until(lambda: len(emitted) == 1), "gate never surfaced via emitter"
        confirm_id = emitted[0]["confirm_id"]

        # The queue stayed EMPTY — the emitter path must not also enqueue
        # (that would double-render the gate once the drain finally runs).
        assert await agent_tools.drain_pending_confirmations() == []

        # The tool is genuinely blocked awaiting the user's decision.
        assert not task.done()

        # Wait for the Future to register, then approve → the tool unblocks.
        assert await _wait_until(lambda: confirm_id in agent_tools._pending_confirmations)
        resolve = await agent_tools.resolve_confirmation(confirm_id, True)
        assert resolve == agent_tools.RESOLVE_RESOLVED
        assert await asyncio.wait_for(task, timeout=2.0) is True
    finally:
        agent_tools.reset_confirm_emitter(token)


@pytest.mark.asyncio
async def test_no_emitter_falls_back_to_the_queue() -> None:
    await agent_tools.drain_pending_confirmations()  # clear residue

    task = asyncio.create_task(
        agent_tools.confirm_or_abort(
            action="buildium_create_task", title="Create?", details="d", items=[], timeout=5.0
        )
    )
    payload: dict | None = None
    for _ in range(200):
        drained = await agent_tools.drain_pending_confirmations()
        if drained:
            payload = drained[0]
            break
        await asyncio.sleep(0.01)
    assert payload is not None, "with no emitter the gate must enqueue for the drain path"

    assert await _wait_until(lambda: payload["confirm_id"] in agent_tools._pending_confirmations)
    assert await agent_tools.resolve_confirmation(payload["confirm_id"], False) == (
        agent_tools.RESOLVE_RESOLVED
    )
    assert await asyncio.wait_for(task, timeout=2.0) is False


@pytest.mark.asyncio
async def test_emitter_failure_falls_back_to_the_queue() -> None:
    await agent_tools.drain_pending_confirmations()  # clear residue

    async def bad_emitter(_payload: dict) -> None:
        raise RuntimeError("bus down")

    token = agent_tools.set_confirm_emitter(bad_emitter)
    try:
        task = asyncio.create_task(
            agent_tools.confirm_or_abort(
                action="gmail_send_email", title="Send?", details="d", items=[], timeout=5.0
            )
        )
        payload: dict | None = None
        for _ in range(200):
            drained = await agent_tools.drain_pending_confirmations()
            if drained:
                payload = drained[0]
                break
            await asyncio.sleep(0.01)
        assert payload is not None, (
            "an emit failure must fall back to the queue, never skip the gate"
        )

        assert await _wait_until(
            lambda: payload["confirm_id"] in agent_tools._pending_confirmations
        )
        assert await agent_tools.resolve_confirmation(payload["confirm_id"], True) == (
            agent_tools.RESOLVE_RESOLVED
        )
        assert await asyncio.wait_for(task, timeout=2.0) is True
    finally:
        agent_tools.reset_confirm_emitter(token)
