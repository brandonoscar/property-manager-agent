"""Tests for the agent-action idempotency guard.

DB-free: a fake in-memory store implements the ``IdempotencyStore`` Protocol so
these run in CI with no Supabase/Composio/Browserbase. The confirm gate is
driven through the real module-level channel in ``helixis.agents.tools``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from helixis.agents import idempotency as idem
from helixis.agents.idempotency import (
    PreSideEffectError,
    derive_key,
    guarded_action,
)
from helixis.db.repositories.action_idempotency import (
    STATUS_ABANDONED_UNKNOWN,
    STATUS_ABORTED,
    STATUS_FAILED_RETRYABLE,
    STATUS_IN_PROGRESS,
    STATUS_SUCCEEDED,
    ClaimResult,
    ClaimRow,
)

# ── fake store ───────────────────────────────────────────────────────────────


class FakeStore:
    """In-memory IdempotencyStore. ``_rows`` keyed by (company_id, idem_key)."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], ClaimRow] = {}
        self._lock = asyncio.Lock()

    async def claim(
        self, *, company_id: str, idem_key: str, action_type: str, window: timedelta | None
    ) -> ClaimResult:
        async with self._lock:
            k = (company_id, idem_key)
            existing = self._rows.get(k)
            if existing is not None:
                return ClaimResult(won=False, row=existing)
            expires = datetime.now(UTC) + window if window is not None else None
            row = ClaimRow(
                company_id=company_id,
                idem_key=idem_key,
                action_type=action_type,
                status=STATUS_IN_PROGRESS,
                result=None,
                error=None,
                attempts=1,
                expires_at=expires,
            )
            self._rows[k] = row
            return ClaimResult(won=True, row=row)

    async def _set(self, company_id: str, idem_key: str, **fields: Any) -> None:
        row = self._rows[(company_id, idem_key)]
        for key, val in fields.items():
            setattr(row, key, val)

    async def finalize_success(self, *, company_id: str, idem_key: str, result: Any) -> None:
        await self._set(company_id, idem_key, status=STATUS_SUCCEEDED, result=result, error=None)

    async def finalize_retryable(self, *, company_id: str, idem_key: str, error: str) -> None:
        await self._set(company_id, idem_key, status=STATUS_FAILED_RETRYABLE, error=error)

    async def finalize_abandoned(self, *, company_id: str, idem_key: str, error: str) -> None:
        await self._set(company_id, idem_key, status=STATUS_ABANDONED_UNKNOWN, error=error)

    async def finalize_aborted(self, *, company_id: str, idem_key: str) -> None:
        await self._set(company_id, idem_key, status=STATUS_ABORTED, error=None)

    async def reclaim(self, *, company_id: str, idem_key: str) -> bool:
        async with self._lock:
            row = self._rows.get((company_id, idem_key))
            if row is None or row.status not in (STATUS_FAILED_RETRYABLE, STATUS_ABORTED):
                return False
            row.status = STATUS_IN_PROGRESS
            row.attempts += 1
            row.error = None
            return True

    async def get(self, *, company_id: str, idem_key: str) -> ClaimRow | None:
        return self._rows.get((company_id, idem_key))

    async def reap_stale(self) -> int:
        # Tests drive abandonment by setting status directly; nothing to scan.
        return 0


_CONFIRM = {"action": "x", "title": "t", "details": "d", "items": []}


async def _approve_next() -> None:
    """Drain the confirm channel and approve the pending payload."""
    from helixis.agents import tools as agent_tools

    for _ in range(200):
        await asyncio.sleep(0)
        drained = await agent_tools.drain_pending_confirmations()
        if drained:
            await agent_tools.resolve_confirmation(drained[0]["confirm_id"], True)
            return
    raise AssertionError("no confirm payload emitted")


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


# ── 1. double-approval fires execute exactly once ─────────────────────────────


@pytest.mark.asyncio
async def test_double_approval_fires_once(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_approve)
    calls = {"n": 0}

    async def execute() -> str:
        calls["n"] += 1
        return "filed-123"

    key = derive_key("co-1", "act", {"a": 1})
    common = dict(
        store=store,
        company_id="co-1",
        idem_key=key,
        action_type="act",
        confirm=_CONFIRM,
        window=timedelta(hours=6),
    )
    first = await guarded_action(execute=execute, **common)
    second = await guarded_action(execute=execute, **common)

    assert first.outcome == "executed"
    assert second.outcome == "replayed"
    assert second.result == "filed-123"
    assert calls["n"] == 1


