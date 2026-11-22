"""Idempotency guard on the human execute endpoint (issue #224).

``execute_event_action`` had a check-then-act guard: it read
``executed_action_index IS NULL`` and only LATER set it, bracketing the Buildium
write non-atomically. Two concurrent submits (double-click, client retry) could
both pass the NULL check and both fire the write — a TOCTOU double-fire.

These tests cover the extracted ``_execute_action_once`` seam, which wraps the
handler in the durable ``helixis_action_idempotency`` claim so the side effect
fires AT MOST ONCE, and classifies handler failures (validation → retryable,
ambiguous → abandoned-and-never-refired). The primitive's own state machine is
covered in ``test_action_idempotency.py``; this proves the wiring at the call
site, with an in-memory fake store (no Supabase needed in CI).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from helixis.agent.handlers import HandlerError
from helixis.agents.idempotency import PreSideEffectError
from helixis.api.routes.buildium import _execute_action_once
from helixis.db.repositories.action_idempotency import (
    STATUS_ABANDONED_UNKNOWN,
    STATUS_ABORTED,
    STATUS_FAILED_RETRYABLE,
    STATUS_IN_PROGRESS,
    STATUS_SUCCEEDED,
    ClaimResult,
    ClaimRow,
)


class FakeStore:
    """In-memory IdempotencyStore (same shape as the primitive's test fake)."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], ClaimRow] = {}

    async def claim(
        self, *, company_id: str, idem_key: str, action_type: str, window: timedelta | None
    ) -> ClaimResult:
        k = (company_id, idem_key)
        existing = self._rows.get(k)
        if existing is not None:
            return ClaimResult(won=False, row=existing)
        row = ClaimRow(
            company_id=company_id,
            idem_key=idem_key,
            action_type=action_type,
            status=STATUS_IN_PROGRESS,
            result=None,
            error=None,
            attempts=1,
            expires_at=datetime.now(UTC) + window if window else None,
        )
        self._rows[k] = row
        return ClaimResult(won=True, row=row)

    async def _set(self, company_id: str, idem_key: str, **f: Any) -> None:
        row = self._rows[(company_id, idem_key)]
        for key, val in f.items():
            setattr(row, key, val)

    async def finalize_success(self, *, company_id: str, idem_key: str, result: Any) -> None:
        await self._set(company_id, idem_key, status=STATUS_SUCCEEDED, result=result)

    async def finalize_retryable(self, *, company_id: str, idem_key: str, error: str) -> None:
        await self._set(company_id, idem_key, status=STATUS_FAILED_RETRYABLE, error=error)

    async def finalize_abandoned(self, *, company_id: str, idem_key: str, error: str) -> None:
        await self._set(company_id, idem_key, status=STATUS_ABANDONED_UNKNOWN, error=error)

    async def finalize_aborted(self, *, company_id: str, idem_key: str) -> None:
        await self._set(company_id, idem_key, status=STATUS_ABORTED)

    async def reclaim(self, *, company_id: str, idem_key: str) -> bool:
        row = self._rows.get((company_id, idem_key))
        if row is None or row.status not in (STATUS_FAILED_RETRYABLE, STATUS_ABORTED):
            return False
        row.status = STATUS_IN_PROGRESS
        return True

    async def get(self, *, company_id: str, idem_key: str) -> ClaimRow | None:
        return self._rows.get((company_id, idem_key))

    async def reap_stale(self) -> int:
        return 0


def _make_handler(calls: dict[str, int], result: dict[str, Any]):
    async def run_handler() -> dict[str, Any]:
        calls["n"] += 1
        return result

    return run_handler


async def _run(
    store: FakeStore,
    run_handler: Any,
    *,
    company_id: str = "co-1",
    event_id: str = "ev-1",
    action_index: int = 0,
    tool_id: str = "create_followup_task",
    merged_params: dict[str, Any] | None = None,
):
    return await _execute_action_once(
        store=store,
        company_id=company_id,
        event_id=event_id,
        action_index=action_index,
        tool_id=tool_id,
        merged_params=merged_params if merged_params is not None else {"title": "Fix sink"},
        run_handler=run_handler,
    )


@pytest.mark.asyncio
async def test_identical_execute_fires_once() -> None:
    """Two identical executes (double-submit) → the Buildium write fires ONCE;
    the second is a replay, not a second side effect."""
    store = FakeStore()
    calls = {"n": 0}
    handler = _make_handler(calls, {"buildium_id": 1, "summary": "Created task"})

    first = await _run(store, handler)
    second = await _run(store, handler)

    assert calls["n"] == 1
    assert first.outcome == "executed"
    assert first.result == {"buildium_id": 1, "summary": "Created task"}
    assert second.outcome == "replayed"


@pytest.mark.asyncio
async def test_distinct_actions_both_fire() -> None:
    """A different action on the same event (different index/params) derives a
    different key → both writes fire (no over-dedup)."""
    store = FakeStore()
    calls = {"n": 0}
    handler = _make_handler(calls, {"buildium_id": 1})

    await _run(store, handler, action_index=0, merged_params={"title": "A"})
    await _run(store, handler, action_index=1, merged_params={"title": "B"})

    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_validation_failure_is_retryable() -> None:
    """A pre-side-effect HandlerError (400) surfaces as PreSideEffectError and
    leaves the claim retryable, so a corrected re-execute fires the write."""
    store = FakeStore()

    async def bad_handler() -> dict[str, Any]:
        raise HandlerError("title is required", tool_id="create_followup_task", status_code=400)

    with pytest.raises(PreSideEffectError):
        await _run(store, bad_handler)

    calls = {"n": 0}
    good = _make_handler(calls, {"buildium_id": 2, "summary": "ok"})
    retried = await _run(store, good)

    assert calls["n"] == 1
    assert retried.outcome == "executed"


@pytest.mark.asyncio
async def test_ambiguous_failure_blocks_refire() -> None:
    """An ambiguous failure (Buildium rejected / 502 — the write MAY have fired)
    is recorded abandoned_unknown: the exact action is never auto-refired."""
    store = FakeStore()

    async def ambiguous_handler() -> dict[str, Any]:
        raise HandlerError("Buildium rejected the task", tool_id="x", status_code=502)

    with pytest.raises(HandlerError):
        await _run(store, ambiguous_handler)

    calls = {"n": 0}
    good = _make_handler(calls, {"buildium_id": 3})
    blocked = await _run(store, good)

    assert calls["n"] == 0  # never fired again
    assert blocked.outcome == "blocked_needs_review"
