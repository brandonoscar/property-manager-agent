"""Per-company daily LLM spend ledger + budget gate (migration 0050).

Pins: token attribution rides the trace context (no context → no row),
accounting never breaks an LLM call, the gate fails OPEN on ledger
errors, and budget <= 0 disables enforcement entirely.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.llm import spend
from helixis.observability.context import trace_context

COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"


def _pool(conn: MagicMock) -> MagicMock:
    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


def _response(total: int | None) -> SimpleNamespace:
    return SimpleNamespace(usage=SimpleNamespace(total_tokens=total))


@pytest.mark.asyncio
async def test_tracker_attributes_tokens_to_context_company() -> None:
    conn = MagicMock()
    conn.execute = AsyncMock()
    with (
        patch("helixis.db.engine.get_pool", return_value=_pool(conn)),
        trace_context(company_id=COMPANY_ID, session_id="s1"),
    ):
        await spend.SpendTracker().async_log_success_event({}, _response(1234), None, None)
    sql, cid, _day, tokens, *components = conn.execute.await_args.args
    assert "INSERT INTO helixis_llm_usage" in sql and "ON CONFLICT (company_id, day)" in sql
    assert (cid, tokens) == (COMPANY_ID, 1234)
    # 0075 widened the row to the billable components; a response carrying only
    # a total still records cleanly, with the cache columns at zero.
    assert components == [0, 0, 0, 0, 0]


@pytest.mark.asyncio
async def test_tracker_skips_without_context_and_survives_db_failure() -> None:
    conn = MagicMock()
    conn.execute = AsyncMock()
    with patch("helixis.db.engine.get_pool", return_value=_pool(conn)):
        # No trace context → nothing to attribute, no DB touch.
        await spend.SpendTracker().async_log_success_event({}, _response(500), None, None)
    conn.execute.assert_not_awaited()

    # A ledger failure never propagates into the LLM call.
    with (
        patch("helixis.db.engine.get_pool", side_effect=RuntimeError("pool down")),
        trace_context(company_id=COMPANY_ID, session_id="s1"),
    ):
        await spend.SpendTracker().async_log_success_event({}, _response(500), None, None)


@pytest.mark.asyncio
async def test_budget_gate_math_and_fail_open() -> None:
    with patch.object(spend, "tokens_used_today", AsyncMock(return_value=4_999_999)):
        assert await spend.budget_exceeded(COMPANY_ID, 5_000_000) is False
    with patch.object(spend, "tokens_used_today", AsyncMock(return_value=5_000_000)):
        assert await spend.budget_exceeded(COMPANY_ID, 5_000_000) is True
    # budget <= 0 disables without touching the ledger.
    ledger = AsyncMock()
    with patch.object(spend, "tokens_used_today", ledger):
        assert await spend.budget_exceeded(COMPANY_ID, 0) is False
    ledger.assert_not_awaited()
    # Ledger error → admit the run (availability over accounting).
    with patch.object(spend, "tokens_used_today", AsyncMock(side_effect=RuntimeError("db"))):
        assert await spend.budget_exceeded(COMPANY_ID, 5_000_000) is False


def test_zero_or_garbage_usage_counts_as_zero() -> None:
    assert spend._usage_breakdown(_response(None)).total == 0
    assert spend._usage_breakdown(SimpleNamespace()).total == 0
    assert spend._usage_breakdown({"usage": {"total_tokens": "nope"}}).total == 0
    assert spend._usage_breakdown({"usage": {"total_tokens": 42}}).total == 42
