"""PR #92 — summary claim queue + worker loop tests.

Mirrors the PR #89 ``test_pr89_planner_worker.py`` structure for
the summarizer half of the pipeline. Covers:

  - claim_summary_batch SQL shape (cooldown + max_attempts gates,
    FOR UPDATE SKIP LOCKED, claims both 'pending' AND 'retrying',
    bumps attempts/timestamp but does NOT change status).
  - claim_summary_batch SQL-injection allow-list.
  - _process_summary_row happy path: summary persisted via
    mark_summary_succeeded with text + prompt_version.
  - _process_summary_row transient error (litellm.Timeout) under
    cap → mark_summary_retrying.
  - _process_summary_row transient error AT cap → mark_summary_failed.
  - _process_summary_row sanitizer rejection → mark_summary_failed
    immediately (non-retryable, even with attempts left).
  - mark_summary_* SQL shape pinning + error truncation.
  - reset_summary_for_resummarize SQL shape.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import litellm
import pytest

from helixis.agent.summarizer import (
    SUMMARY_PROMPT_VERSION,
    SummarizerError,
    SummaryResult,
)
from helixis.webhooks.queue import SummaryClaimedRow


def _summary_row(
    *,
    summary_attempts: int = 0,
    enrichment: dict[str, Any] | None = None,
) -> SummaryClaimedRow:
    return SummaryClaimedRow(
        id=uuid.uuid4(),
        event_name="Task.Created",
        company_id="company-pr92",
        event_time=datetime(2026, 5, 18, 18, 0, 0, tzinfo=UTC),
        payload={"EventName": "Task.Created", "TaskId": 17435},
        enrichment=enrichment or {"task": {"title": "Loud crashing"}},
        summary_attempts=summary_attempts,
    )


# ── claim_summary_batch ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claim_summary_batch_executes_expected_sql() -> None:
    """Must filter by status IN ('pending','retrying'), respect
    cooldown + attempts cap, use FOR UPDATE SKIP LOCKED, bump
    attempts + last_attempted_at but NOT change status."""
    from helixis.webhooks.queue import claim_summary_batch

    captured: dict[str, Any] = {}

    async def _fake_fetch(sql: str, *args: Any) -> list[Any]:
        captured["sql"] = sql
        captured["args"] = args
        return []

    pool = MagicMock()
    pool.fetch = _fake_fetch

    await claim_summary_batch(pool, batch_size=4, max_attempts=5, cooldown_seconds=30)

    sql = captured["sql"]
    assert "summary_status IN ('pending', 'retrying')" in sql
    assert "summary_attempts < $2" in sql
    # ⚠ RE-POINTED 2026-09-10, not weakened. The claim is now a two-CTE
    # round-robin (webhooks/fair_queue), so the lock names its table:
    # Postgres refuses a bare FOR UPDATE over a join to a CTE. The
    # property asserted — concurrent workers skip rather than block — is
    # unchanged, and this literal is what the live server accepted.
    assert "FOR UPDATE OF b SKIP LOCKED" in sql
    assert "summary_attempts          = summary_attempts + 1" in sql
    assert "summary_last_attempted_at = now()" in sql
    # Status flip is INTENTIONALLY absent from the claim — see the
    # claim_summary_batch docstring for the rationale.
    assert "SET summary_status =" not in sql
    assert captured["args"] == (4, 5, 30)


@pytest.mark.asyncio
async def test_claim_summary_batch_waits_for_enrichment_or_grace_window() -> None:
    """The summarizer must not narrate a row whose enrichment is in flight.

    PR #128: the receiver inserts the row BEFORE running Buildium API calls
    (~1-3s), so the poll cycle was claiming rows between the INSERT and the
    enrichment UPDATE and the summarizer ran with NULL enrichment, producing
    ``A new "Todo" task was created on May 22, 2026. No further details ...``

    ⚠ Widened 2026-08-25. PR #128's wall-clock grace held only for the path it
    was written for. On a row the receiver never enriched, the PLANNER writes
    the enrichment -- and planner and summarizer are claimed in the SAME poll
    cycle, 190ms apart, while the planner's Buildium calls take seconds.
    Measured: planner claimed 02:13:09.495, summary 02:13:09.685, row 39s old.
    At 39s the 15s arm waved it straight through, so the summarizer won the
    race EVERY time and wrote "the enrichment didn't return a property, unit,
    reporter, or description" onto cards whose enrichment landed seconds later
    with all four -- above suggested actions planned from that same enrichment
    and citing it. The card contradicted itself.

    A wall clock cannot express "has the producer had its turn". So the guard
    now asks the producer directly, and the clock survives only as a widened
    anti-stuck valve that cannot pre-empt a merely-slow planner.
    """
    from helixis.webhooks.queue import claim_summary_batch

    captured: dict[str, Any] = {}

    async def _fake_fetch(sql: str, *args: Any) -> list[Any]:
        captured["sql"] = sql
        return []

    pool = MagicMock()
    pool.fetch = _fake_fetch

    # Without prompt-version arg.
    await claim_summary_batch(pool, batch_size=4, max_attempts=5, cooldown_seconds=30)
    sql = captured["sql"]
    assert "enrichment IS NOT NULL" in sql
    # Hold while the producer is still working -- the clause the race needed.
    assert "planner_status NOT IN ('pending', 'in_progress')" in sql
    # The valve, widened so it cannot fire before the planner finishes.
    assert "received_at < now() - interval '5 minutes'" in sql
    assert "interval '15 seconds'" not in sql

    # WITH prompt-version arg (the self-heal path).
    captured.clear()
    await claim_summary_batch(
        pool,
        batch_size=4,
        max_attempts=5,
        cooldown_seconds=30,
        current_prompt_version="v6",
    )
    sql = captured["sql"]
    assert "enrichment IS NOT NULL" in sql
    # Hold while the producer is still working -- the clause the race needed.
    assert "planner_status NOT IN ('pending', 'in_progress')" in sql
    # The valve, widened so it cannot fire before the planner finishes.
    assert "received_at < now() - interval '5 minutes'" in sql
    assert "interval '15 seconds'" not in sql


@pytest.mark.asyncio
async def test_claim_summary_batch_rejects_unknown_table() -> None:
    from helixis.webhooks.queue import claim_summary_batch

    with pytest.raises(ValueError, match="unsupported planner queue table"):
        await claim_summary_batch(MagicMock(), table_name="foo; DROP TABLE bar--")


# ── _process_summary_row happy path ────────────────────────────────


@pytest.mark.asyncio
async def test_process_summary_row_success_persists_text_and_version() -> None:
    """Summarizer returns a SummaryResult → mark_summary_succeeded
    fires with the right kwargs; failed / retrying not called."""
    from helixis.webhooks.worker import _process_summary_row

    row = _summary_row()
    result = SummaryResult(
        text="Sarah Chen at 123 Main St unit 4B reported a leak.",
        prompt_version=SUMMARY_PROMPT_VERSION,
        model_name="gemini/gemini-2.5-flash",
        duration_ms=4200,
    )

    with (
        patch(
            "helixis.webhooks.worker.Summarizer",
            return_value=MagicMock(summarize=AsyncMock(return_value=result)),
        ),
        patch("helixis.webhooks.worker.get_pool", return_value=MagicMock()),
        patch("helixis.webhooks.worker.mark_summary_succeeded", new=AsyncMock()) as ok_mock,
        patch("helixis.webhooks.worker.mark_summary_retrying", new=AsyncMock()) as retry_mock,
        patch("helixis.webhooks.worker.mark_summary_failed", new=AsyncMock()) as failed_mock,
    ):
        await _process_summary_row(row)

    ok_mock.assert_awaited_once()
    kwargs = ok_mock.await_args.kwargs
    assert kwargs["text"] == result.text
    assert kwargs["prompt_version"] == SUMMARY_PROMPT_VERSION
    retry_mock.assert_not_awaited()
    failed_mock.assert_not_awaited()


# ── transient error paths ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_summary_row_transient_under_cap_marks_retrying() -> None:
    """SummarizerError caused by litellm.Timeout, attempts < cap →
    flip to 'retrying' so the claim loop picks it back up after the
    cooldown window."""
    from helixis.webhooks.worker import _process_summary_row

    row = _summary_row(summary_attempts=1)  # under cap (5)
    transient = litellm.Timeout("slow", model="gemini/x", llm_provider="gemini")
    err = SummarizerError("summarizer exhausted transient retries")
    err.__cause__ = transient

    fake_summarizer = MagicMock(summarize=AsyncMock(side_effect=err))

    with (
        patch("helixis.webhooks.worker.Summarizer", return_value=fake_summarizer),
        patch("helixis.webhooks.worker.get_pool", return_value=MagicMock()),
        patch("helixis.webhooks.worker.mark_summary_succeeded", new=AsyncMock()) as ok_mock,
        patch("helixis.webhooks.worker.mark_summary_retrying", new=AsyncMock()) as retry_mock,
        patch("helixis.webhooks.worker.mark_summary_failed", new=AsyncMock()) as failed_mock,
    ):
        await _process_summary_row(row)

    ok_mock.assert_not_awaited()
    retry_mock.assert_awaited_once()
    failed_mock.assert_not_awaited()
    # The worker stores ``repr(SummarizerError)``, not the underlying
    # litellm.Timeout cause — the cause is what's used internally to
    # classify the error as transient (see worker.py:_TRANSIENT_FOR_SUMMARY)
    # but the persisted error message is the SummarizerError repr.
    assert "transient retries" in retry_mock.await_args.kwargs["error"]


@pytest.mark.asyncio
async def test_process_summary_row_transient_at_cap_marks_failed() -> None:
    """At ``summary_attempts >= _SUMMARY_MAX_ATTEMPTS``, a transient
    error flips to 'failed' so the inbox shows the Resummarize
    affordance instead of spinning forever."""
    from helixis.webhooks.worker import _SUMMARY_MAX_ATTEMPTS, _process_summary_row

    row = _summary_row(summary_attempts=_SUMMARY_MAX_ATTEMPTS)
    transient = litellm.Timeout("slow", model="gemini/x", llm_provider="gemini")
    err = SummarizerError("exhausted")
    err.__cause__ = transient

    with (
        patch(
            "helixis.webhooks.worker.Summarizer",
            return_value=MagicMock(summarize=AsyncMock(side_effect=err)),
        ),
        patch("helixis.webhooks.worker.get_pool", return_value=MagicMock()),
        patch("helixis.webhooks.worker.mark_summary_succeeded", new=AsyncMock()),
        patch("helixis.webhooks.worker.mark_summary_retrying", new=AsyncMock()) as retry_mock,
        patch("helixis.webhooks.worker.mark_summary_failed", new=AsyncMock()) as failed_mock,
    ):
        await _process_summary_row(row)

    retry_mock.assert_not_awaited()
    failed_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_summary_row_sanitizer_rejection_is_immediate_failure() -> None:
    """Sanitizer rejection isn't transient — flip straight to
    'failed' regardless of attempts remaining. The model already
    saw the system prompt's no-ID rule; retrying just wastes
    Gemini budget."""
    from helixis.webhooks.worker import _process_summary_row

    row = _summary_row(summary_attempts=0)  # plenty of attempts left
    # SummarizerError WITHOUT a transient __cause__ — that's how the
    # summarizer client signals "non-retryable" to the worker.
    err = SummarizerError("summarizer output rejected by sanitizer: 'Task #17435'")

    with (
        patch(
            "helixis.webhooks.worker.Summarizer",
            return_value=MagicMock(summarize=AsyncMock(side_effect=err)),
        ),
        patch("helixis.webhooks.worker.get_pool", return_value=MagicMock()),
        patch("helixis.webhooks.worker.mark_summary_retrying", new=AsyncMock()) as retry_mock,
        patch("helixis.webhooks.worker.mark_summary_failed", new=AsyncMock()) as failed_mock,
    ):
        await _process_summary_row(row)

    retry_mock.assert_not_awaited()
    failed_mock.assert_awaited_once()
    assert "sanitizer" in failed_mock.await_args.kwargs["error"]


@pytest.mark.asyncio
async def test_process_summary_row_unexpected_exception_marks_failed() -> None:
    """Anything we don't classify (ZeroDivisionError etc.) is caught
    and recorded as failed so the gather() doesn't kill the batch."""
    from helixis.webhooks.worker import _process_summary_row

    row = _summary_row()

    with (
        patch(
            "helixis.webhooks.worker.Summarizer",
            return_value=MagicMock(summarize=AsyncMock(side_effect=ZeroDivisionError("oh no"))),
        ),
        patch("helixis.webhooks.worker.get_pool", return_value=MagicMock()),
        patch("helixis.webhooks.worker.mark_summary_succeeded", new=AsyncMock()),
        patch("helixis.webhooks.worker.mark_summary_retrying", new=AsyncMock()),
        patch("helixis.webhooks.worker.mark_summary_failed", new=AsyncMock()) as failed_mock,
    ):
        # Must not raise.
        await _process_summary_row(row)

    failed_mock.assert_awaited_once()
    assert "ZeroDivisionError" in failed_mock.await_args.kwargs["error"]


