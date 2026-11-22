"""Parked approvals — a deploy mid-confirm must leave RETRYABLE state.

Every merge to main is a Render deploy, and before this change a process
death during a confirm wait left the claim ``in_progress`` → reaped to
``abandoned_unknown`` → the user's retry was blocked with needs-review even
though ``execute()`` provably never started. The fix: ``guarded_action``
parks the claim as ``awaiting_approval`` while the human decides, un-parks
before execute, and the reaper/shutdown release turn parked claims into
``failed_retryable``.

DB-free: an in-memory parking-capable store for the guard flow, and the
fake-pool pattern (mirroring ``test_idempotency_reaper``) for the new SQL
methods' command-tag handling.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from helixis.agents import idempotency as idem
from helixis.agents.idempotency import guarded_action
from helixis.db.repositories.action_idempotency import (
    STATUS_ABORTED,
    STATUS_AWAITING_APPROVAL,
    STATUS_IN_PROGRESS,
    STATUS_SUCCEEDED,
    ClaimResult,
    ClaimRow,
    PgIdempotencyStore,
)

# ── fakes ────────────────────────────────────────────────────────────


class ParkingFakeStore:
    """In-memory store WITH the parking methods; records the status history
    so tests can assert ordering (parked before confirm, resumed before
    execute)."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], ClaimRow] = {}
        self._lock = asyncio.Lock()
        self.status_history: list[str] = []
        self.resume_result = True  # flip False to simulate losing the un-park race

    def _set_status(self, k: tuple[str, str], status: str) -> None:
        row = self._rows[k]
        self._rows[k] = ClaimRow(
            company_id=row.company_id,
            idem_key=row.idem_key,
            action_type=row.action_type,
            status=status,
            result=row.result,
            error=row.error,
            attempts=row.attempts,
            expires_at=row.expires_at,
        )
        self.status_history.append(status)

    async def claim(
        self, *, company_id: str, idem_key: str, action_type: str, window: timedelta | None
    ) -> ClaimResult:
        async with self._lock:
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
                expires_at=(datetime.now(UTC) + window) if window is not None else None,
            )
            self._rows[k] = row
            self.status_history.append(STATUS_IN_PROGRESS)
            return ClaimResult(won=True, row=row)

    async def finalize_success(self, *, company_id: str, idem_key: str, result: Any) -> None:
        self._set_status((company_id, idem_key), STATUS_SUCCEEDED)

    async def finalize_retryable(self, *, company_id: str, idem_key: str, error: str) -> None:
        self._set_status((company_id, idem_key), "failed_retryable")

    async def finalize_abandoned(self, *, company_id: str, idem_key: str, error: str) -> None:
        self._set_status((company_id, idem_key), "abandoned_unknown")

    async def finalize_aborted(self, *, company_id: str, idem_key: str) -> None:
        self._set_status((company_id, idem_key), STATUS_ABORTED)

    async def reclaim(self, *, company_id: str, idem_key: str) -> bool:
        self._set_status((company_id, idem_key), STATUS_IN_PROGRESS)
        return True

    async def get(self, *, company_id: str, idem_key: str) -> ClaimRow | None:
        return self._rows.get((company_id, idem_key))

    async def reap_stale(self) -> int:
        return 0

    async def mark_awaiting_approval(self, *, company_id: str, idem_key: str) -> bool:
        self._set_status((company_id, idem_key), STATUS_AWAITING_APPROVAL)
        return True

    async def resume_from_awaiting(self, *, company_id: str, idem_key: str) -> bool:
        if not self.resume_result:
            return False
        self._set_status((company_id, idem_key), STATUS_IN_PROGRESS)
        return True

    async def release_awaiting(self) -> int:
        return 0


class LegacyFakeStore(ParkingFakeStore):
    """A store WITHOUT the parking methods — older fakes / degraded modes.
    The guard must fall back to today's unparked flow unchanged."""

    mark_awaiting_approval = None  # type: ignore[assignment]
    resume_from_awaiting = None  # type: ignore[assignment]


async def _auto_approve(**_kwargs: Any) -> bool:
    return True


async def _auto_decline(**_kwargs: Any) -> bool:
    return False


def _confirm() -> dict[str, Any]:
    return {"action": "test", "title": "t", "details": "d", "items": []}