# ── 2. pre-side-effect failure → failed_retryable → retry runs ────────────────


@pytest.mark.asyncio
async def test_pre_side_effect_failure_is_retryable(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_approve)
    calls = {"n": 0}

    async def execute() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise PreSideEffectError("bad args")
        return "ok-2"

    key = derive_key("co-1", "act", {"a": 1})
    common = dict(
        store=store,
        company_id="co-1",
        idem_key=key,
        action_type="act",
        confirm=_CONFIRM,
        window=timedelta(hours=6),
    )
    with pytest.raises(PreSideEffectError):
        await guarded_action(execute=execute, **common)
    row = await store.get(company_id="co-1", idem_key=key)
    assert row is not None and row.status == STATUS_FAILED_RETRYABLE

    second = await guarded_action(execute=execute, **common)
    assert second.outcome == "executed"
    assert second.result == "ok-2"
    assert calls["n"] == 2


# ── 3. two genuinely-different actions both run ───────────────────────────────


@pytest.mark.asyncio
async def test_distinct_actions_both_run(store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_approve)
    calls = {"n": 0}

    async def execute() -> str:
        calls["n"] += 1
        return f"r{calls['n']}"

    k1 = derive_key("co-1", "act", {"a": 1})
    k2 = derive_key("co-1", "act", {"a": 2})
    r1 = await guarded_action(
        store=store,
        company_id="co-1",
        idem_key=k1,
        action_type="act",
        confirm=_CONFIRM,
        execute=execute,
        window=timedelta(hours=6),
    )
    r2 = await guarded_action(
        store=store,
        company_id="co-1",
        idem_key=k2,
        action_type="act",
        confirm=_CONFIRM,
        execute=execute,
        window=timedelta(hours=6),
    )
    assert r1.outcome == "executed" and r2.outcome == "executed"
    assert calls["n"] == 2


# ── 4. concurrent identical calls → exactly one execute ───────────────────────


@pytest.mark.asyncio
async def test_concurrent_calls_one_execution(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_approve)
    calls = {"n": 0}

    async def execute() -> str:
        calls["n"] += 1
        await asyncio.sleep(0.01)  # widen the race window
        return "once"

    key = derive_key("co-1", "act", {"a": 1})
    common = dict(
        store=store,
        company_id="co-1",
        idem_key=key,
        action_type="act",
        confirm=_CONFIRM,
        execute=execute,
        window=timedelta(hours=6),
        pending_wait=2.0,
        poll=0.01,
    )
    a, b = await asyncio.gather(guarded_action(**common), guarded_action(**common))
    outcomes = sorted([a.outcome, b.outcome])
    assert calls["n"] == 1
    assert outcomes == ["executed", "replayed"]


# ── 5. stale in_progress → abandoned_unknown → blocked, no execute ────────────


@pytest.mark.asyncio
async def test_abandoned_blocks_and_does_not_fire(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_approve)
    key = derive_key("co-1", "act", {"a": 1})
    # Seed an abandoned_unknown row (as the reaper would leave a stale claim).
    await store.claim(company_id="co-1", idem_key=key, action_type="act", window=timedelta(hours=6))
    await store.finalize_abandoned(company_id="co-1", idem_key=key, error="reaped")

    fired = {"n": 0}

    async def execute() -> str:
        fired["n"] += 1
        return "should-not-run"

    res = await guarded_action(
        store=store,
        company_id="co-1",
        idem_key=key,
        action_type="act",
        confirm=_CONFIRM,
        execute=execute,
        window=timedelta(hours=6),
    )
    assert res.outcome == "blocked_needs_review"
    assert fired["n"] == 0


# ── 6. derive_key: order-independent + full-value (no truncation collision) ───


def test_derive_key_order_independent() -> None:
    a = derive_key("co-1", "act", {"x": 1, "y": 2})
    b = derive_key("co-1", "act", {"y": 2, "x": 1})
    assert a == b


def test_derive_key_full_value_no_truncation_collision() -> None:
    base = "z" * 5000
    a = derive_key("co-1", "act", {"body": base + "A"})
    b = derive_key("co-1", "act", {"body": base + "B"})
    assert a != b  # full value hashed → tail difference changes the key


def test_derive_key_tenant_and_extra_scoped() -> None:
    assert derive_key("co-1", "act", {"a": 1}) != derive_key("co-2", "act", {"a": 1})
    assert derive_key("co-1", "act", {"a": 1}, extra="sess-1") != derive_key(
        "co-1", "act", {"a": 1}, extra="sess-2"
    )


