"""Durable confirmation layer (migration 0022).

The in-process ``Future`` is the authoritative fast path; these tests pin the
durability layer underneath it — the part that makes a restart-orphaned confirm
fail honestly instead of as a silent no-op, and records every decision.

A :class:`FakeConfirmationStore` mirrors the SQL semantics (terminal
transitions only fire while a row is ``pending``) so the path runs in CI with
no Postgres. It is injected by monkeypatching ``tools._durable_store``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from helixis.agents import tools as agent_tools
from helixis.db.repositories import pending_confirmations as pc
from helixis.memory.context import memory_context


class FakeConfirmationStore:
    """In-memory stand-in implementing the ``ConfirmationStore`` Protocol."""

    def __init__(self) -> None:
        self.rows: dict[str, pc.ConfirmationRow] = {}

    async def create(
        self,
        *,
        confirm_id,
        company_id,
        session_id,
        user_id,
        action,
        title,
        details,
        items,
        timeout_seconds,
    ) -> None:
        self.rows.setdefault(
            confirm_id,
            pc.ConfirmationRow(
                confirm_id=confirm_id,
                company_id=company_id,
                session_id=session_id,
                user_id=user_id,
                action=action,
                status=pc.STATUS_PENDING,
                created_at=datetime.now(UTC),
                resolved_at=None,
            ),
        )

    def _terminate(self, confirm_id: str, status: str) -> pc.ConfirmationRow | None:
        row = self.rows.get(confirm_id)
        if row is None or row.status != pc.STATUS_PENDING:
            return None
        row.status = status
        row.resolved_at = datetime.now(UTC)
        return row

    async def resolve(self, *, confirm_id, approved) -> pc.ConfirmationRow | None:
        return self._terminate(confirm_id, pc.STATUS_APPROVED if approved else pc.STATUS_DENIED)

    async def mark_expired(self, *, confirm_id) -> pc.ConfirmationRow | None:
        return self._terminate(confirm_id, pc.STATUS_EXPIRED)

    async def mark_orphaned(self, *, confirm_id) -> pc.ConfirmationRow | None:
        return self._terminate(confirm_id, pc.STATUS_ORPHANED)

    async def orphan_all_pending(self) -> int:
        n = 0
        for row in self.rows.values():
            if row.status == pc.STATUS_PENDING:
                row.status = pc.STATUS_ORPHANED
                row.resolved_at = datetime.now(UTC)
                n += 1
        return n

    async def expire_stale(self) -> int:  # pragma: no cover - not exercised here
        return 0


@pytest.fixture
def fake_store(monkeypatch) -> FakeConfirmationStore:
    store = FakeConfirmationStore()
    monkeypatch.setattr(agent_tools, "_durable_store", lambda: store)
    return store


@pytest.mark.asyncio
async def test_confirm_persists_pending_then_resolves(fake_store: FakeConfirmationStore) -> None:
    """A surfaced confirm writes a ``pending`` row; an approval from a live
    waiter flips it to ``approved`` and reports ``resolved``."""
    await agent_tools.drain_pending_confirmations()
    with memory_context(company_id="co-1", user_id="u-1", session_id="s-1"):
        task = asyncio.create_task(
            agent_tools.confirm_or_abort(
                action="buildium_create_task", title="Create?", details="A task", timeout=5.0
            )
        )
        await asyncio.sleep(0)  # let it persist + register the future
        drained = await agent_tools.drain_pending_confirmations()
        confirm_id = drained[0]["confirm_id"]
        # Durable row exists and is pending.
        assert fake_store.rows[confirm_id].status == pc.STATUS_PENDING

        resolve = await agent_tools.resolve_confirmation(confirm_id, True)
        assert resolve == agent_tools.RESOLVE_RESOLVED
        assert await asyncio.wait_for(task, timeout=1.0) is True
        assert fake_store.rows[confirm_id].status == pc.STATUS_APPROVED


@pytest.mark.asyncio
async def test_resolve_without_live_waiter_is_orphaned(fake_store: FakeConfirmationStore) -> None:
    """A pending row whose live waiter is gone (simulated restart) resolves to
    ``orphaned`` — the honest 'the run died, re-issue' signal."""
    confirm_id = "11111111-1111-1111-1111-111111111111"
    await fake_store.create(
        confirm_id=confirm_id,
        company_id="co-1",
        session_id="s-1",
        user_id="u-1",
        action="buildium_create_task",
        title="Create?",
        details="A task",
        items=[],
        timeout_seconds=300,
    )
    # No future was ever registered for this id → no live waiter.
    resolve = await agent_tools.resolve_confirmation(confirm_id, True)
    assert resolve == agent_tools.RESOLVE_ORPHANED
    assert fake_store.rows[confirm_id].status == pc.STATUS_ORPHANED


@pytest.mark.asyncio
async def test_resolve_unknown_id_is_unknown(fake_store: FakeConfirmationStore) -> None:
    """Unknown id, no durable row, no live waiter → benign ``unknown``."""
    resolve = await agent_tools.resolve_confirmation("does-not-exist", False)
    assert resolve == agent_tools.RESOLVE_UNKNOWN


@pytest.mark.asyncio
async def test_timeout_marks_expired(fake_store: FakeConfirmationStore) -> None:
    """No answer within the window → the durable row is marked ``expired``."""
    await agent_tools.drain_pending_confirmations()
    with memory_context(company_id="co-1", user_id="u-1", session_id="s-1"):
        approved = await agent_tools.confirm_or_abort(
            action="send_email", title="Send?", details="x", timeout=0.05
        )
    assert approved is False
    drained = await agent_tools.drain_pending_confirmations()
    confirm_id = drained[0]["confirm_id"]
    assert fake_store.rows[confirm_id].status == pc.STATUS_EXPIRED


@pytest.mark.asyncio
async def test_no_store_degrades_to_live_only(monkeypatch) -> None:
    """With no durable store, the gate behaves exactly as pre-0022: a live
    waiter still resolves; an absent waiter is a benign ``unknown``."""
    monkeypatch.setattr(agent_tools, "_durable_store", lambda: None)
    await agent_tools.drain_pending_confirmations()
    with memory_context(company_id="co-1", user_id="u-1", session_id="s-1"):
        task = asyncio.create_task(
            agent_tools.confirm_or_abort(action="x", title="t", details="d", timeout=5.0)
        )
        await asyncio.sleep(0)
        confirm_id = (await agent_tools.drain_pending_confirmations())[0]["confirm_id"]
        resolve = await agent_tools.resolve_confirmation(confirm_id, True)
        assert resolve == agent_tools.RESOLVE_RESOLVED
        assert await asyncio.wait_for(task, timeout=1.0) is True
    # Unknown id with no store → unknown (not a crash).
    assert await agent_tools.resolve_confirmation("nope", True) == agent_tools.RESOLVE_UNKNOWN