# ── guard flow ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claim_parks_during_confirm_and_resumes_before_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ParkingFakeStore()
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_approve)
    ran = []

    async def execute() -> str:
        # At execute time the claim must be back to in_progress (a death here
        # is ambiguous again) — never still parked.
        row = store._rows[("c1", "k1")]
        assert row.status == STATUS_IN_PROGRESS
        ran.append(True)
        return "done"

    res = await guarded_action(
        store=store,
        company_id="c1",
        idem_key="k1",
        action_type="test.act",
        confirm=_confirm(),
        execute=execute,
        window=None,
    )
    assert res.outcome == "executed" and ran
    # in_progress (claim) → awaiting_approval (park) → in_progress (resume)
    # → succeeded (finalize).
    assert store.status_history == [
        STATUS_IN_PROGRESS,
        STATUS_AWAITING_APPROVAL,
        STATUS_IN_PROGRESS,
        STATUS_SUCCEEDED,
    ]


@pytest.mark.asyncio
async def test_decline_from_parked_finalizes_aborted(monkeypatch: pytest.MonkeyPatch) -> None:
    store = ParkingFakeStore()
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_decline)

    async def execute() -> str:
        raise AssertionError("must not execute on decline")

    res = await guarded_action(
        store=store,
        company_id="c1",
        idem_key="k1",
        action_type="test.act",
        confirm=_confirm(),
        execute=execute,
        window=None,
    )
    assert res.outcome == "not_approved"
    assert store.status_history[-1] == STATUS_ABORTED


@pytest.mark.asyncio
async def test_lost_unpark_race_never_executes(monkeypatch: pytest.MonkeyPatch) -> None:
    """If shutdown release / the reaper flipped the parked row first, the
    claim is no longer ours — approving must NOT fire the side effect."""
    store = ParkingFakeStore()
    store.resume_result = False
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_approve)

    async def execute() -> str:
        raise AssertionError("must not execute after losing the un-park race")

    res = await guarded_action(
        store=store,
        company_id="c1",
        idem_key="k1",
        action_type="test.act",
        confirm=_confirm(),
        execute=execute,
        window=None,
    )
    assert res.outcome == "already_pending"


@pytest.mark.asyncio
async def test_store_without_parking_methods_runs_unparked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LegacyFakeStore()
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_approve)

    async def execute() -> str:
        return "done"

    res = await guarded_action(
        store=store,
        company_id="c1",
        idem_key="k1",
        action_type="test.act",
        confirm=_confirm(),
        execute=execute,
        window=None,
    )
    assert res.outcome == "executed"
    assert STATUS_AWAITING_APPROVAL not in store.status_history


@pytest.mark.asyncio
async def test_peer_sees_awaiting_approval_as_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """A claim loser observing a peer's parked claim must wait/back off —
    never read it as an unknown status and block with needs-review."""
    store = ParkingFakeStore()
    await store.claim(company_id="c1", idem_key="k1", action_type="test.act", window=None)
    await store.mark_awaiting_approval(company_id="c1", idem_key="k1")
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_approve)

    async def execute() -> str:
        raise AssertionError("loser must not execute")

    res = await guarded_action(
        store=store,
        company_id="c1",
        idem_key="k1",
        action_type="test.act",
        confirm=_confirm(),
        execute=execute,
        window=None,
        pending_wait=0.3,
        poll=0.05,
    )
    assert res.outcome == "already_pending"


# ── SQL methods (fake-pool pattern from test_idempotency_reaper) ─────


class _FakeConn:
    def __init__(self, tags: list[str]) -> None:
        self._tags = tags
        self.queries: list[str] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.queries.append(query)
        return self._tags[len(self.queries) - 1]

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _FakeConn:
        return self._conn


@pytest.mark.asyncio
async def test_reap_stale_sums_both_branches() -> None:
    conn = _FakeConn(["UPDATE 2", "UPDATE 3"])
    store = PgIdempotencyStore(_FakePool(conn))  # type: ignore[arg-type]
    assert await store.reap_stale() == 5
    assert "in_progress" in conn.queries[0]
    assert "abandoned_unknown" in conn.queries[0]
    # Parked claims are provably pre-side-effect → retryable, never abandoned.
    assert "awaiting_approval" in conn.queries[1]
    assert "failed_retryable" in conn.queries[1]


@pytest.mark.asyncio
async def test_release_awaiting_has_no_lease_condition() -> None:
    """Shutdown release fires immediately — the process is dying, so waiting
    out the lease would strand the user for up to 15 minutes."""
    conn = _FakeConn(["UPDATE 4"])
    store = PgIdempotencyStore(_FakePool(conn))  # type: ignore[arg-type]
    assert await store.release_awaiting() == 4
    assert "lease_until" not in conn.queries[0].split("WHERE")[1]
    assert "failed_retryable" in conn.queries[0]