# ── 7. aborted (declined) first attempt → later attempt can re-confirm ────────


@pytest.mark.asyncio
async def test_aborted_then_reconfirm(store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> None:
    decision = {"approve": False}

    async def gate(**_kwargs: object) -> bool:
        return decision["approve"]

    monkeypatch.setattr(idem, "confirm_or_abort", gate)
    fired = {"n": 0}

    async def execute() -> str:
        fired["n"] += 1
        return "filed"

    key = derive_key("co-1", "act", {"a": 1})
    common = dict(
        store=store,
        company_id="co-1",
        idem_key=key,
        action_type="act",
        confirm=_CONFIRM,
        execute=execute,
        window=timedelta(hours=6),
    )
    first = await guarded_action(**common)
    assert first.outcome == "not_approved"
    assert fired["n"] == 0
    row = await store.get(company_id="co-1", idem_key=key)
    assert row is not None and row.status == STATUS_ABORTED

    decision["approve"] = True
    second = await guarded_action(**common)
    assert second.outcome == "executed"
    assert fired["n"] == 1


# ── 9. ambiguous failure → abandoned_unknown, not auto-retried ────────────────


@pytest.mark.asyncio
async def test_ambiguous_failure_is_abandoned(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_approve)

    async def execute() -> str:
        raise TimeoutError("connection died after send")

    key = derive_key("co-1", "act", {"a": 1})
    common = dict(
        store=store,
        company_id="co-1",
        idem_key=key,
        action_type="act",
        confirm=_CONFIRM,
        window=timedelta(hours=6),
    )
    with pytest.raises(TimeoutError):
        await guarded_action(execute=execute, **common)
    row = await store.get(company_id="co-1", idem_key=key)
    assert row is not None and row.status == STATUS_ABANDONED_UNKNOWN

    # A later attempt must NOT re-fire — it's blocked for review.
    fired = {"n": 0}

    async def execute2() -> str:
        fired["n"] += 1
        return "x"

    res = await guarded_action(execute=execute2, **common)
    assert res.outcome == "blocked_needs_review"
    assert fired["n"] == 0


# ── 10. two identical writes in-window collapse to one + replay ───────────────


@pytest.mark.asyncio
async def test_identical_writes_collapse_with_replay(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_approve)
    sends = {"n": 0}
    recorded: list[str] = []

    async def execute() -> dict[str, str]:
        sends["n"] += 1
        return {"id": "msg-1"}

    async def record(res: idem.GuardResult) -> None:
        recorded.append(res.outcome)

    # Same args → same key (simulates an identical Composio send within window).
    args = {"to": "a@b.com", "subject": "hi"}
    key = derive_key("co-1", "GMAIL_SEND", args)
    common = dict(
        store=store,
        company_id="co-1",
        idem_key=key,
        action_type="GMAIL_SEND",
        confirm=_CONFIRM,
        execute=execute,
        window=timedelta(hours=6),
        record=record,
    )
    r1 = await guarded_action(**common)
    r2 = await guarded_action(**common)
    assert sends["n"] == 1
    assert r1.outcome == "executed"
    assert r2.outcome == "replayed"
    assert r2.result == {"id": "msg-1"}
    # record() fires on BOTH the fresh success and the replay (audit preserved).
    assert recorded == ["executed", "replayed"]


# ── confirm gate end-to-end (real channel), fail-closed on decline ────────────


@pytest.mark.asyncio
async def test_real_confirm_channel_fires_after_approval(store: FakeStore) -> None:
    from helixis.agents import tools as agent_tools

    await agent_tools.drain_pending_confirmations()
    fired = {"n": 0}

    async def execute() -> str:
        fired["n"] += 1
        return "ok"

    key = derive_key("co-1", "act", {"a": 1})
    task = asyncio.create_task(
        guarded_action(
            store=store,
            company_id="co-1",
            idem_key=key,
            action_type="act",
            confirm=_CONFIRM,
            execute=execute,
            window=timedelta(hours=6),
        )
    )
    await asyncio.sleep(0)
    assert fired["n"] == 0  # blocked at the gate until approval
    await _approve_next()
    res = await asyncio.wait_for(task, timeout=1.0)
    assert res.outcome == "executed"
    assert fired["n"] == 1


# ── verify (read-back) hook ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verify_none_behaves_as_today(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No verifier supplied → execute → finalize_success, exactly as before."""
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_approve)
    calls = {"n": 0}

    async def execute() -> str:
        calls["n"] += 1
        return "r"

    key = derive_key("co-1", "act", {"a": 1})
    res = await guarded_action(
        store=store,
        company_id="co-1",
        idem_key=key,
        action_type="act",
        confirm=_CONFIRM,
        execute=execute,
        window=timedelta(hours=6),
    )
    assert res.outcome == "executed"
    assert calls["n"] == 1
    row = await store.get(company_id="co-1", idem_key=key)
    assert row is not None and row.status == STATUS_SUCCEEDED