# ── mark_summary_* SQL pinning ────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_summary_succeeded_sql_shape() -> None:
    """Pins the UPDATE for the success path so a refactor can't
    silently change column semantics."""
    from contextlib import asynccontextmanager

    from helixis.webhooks.queue import mark_summary_succeeded

    captured: dict[str, Any] = {}

    class _FakeConn:
        async def execute(self, sql: str, *args: Any) -> None:
            captured["sql"] = sql
            captured["args"] = args

    @asynccontextmanager
    async def _acquire():
        yield _FakeConn()

    pool = MagicMock()
    pool.acquire = _acquire
    event_id = uuid.uuid4()
    await mark_summary_succeeded(pool, event_id, text="hello world", prompt_version="v1")

    sql = captured["sql"]
    assert "summary_status         = 'succeeded'" in sql
    assert "summary_text           = $2" in sql
    assert "summary_prompt_version = $3" in sql
    assert "summary_error          = NULL" in sql
    assert captured["args"] == (event_id, "hello world", "v1")


@pytest.mark.asyncio
async def test_mark_summary_retrying_truncates_long_error() -> None:
    from contextlib import asynccontextmanager

    from helixis.webhooks.queue import mark_summary_retrying

    captured: dict[str, Any] = {}

    class _FakeConn:
        async def execute(self, _sql: str, *args: Any) -> None:
            captured["args"] = args

    @asynccontextmanager
    async def _acquire():
        yield _FakeConn()

    pool = MagicMock()
    pool.acquire = _acquire

    huge = "y" * 5000
    await mark_summary_retrying(pool, uuid.uuid4(), error=huge)
    assert len(captured["args"][1]) == 2000


