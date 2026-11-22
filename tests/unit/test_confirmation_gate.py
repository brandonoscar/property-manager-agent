"""Confirm-gate emission/resolve plumbing.

Pins the queue-based emission path shared by every gated write
(``confirm_or_abort``, the Buildium tool bodies, and the Composio
write-gate wrapper):

1. A gated call pushes a payload onto the shared queue before awaiting.
2. ``drain_pending_confirmations`` returns the queued payloads
   non-blockingly (the orchestrator drains after each agent step).
3. ``resolve_confirmation(confirm_id, True/False)`` unblocks the
   awaiting tool and the return value matches.
"""

from __future__ import annotations

import asyncio

import pytest

from helixis.agents import tools as agent_tools


@pytest.mark.asyncio
async def test_confirm_payload_pushes_and_resolves_approved() -> None:
    """Full happy path: a gated call awaits, drain finds the payload,
    resolve_confirmation(confirm_id, True) unblocks the wait."""
    # Drain anything left over from another test in the same process.
    await agent_tools.drain_pending_confirmations()

    # Exercise the underlying confirm infra directly via the helper below
    # rather than constructing a FunctionTool harness.
    confirm_id = _push_test_payload(action="send_email", title="Send", details="Email A")
    pending = asyncio.create_task(
        agent_tools._wait_for_confirmation(confirm_id)  # noqa: SLF001
    )
    # Yield once so the task enters its wait_for and registers the
    # future in ``_pending_confirmations``.
    await asyncio.sleep(0)
    # Drain — the payload pushed by the helper must come back.
    drained = await agent_tools.drain_pending_confirmations()
    assert len(drained) == 1
    assert drained[0]["confirm_id"] == confirm_id
    assert drained[0]["title"] == "Send"

    # Resolving the future unblocks the awaiting task and reports that a live
    # waiter received the answer.
    resolve = await agent_tools.resolve_confirmation(confirm_id, True)
    assert resolve == agent_tools.RESOLVE_RESOLVED
    # edit-before-approve: the waiter now returns (approved, edits).
    result = await asyncio.wait_for(pending, timeout=1.0)
    assert result == (True, None)


def _push_test_payload(*, action: str, title: str, details: str) -> str:
    """Mirror what ``confirm_or_abort`` pushes onto the queue without
    invoking a real gated tool. Returns the confirm_id so tests can
    resolve it later."""
    import uuid

    confirm_id = str(uuid.uuid4())
    agent_tools._pending_confirm_emissions.put_nowait(  # noqa: SLF001
        {
            "confirm_id": confirm_id,
            "action": action,
            "title": title,
            "details": details,
            "items": [],
        }
    )
    return confirm_id


@pytest.mark.asyncio
async def test_resolve_confirmation_false_returns_denied() -> None:
    """Symmetric path — explicit denial unblocks the future with False."""
    await agent_tools.drain_pending_confirmations()
    confirm_id = _push_test_payload(action="delete", title="Delete row", details="...")
    pending = asyncio.create_task(
        agent_tools._wait_for_confirmation(confirm_id)  # noqa: SLF001
    )
    await asyncio.sleep(0)
    resolve = await agent_tools.resolve_confirmation(confirm_id, False)
    assert resolve == agent_tools.RESOLVE_RESOLVED
    # edit-before-approve: the waiter now returns (approved, edits).
    result = await asyncio.wait_for(pending, timeout=1.0)
    assert result == (False, None)
    # Queue drained.
    await agent_tools.drain_pending_confirmations()


@pytest.mark.asyncio
async def test_drain_pending_confirmations_is_non_blocking_when_empty() -> None:
    """The drain helper must never block — it's called in the
    orchestrator's hot path after every stream event."""
    await agent_tools.drain_pending_confirmations()  # ensure empty
    drained = await asyncio.wait_for(agent_tools.drain_pending_confirmations(), timeout=0.1)
    assert drained == []