@pytest.mark.asyncio
async def test_verify_true_records_success(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_approve)
    calls = {"n": 0}

    async def execute() -> str:
        calls["n"] += 1
        return "filed"

    async def verify(result: str) -> bool:
        return True

    key = derive_key("co-1", "act", {"a": 1})
    res = await guarded_action(
        store=store,
        company_id="co-1",
        idem_key=key,
        action_type="act",
        confirm=_CONFIRM,
        execute=execute,
        window=timedelta(hours=6),
        verify=verify,
    )
    assert res.outcome == "executed"
    assert res.result == "filed"
    assert calls["n"] == 1
    row = await store.get(company_id="co-1", idem_key=key)
    assert row is not None and row.status == STATUS_SUCCEEDED


@pytest.mark.asyncio
async def test_verify_false_is_needs_review_no_reexecute(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_approve)
    calls = {"n": 0}

    async def execute() -> str:
        calls["n"] += 1
        return "filed"

    async def verify(result: str) -> bool:
        return False

    key = derive_key("co-1", "act", {"a": 1})
    res = await guarded_action(
        store=store,
        company_id="co-1",
        idem_key=key,
        action_type="act",
        confirm=_CONFIRM,
        execute=execute,
        window=timedelta(hours=6),
        verify=verify,
    )
    assert res.outcome == "blocked_needs_review"
    assert calls["n"] == 1  # executed once, NOT re-executed
    row = await store.get(company_id="co-1", idem_key=key)
    assert row is not None
    assert row.status == STATUS_ABANDONED_UNKNOWN
    assert row.error == "verify_failed:not_found"


@pytest.mark.asyncio
async def test_verify_raises_is_needs_review_with_error_tag(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_approve)
    calls = {"n": 0}

    async def execute() -> str:
        calls["n"] += 1
        return "filed"

    async def verify(result: str) -> bool:
        raise TimeoutError("read-back timed out")

    key = derive_key("co-1", "act", {"a": 1})
    res = await guarded_action(
        store=store,
        company_id="co-1",
        idem_key=key,
        action_type="act",
        confirm=_CONFIRM,
        execute=execute,
        window=timedelta(hours=6),
        verify=verify,
    )
    assert res.outcome == "blocked_needs_review"
    assert calls["n"] == 1  # not re-executed
    row = await store.get(company_id="co-1", idem_key=key)
    assert row is not None
    assert row.status == STATUS_ABANDONED_UNKNOWN
    assert row.error == "verify_error:TimeoutError"


@pytest.mark.asyncio
async def test_verify_runs_on_passthrough_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """No store (passthrough) → verify still runs; no store calls made."""
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_approve)
    monkeypatch.setattr(idem, "store_from_pool", lambda: None)
    monkeypatch.setattr(idem, "_idempotency_required", lambda: False)

    calls = {"n": 0}

    async def execute() -> str:
        calls["n"] += 1
        return "filed"

    # True → executed result returned.
    res_ok = await idem.run_guarded(
        company_id="co-1",
        idem_key="k",
        action_type="act",
        confirm=_CONFIRM,
        execute=execute,
        window=timedelta(hours=6),
        verify=lambda _r: _async_true(),
    )
    assert res_ok.outcome == "executed"
    assert res_ok.result == "filed"

    # False → needs-review, still no store.
    async def verify_false(_r: str) -> bool:
        return False

    res_block = await idem.run_guarded(
        company_id="co-1",
        idem_key="k2",
        action_type="act",
        confirm=_CONFIRM,
        execute=execute,
        window=timedelta(hours=6),
        verify=verify_false,
    )
    assert res_block.outcome == "blocked_needs_review"

    # Raises → needs-review.
    async def verify_raise(_r: str) -> bool:
        raise RuntimeError("boom")

    res_err = await idem.run_guarded(
        company_id="co-1",
        idem_key="k3",
        action_type="act",
        confirm=_CONFIRM,
        execute=execute,
        window=timedelta(hours=6),
        verify=verify_raise,
    )
    assert res_err.outcome == "blocked_needs_review"
    assert calls["n"] == 3  # one execute per call, never re-executed