# ── reset_summary_for_resummarize ─────────────────────────────────


@pytest.mark.asyncio
async def test_reset_summary_for_resummarize_sql_shape() -> None:
    """The resummarize reset clears text-relevant lifecycle state
    (status / error / attempts / last_attempted_at) but does NOT
    touch summary_text — the old text stays visible until the
    worker replaces it. Matches the planner replan UX."""
    from contextlib import asynccontextmanager

    from helixis.webhooks.queue import reset_summary_for_resummarize

    captured: dict[str, Any] = {}

    class _FakeConn:
        async def fetchrow(self, sql: str, *args: Any) -> Any:
            captured["sql"] = sql
            captured["args"] = args
            return {"id": args[0]}

    @asynccontextmanager
    async def _acquire():
        yield _FakeConn()

    pool = MagicMock()
    pool.acquire = _acquire
    event_id = uuid.uuid4()
    updated = await reset_summary_for_resummarize(pool, event_id, company_id="company-pr92")

    assert updated is True
    sql = captured["sql"]
    assert "summary_status            = 'pending'" in sql
    assert "summary_error             = NULL" in sql
    assert "summary_attempts          = 0" in sql
    assert "summary_last_attempted_at = NULL" in sql
    # summary_text must NOT be reset — kept around as a placeholder
    # while the worker re-derives.
    assert "summary_text" not in sql
