"""initial-sync: the onboarding backfill wrapped in sync_state tracking.

``backfill_company`` itself is tested elsewhere; here we pin the status
state-machine (pending→syncing→done | failed) and the repo's row mapping.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.buildium import backfill
from helixis.buildium.backfill import BackfillResult, run_initial_sync
from helixis.db.repositories import sync_state

# ── orchestrator state machine ──────────────────────────────────────


@pytest.mark.asyncio
async def test_run_initial_sync_marks_done_with_counts() -> None:
    result = BackfillResult(properties=3, units=5, leases=2, work_orders=1)
    with (
        patch.object(backfill, "backfill_company", AsyncMock(return_value=result)),
        patch.object(sync_state, "mark_syncing", AsyncMock()) as ms,
        patch.object(sync_state, "mark_done", AsyncMock()) as md,
        patch.object(sync_state, "mark_failed", AsyncMock()) as mf,
    ):
        await run_initial_sync(MagicMock(), MagicMock(), company_id="c1", account_id=42)

    ms.assert_awaited_once()
    md.assert_awaited_once()
    # counts arg carries the per-stage totals.
    counts = md.await_args.args[2]
    assert counts["properties"] == 3
    assert counts["leases"] == 2
    mf.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_initial_sync_marks_failed_on_crash() -> None:
    with (
        patch.object(backfill, "backfill_company", AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(sync_state, "mark_syncing", AsyncMock()),
        patch.object(sync_state, "mark_done", AsyncMock()) as md,
        patch.object(sync_state, "mark_failed", AsyncMock()) as mf,
    ):
        await run_initial_sync(MagicMock(), MagicMock(), company_id="c1", account_id=42)

    mf.assert_awaited_once()
    md.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_initial_sync_finishes_done_despite_stage_errors() -> None:
    # Non-fatal stage errors still land partial data → banner must clear (done),
    # not hang. backfill_company already tolerated them.
    result = BackfillResult(properties=1, errors=["leases: timeout"])
    with (
        patch.object(backfill, "backfill_company", AsyncMock(return_value=result)),
        patch.object(sync_state, "mark_syncing", AsyncMock()),
        patch.object(sync_state, "mark_done", AsyncMock()) as md,
        patch.object(sync_state, "mark_failed", AsyncMock()) as mf,
    ):
        await run_initial_sync(MagicMock(), MagicMock(), company_id="c1", account_id=42)

    md.assert_awaited_once()
    mf.assert_not_awaited()


# ── repository row mapping ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_sync_state_maps_row() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(
        return_value={
            "company_id": "c1",
            "status": "syncing",
            "started_at": None,
            "finished_at": None,
            "counts": {"properties": 4},
            "last_error": None,
        }
    )
    state = await sync_state.get_sync_state(pool, "c1")
    assert state is not None
    assert state.status == "syncing"
    assert state.counts == {"properties": 4}
    assert state.to_public()["status"] == "syncing"


@pytest.mark.asyncio
async def test_get_sync_state_none_when_absent() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    assert await sync_state.get_sync_state(pool, "c1") is None