@pytest.mark.asyncio
async def test_verify_fail_tag_default_and_override(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verify-False tag defaults to verify_failed:not_found (Buildium's,
    unchanged) and can be overridden (e.g. Composio's provider_unsuccessful)."""
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_approve)

    async def execute() -> str:
        return "x"

    async def verify_false(_r: str) -> bool:
        return False

    # Default tag preserved.
    k1 = derive_key("co-1", "act", {"a": 1})
    await guarded_action(
        store=store,
        company_id="co-1",
        idem_key=k1,
        action_type="act",
        confirm=_CONFIRM,
        execute=execute,
        window=timedelta(hours=6),
        verify=verify_false,
    )
    row1 = await store.get(company_id="co-1", idem_key=k1)
    assert row1 is not None and row1.error == "verify_failed:not_found"

    # Override honored.
    k2 = derive_key("co-1", "act", {"a": 2})
    await guarded_action(
        store=store,
        company_id="co-1",
        idem_key=k2,
        action_type="act",
        confirm=_CONFIRM,
        execute=execute,
        window=timedelta(hours=6),
        verify=verify_false,
        verify_fail_tag="verify_failed:provider_unsuccessful",
    )
    row2 = await store.get(company_id="co-1", idem_key=k2)
    assert row2 is not None and row2.error == "verify_failed:provider_unsuccessful"


async def _async_true() -> bool:
    return True


async def _auto_approve(**_kwargs: object) -> bool:
    return True


# ── run_guarded no-store fallback: required vs. passthrough ───────────────────


@pytest.mark.asyncio
async def test_required_unavailable_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """required=True + no store → execute NOT called; DISTINCT
    blocked_store_unavailable outcome (issue #227) so callers can say nothing
    happened, not "did it but can't confirm"."""
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_approve)
    monkeypatch.setattr(idem, "store_from_pool", lambda: None)
    monkeypatch.setattr(idem, "_idempotency_required", lambda: True)
    fired = {"n": 0}
    signalled: list[bool] = []
    monkeypatch.setattr(
        idem,
        "_signal_passthrough",
        lambda action_type, company_id, *, fail_closed: signalled.append(fail_closed),
    )

    async def execute() -> str:
        fired["n"] += 1
        return "should-not-run"

    res = await idem.run_guarded(
        company_id="co-1",
        idem_key="k",
        action_type="GMAIL_SEND",
        confirm=_CONFIRM,
        execute=execute,
        window=timedelta(hours=6),
    )
    assert res.outcome == "blocked_store_unavailable"
    assert fired["n"] == 0
    assert signalled == [True]  # signal emitted, marked fail_closed


@pytest.mark.asyncio
async def test_not_required_unavailable_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """required=False + no store → confirm+execute once + signal emitted."""
    confirms = {"n": 0}

    async def gate(**_kwargs: object) -> bool:
        confirms["n"] += 1
        return True

    monkeypatch.setattr(idem, "confirm_or_abort", gate)
    monkeypatch.setattr(idem, "store_from_pool", lambda: None)
    monkeypatch.setattr(idem, "_idempotency_required", lambda: False)
    fired = {"n": 0}
    signalled: list[bool] = []
    monkeypatch.setattr(
        idem,
        "_signal_passthrough",
        lambda action_type, company_id, *, fail_closed: signalled.append(fail_closed),
    )

    async def execute() -> str:
        fired["n"] += 1
        return "sent"

    res = await idem.run_guarded(
        company_id="co-1",
        idem_key="k",
        action_type="GMAIL_SEND",
        confirm=_CONFIRM,
        execute=execute,
        window=timedelta(hours=6),
    )
    assert res.outcome == "executed"
    assert confirms["n"] == 1  # gate still enforced
    assert fired["n"] == 1
    assert signalled == [False]  # passthrough signal, not fail_closed


# ── derive_key determinism on non-JSON-native values ─────────────────────────


def test_derive_key_stable_for_dates_and_none() -> None:
    """Non-JSON values (datetime/None) serialize deterministically, so the same
    logical action yields the same key across calls."""
    when = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)
    a = derive_key("co-1", "act", {"due": when, "note": None})
    b = derive_key("co-1", "act", {"due": when, "note": None})
    assert a == b
    # A different date changes the key (no collapsing).
    c = derive_key("co-1", "act", {"due": datetime(2026, 6, 1, tzinfo=UTC), "note": None})
    assert a != c