@pytest.mark.asyncio
async def test_drain_returns_in_fifo_order() -> None:
    """Multiple pending confirmations come out in the order they were
    queued — important for a multi-action chain like 'send these 3
    emails' where the model expects approvals in the order requested."""
    await agent_tools.drain_pending_confirmations()
    ids = []
    for i in range(3):
        ids.append(_push_test_payload(action="send", title=f"#{i}", details=f"row {i}"))
    drained = await agent_tools.drain_pending_confirmations()
    assert [d["confirm_id"] for d in drained] == ids


# ── edit-before-approve (UI/UX day item 1, 2026-08-19) ──────────────


@pytest.mark.asyncio
async def test_approval_with_edits_reaches_the_waiter_and_contextvar() -> None:
    """An approval carrying edits resolves the waiter with them, and
    ``confirm_or_abort`` publishes only DECLARED-editable fields to
    ``current_confirm_edits`` (values length-capped)."""
    await agent_tools.drain_pending_confirmations()

    # The executor reads the contextvar in the SAME task that ran the gate —
    # exactly how run_guarded's execute() sees it in production (a contextvar
    # set inside a child task never leaks back to the test's context).
    async def gated() -> tuple[bool, dict | None]:
        approved = await agent_tools.confirm_or_abort(
            action="GMAIL_SEND_EMAIL",
            title="Send?",
            details="...",
            request_body={"subject": "s", "body": "original"},
            editable_fields=["body"],
        )
        return approved, agent_tools.current_confirm_edits.get()

    task = asyncio.create_task(gated())
    await asyncio.sleep(0)
    drained = await agent_tools.drain_pending_confirmations()
    assert drained[0]["editable_fields"] == ["body"]
    confirm_id = drained[0]["confirm_id"]

    resolve = await agent_tools.resolve_confirmation(
        confirm_id,
        True,
        edits={"body": "edited by the approver", "recipient": "evil@x.com"},
    )
    assert resolve == agent_tools.RESOLVE_RESOLVED
    approved, published = await asyncio.wait_for(task, timeout=1.0)
    assert approved is True
    # Only the declared-editable field survives; the recipient edit is dropped.
    assert published == {"body": "edited by the approver"}


@pytest.mark.asyncio
async def test_decline_never_publishes_edits() -> None:
    await agent_tools.drain_pending_confirmations()

    async def gated() -> tuple[bool, dict | None]:
        # A stale value from an earlier gate on this task must be cleared by
        # the next gate (reset-first), so the executor can never see it.
        agent_tools.current_confirm_edits.set({"body": "stale"})
        approved = await agent_tools.confirm_or_abort(
            action="GMAIL_SEND_EMAIL",
            title="Send?",
            details="...",
            editable_fields=["body"],
        )
        return approved, agent_tools.current_confirm_edits.get()

    task = asyncio.create_task(gated())
    await asyncio.sleep(0)
    drained = await agent_tools.drain_pending_confirmations()
    await agent_tools.resolve_confirmation(
        drained[0]["confirm_id"], False, edits={"body": "should not survive"}
    )
    approved, published = await asyncio.wait_for(task, timeout=1.0)
    assert approved is False
    assert published is None


@pytest.mark.asyncio
async def test_read_only_gate_ignores_edits_entirely() -> None:
    """A gate that declared NO editable fields (e.g. every SMS gate) discards
    any edits an approval smuggles in — templates-only stays structural."""
    await agent_tools.drain_pending_confirmations()

    async def gated() -> tuple[bool, dict | None]:
        approved = await agent_tools.confirm_or_abort(
            action="send_text_message",
            title="Send this text message?",
            details="...",
            request_body={"message": "template output"},
        )
        return approved, agent_tools.current_confirm_edits.get()

    task = asyncio.create_task(gated())
    await asyncio.sleep(0)
    drained = await agent_tools.drain_pending_confirmations()
    assert "editable_fields" not in drained[0]
    await agent_tools.resolve_confirmation(
        drained[0]["confirm_id"], True, edits={"message": "freeform override"}
    )
    approved, published = await asyncio.wait_for(task, timeout=1.0)
    assert approved is True
    assert published is None
