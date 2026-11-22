"""PR #89 — planner worker loop tests.

The planner moved out of the synchronous Buildium webhook receiver
into the worker. This file pins:

  - ``claim_planner_batch`` SQL shape (atomic
    pending → in_progress flip, attempts++, last_attempted_at stamp,
    cooldown + max_attempts gates, FOR UPDATE SKIP LOCKED).
  - ``_process_planner_row`` happy path: planner succeeds →
    ``coalesce_action_surface`` is called with both surfaces, row
    flipped to ``planner_status='succeeded'``.
  - ``_process_planner_row`` timeout: ``litellm.Timeout`` →
    ``planner_status='timed_out'``, deterministic floor untouched.
  - ``_process_planner_row`` planner-output failure:
    ``PlannerError`` → ``planner_status='failed'``.
  - ``_process_planner_row`` no-handler:
    ``get_enrichment_handler`` returns None → fast-fail to
    ``planner_status='failed'`` with a descriptive error.
  - ``_process_planner_row`` enrichment-missing: row has
    ``enrichment IS NULL`` → fast-fail (the receiver should always
    have persisted enrichment first; missing is itself a bug worth
    surfacing).
  - Max-attempts cap: a row past 5 attempts that times out flips
    to ``failed`` rather than ``timed_out`` so the inbox shows the
    Replan affordance instead of infinite spinner.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import litellm
import pytest

from helixis.agent.enrichment.models import (
    EnrichedEvent,
    EnrichedTask,
    EnrichedUnit,
)
from helixis.agent.planner import PlannerError
from helixis.agent.planner.models import ActionSurface, ProposedAction
from helixis.webhooks.queue import PlannerClaimedRow

# ── Shared fixtures ────────────────────────────────────────────────


def _enriched_dict() -> dict[str, Any]:
    """A realistic enrichment JSON the receiver would have persisted."""
    return EnrichedEvent(
        event_name="Task.Created",
        company_id="company-pr89",
        task=EnrichedTask(
            id=17435,
            title="Loud crashing",
            description="It came from upstairs",
            category="Maintenance",
            priority="High",
            assigned_to_user_id=42,
            requested_by_first_name="Sarah",
            requested_by_last_name="Chen",
        ),
        unit=EnrichedUnit(
            id=412,
            property_id=100,
            unit_number="4B",
            address_line1="123 Main St",
        ),
    ).model_dump(mode="json")


def _action_surface() -> ActionSurface:
    """A minimum-valid ActionSurface a fake planner can return."""
    return ActionSurface(
        summary="Loud crashing — Sarah Chen — 123 Main St #4B",
        actions=[
            ProposedAction(
                tool_id="reply_to_tenant",
                label="Reply",
                intent="Ask Sarah for more detail",
                parameters={},
            ),
        ],
        freeform_placeholder="Type a reply…",
    )


def _planner_row(
    *,
    enrichment: dict[str, Any] | None = None,
    planner_attempts: int = 0,
    deterministic_surface: dict[str, Any] | None = None,
) -> PlannerClaimedRow:
    """Synthetic PlannerClaimedRow as the claim function would emit."""
    return PlannerClaimedRow(
        id=uuid.uuid4(),
        event_name="Task.Created",
        company_id="company-pr89",
        payload={"EventName": "Task.Created", "TaskId": 17435},
        enrichment=enrichment if enrichment is not None else _enriched_dict(),
        routing_decision=None,
        deterministic_surface=deterministic_surface
        or {
            "summary": "Loud crashing (floor)",
            "actions": [{"tool_id": "noop_view_buildium", "label": "View"}],
            "freeform_placeholder": "Type…",
        },
        planner_attempts=planner_attempts,
    )


def _fake_handler(
    *,
    plan_returns: ActionSurface | None = None,
    plan_raises: Exception | None = None,
    enrich_returns: EnrichedEvent | None = None,
    enrich_raises: Exception | None = None,
) -> MagicMock:
    """Build a fake EnrichmentHandler. The ``planner`` half is the
    primary subject of the PR #89 tests; the ``enricher`` half was
    a no-op stub pre-PR-#90 but is now exercised by the
    re-enrichment branch in ``_process_planner_row``."""
    handler = MagicMock()
    handler.planner = MagicMock()
    if plan_raises is not None:
        handler.planner.plan = AsyncMock(side_effect=plan_raises)
    else:
        handler.planner.plan = AsyncMock(return_value=plan_returns or _action_surface())
    # PR #90 — enricher is used by the re-enrichment branch when a
    # legacy row arrives with ``enrichment IS NULL``. Default returns
    # a realistic EnrichedEvent so tests that DON'T exercise the
    # missing-enrichment path (the vast majority) just work; tests
    # that DO exercise it override via ``enrich_returns`` /
    # ``enrich_raises``.
    handler.enricher = MagicMock()
    if enrich_raises is not None:
        handler.enricher.enrich = AsyncMock(side_effect=enrich_raises)
    else:
        # Reconstruct from the same dict the test fixture uses so the
        # planner sees a coherent shape on the happy-replan path.
        enriched_default = enrich_returns or EnrichedEvent.model_validate(_enriched_dict())
        handler.enricher.enrich = AsyncMock(return_value=enriched_default)
    return handler


# ── Test 1 — claim SQL shape ──────────────────────────────────────


@pytest.mark.asyncio
async def test_claim_planner_batch_executes_expected_sql() -> None:
    """The atomic claim must filter by planner_status='pending', the
    cooldown window, AND planner_attempts < max — then flip to
    'in_progress' and bump attempts/timestamp. FOR UPDATE SKIP
    LOCKED keeps multiple worker replicas from fighting."""
    from helixis.webhooks.queue import claim_planner_batch

    captured: dict[str, Any] = {}

    async def _fake_fetch(sql: str, *args: Any) -> list[Any]:
        captured["sql"] = sql
        captured["args"] = args
        return []

    pool = MagicMock()
    pool.fetch = _fake_fetch

    await claim_planner_batch(pool, batch_size=7, max_attempts=5, cooldown_seconds=30)

    sql = captured["sql"]
    args = captured["args"]
    # ⚠ Was ``planner_status = 'pending'``. Widened deliberately 2026-08-29:
    # a row abandoned mid-plan sat at ``in_progress`` and matched nothing, so
    # 7 rows stalled for 52 days and ``timed_out`` was a fourth terminal state
    # rather than a retry. See test_planner_queue_retry.py.
    assert "planner_status IN ('pending', 'in_progress', 'timed_out')" in sql
    assert "planner_attempts < $2" in sql
    # ⚠ RE-POINTED 2026-09-10, not weakened. The claim is now a two-CTE
    # round-robin (webhooks/fair_queue), so the lock names its table:
    # Postgres refuses a bare FOR UPDATE over a join to a CTE. The
    # property asserted — concurrent workers skip rather than block — is
    # unchanged, and this literal is what the live server accepted.
    assert "FOR UPDATE OF b SKIP LOCKED" in sql
    assert "planner_status            = 'in_progress'" in sql
    assert "planner_attempts          = planner_attempts + 1" in sql
    assert "planner_last_attempted_at = now()" in sql
    assert args == (7, 5, 30)


@pytest.mark.asyncio
async def test_claim_planner_batch_waits_for_enrichment_or_grace_window() -> None:
    """PR #128 — race fix. Receiver inserts the row BEFORE running
    Buildium API calls for enrichment (~1-3s), so the worker's poll
    cycle was claiming rows between INSERT and the enrichment UPDATE.
    The planner then ran with NULL enrichment and produced generic
    "Add note + Create task" actions instead of context-aware ones.

    Pin the SQL clause: claim only rows where enrichment IS NOT NULL,
    OR ``received_at`` is older than 15 seconds (grace window so a
    row with legitimately no enrichment doesn't stay stuck forever).
    """
    from helixis.webhooks.queue import claim_planner_batch

    captured: dict[str, Any] = {}

    async def _fake_fetch(sql: str, *args: Any) -> list[Any]:
        captured["sql"] = sql
        return []

    pool = MagicMock()
    pool.fetch = _fake_fetch

    # Default path (no prompt-version self-heal).
    await claim_planner_batch(pool, batch_size=7, max_attempts=5, cooldown_seconds=30)
    sql = captured["sql"]
    assert "enrichment IS NOT NULL" in sql
    assert "received_at < now() - interval '15 seconds'" in sql

    # WITH prompt-version arg (the PR #107 self-heal path).
    captured.clear()
    await claim_planner_batch(
        pool,
        batch_size=7,
        max_attempts=5,
        cooldown_seconds=30,
        current_prompt_version="v3",
    )
    sql = captured["sql"]
    assert "enrichment IS NOT NULL" in sql
    assert "received_at < now() - interval '15 seconds'" in sql


@pytest.mark.asyncio
async def test_claim_planner_batch_rejects_unknown_table() -> None:
    """SQL-injection defense: the table_name allow-list must
    refuse anything not in ``_PLANNER_QUEUE_TABLES``."""
    from helixis.webhooks.queue import claim_planner_batch

    pool = MagicMock()
    with pytest.raises(ValueError, match="unsupported planner queue table"):
        await claim_planner_batch(pool, table_name="buildium_events; DROP TABLE foo--")


# ── Test 2 — happy path (planner succeeds) ────────────────────────


@pytest.mark.asyncio
async def test_process_planner_row_success_coalesces_and_marks_succeeded() -> None:
    """Planner returns a valid surface → coalesce_action_surface
    persists it, mark_planner_succeeded flips the row."""
    from helixis.webhooks.worker import _process_planner_row

    row = _planner_row()
    handler = _fake_handler()

    with (
        patch("helixis.webhooks.worker.get_enrichment_handler", return_value=handler),
        patch("helixis.webhooks.worker.get_pool", return_value=MagicMock()),
        patch("helixis.webhooks.worker.coalesce_action_surface", new=AsyncMock()) as coalesce_mock,
        patch("helixis.webhooks.worker.mark_planner_succeeded", new=AsyncMock()) as ok_mock,
        patch("helixis.webhooks.worker.mark_planner_timed_out", new=AsyncMock()) as timeout_mock,
        patch("helixis.webhooks.worker.mark_planner_failed", new=AsyncMock()) as failed_mock,
    ):
        await _process_planner_row(row)

    handler.planner.plan.assert_awaited_once()
    coalesce_mock.assert_awaited_once()
    coalesce_kwargs = coalesce_mock.await_args.kwargs
    assert coalesce_kwargs["llm_surface"]["summary"].startswith("Loud crashing")
    assert coalesce_kwargs["deterministic_surface"] is not None
    ok_mock.assert_awaited_once()
    timeout_mock.assert_not_awaited()
    failed_mock.assert_not_awaited()


# ── Test 3 — timeout path ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_planner_row_timeout_marks_timed_out() -> None:
    """``litellm.Timeout`` → planner_status='timed_out', deterministic
    floor stays. No coalesce call (we don't have a valid surface)."""
    from helixis.webhooks.worker import _process_planner_row

    row = _planner_row(planner_attempts=1)  # under the cap
    timeout_exc = litellm.Timeout(
        "Connection timed out. Timeout passed=30.0, time taken=30.5 seconds.",
        model="gemini/gemini-2.5-flash",
        llm_provider="gemini",
    )
    handler = _fake_handler(plan_raises=timeout_exc)

    with (
        patch("helixis.webhooks.worker.get_enrichment_handler", return_value=handler),
        patch("helixis.webhooks.worker.get_pool", return_value=MagicMock()),
        patch("helixis.webhooks.worker.coalesce_action_surface", new=AsyncMock()) as coalesce_mock,
        patch("helixis.webhooks.worker.mark_planner_succeeded", new=AsyncMock()) as ok_mock,
        patch("helixis.webhooks.worker.mark_planner_timed_out", new=AsyncMock()) as timeout_mock,
        patch("helixis.webhooks.worker.mark_planner_failed", new=AsyncMock()) as failed_mock,
    ):
        await _process_planner_row(row)

    coalesce_mock.assert_not_awaited()
    ok_mock.assert_not_awaited()
    timeout_mock.assert_awaited_once()
    failed_mock.assert_not_awaited()
    # Error text carries the litellm.Timeout repr so audits can grep it.
    err = timeout_mock.await_args.kwargs["error"]
    assert "Timeout" in err
    assert "30" in err  # the timeout value


@pytest.mark.asyncio
async def test_process_planner_row_timeout_past_max_marks_failed() -> None:
    """At ``planner_attempts >= _PLANNER_MAX_ATTEMPTS``, a timeout
    flips to 'failed' rather than 'timed_out' so the inbox stops
    looking like it might still succeed."""
    from helixis.webhooks.worker import _PLANNER_MAX_ATTEMPTS, _process_planner_row

    row = _planner_row(planner_attempts=_PLANNER_MAX_ATTEMPTS)
    timeout_exc = litellm.Timeout(
        "still hitting the wall",
        model="gemini/gemini-2.5-flash",
        llm_provider="gemini",
    )
    handler = _fake_handler(plan_raises=timeout_exc)

    with (
        patch("helixis.webhooks.worker.get_enrichment_handler", return_value=handler),
        patch("helixis.webhooks.worker.get_pool", return_value=MagicMock()),
        patch("helixis.webhooks.worker.coalesce_action_surface", new=AsyncMock()),
        patch("helixis.webhooks.worker.mark_planner_succeeded", new=AsyncMock()),
        patch("helixis.webhooks.worker.mark_planner_timed_out", new=AsyncMock()) as timeout_mock,
        patch("helixis.webhooks.worker.mark_planner_failed", new=AsyncMock()) as failed_mock,
    ):
        await _process_planner_row(row)

    timeout_mock.assert_not_awaited()
    failed_mock.assert_awaited_once()
    err = failed_mock.await_args.kwargs["error"]
    # ⚠ Was ``"timed out" in err``. The branch now covers every transient
    # upstream failure — connection errors, 5xx, rate limits — not only
    # timeouts, so a hardcoded "timed out" would be a false statement for
    # three of the four. What the assertion is actually for is that the cause
    # and the attempt count survive into the recorded error.
    assert "after 5 attempts" in err.lower()
    assert "timeout" in err.lower()
    assert "5" in err  # attempts count made it in


# ── Test 4 — PlannerError path ────────────────────────────────────


@pytest.mark.asyncio
async def test_process_planner_row_planner_error_marks_failed() -> None:
    """``PlannerError`` (LLM returned invalid output) → 'failed'.
    The deterministic floor stays — inbox shows Replan affordance."""
    from helixis.webhooks.worker import _process_planner_row

    row = _planner_row()
    handler = _fake_handler(plan_raises=PlannerError("schema mismatch on action[0].tool_id"))

    with (
        patch("helixis.webhooks.worker.get_enrichment_handler", return_value=handler),
        patch("helixis.webhooks.worker.get_pool", return_value=MagicMock()),
        patch("helixis.webhooks.worker.coalesce_action_surface", new=AsyncMock()) as coalesce_mock,
        patch("helixis.webhooks.worker.mark_planner_succeeded", new=AsyncMock()) as ok_mock,
        patch("helixis.webhooks.worker.mark_planner_timed_out", new=AsyncMock()) as timeout_mock,
        patch("helixis.webhooks.worker.mark_planner_failed", new=AsyncMock()) as failed_mock,
    ):
        await _process_planner_row(row)

    coalesce_mock.assert_not_awaited()
    ok_mock.assert_not_awaited()
    timeout_mock.assert_not_awaited()
    failed_mock.assert_awaited_once()
    assert "schema mismatch" in failed_mock.await_args.kwargs["error"]


# ── Test 5 — no enrichment handler ────────────────────────────────


@pytest.mark.asyncio
async def test_process_planner_row_no_handler_fast_fails() -> None:
    """If the event_name isn't in the enrichment registry, the
    worker marks the row 'failed' rather than retrying indefinitely.
    Defensive — in practice the receiver only enqueues planner work
    for events the registry covers."""
    from helixis.webhooks.worker import _process_planner_row

    row = _planner_row()

    with (
        patch("helixis.webhooks.worker.get_enrichment_handler", return_value=None),
        patch("helixis.webhooks.worker.get_pool", return_value=MagicMock()),
        patch("helixis.webhooks.worker.coalesce_action_surface", new=AsyncMock()) as coalesce_mock,
        patch("helixis.webhooks.worker.mark_planner_succeeded", new=AsyncMock()) as ok_mock,
        patch("helixis.webhooks.worker.mark_planner_failed", new=AsyncMock()) as failed_mock,
    ):
        await _process_planner_row(row)

    coalesce_mock.assert_not_awaited()
    ok_mock.assert_not_awaited()
    failed_mock.assert_awaited_once()
    assert "no enrichment handler" in failed_mock.await_args.kwargs["error"]


# ── Test 6 — missing enrichment ───────────────────────────────────


@pytest.mark.asyncio
async def test_process_planner_row_reenriches_when_enrichment_null() -> None:
    """PR #90 — pre-PR-#83 legacy rows arrive with ``enrichment IS NULL``
    because the receiver of the day did not persist it. The worker
    self-heals by re-running ``handler.enricher.enrich(payload, company_id)``
    in-place (mirroring router.py:208 exactly), persisting the result
    via an UPDATE so subsequent replans skip the re-enrich, and
    continuing to the planner with the freshly-computed
    EnrichedEvent. ``mark_planner_failed`` is NOT called."""
    from helixis.webhooks.worker import _process_planner_row

    row = _planner_row()
    row.enrichment = None
    handler = _fake_handler()

    pool_mock = MagicMock()
    pool_mock.execute = AsyncMock()

    with (
        patch("helixis.webhooks.worker.get_enrichment_handler", return_value=handler),
        patch("helixis.webhooks.worker.get_pool", return_value=pool_mock),
        patch("helixis.webhooks.worker.coalesce_action_surface", new=AsyncMock()) as coalesce_mock,
        patch("helixis.webhooks.worker.mark_planner_succeeded", new=AsyncMock()) as ok_mock,
        patch("helixis.webhooks.worker.mark_planner_failed", new=AsyncMock()) as failed_mock,
    ):
        await _process_planner_row(row)

    # 1) enricher was called with the exact same shape as the receiver:
    #    (payload_dict, company_id) — positional, no keyword args.
    handler.enricher.enrich.assert_awaited_once()
    enrich_args = handler.enricher.enrich.await_args.args
    assert enrich_args[0] == row.payload
    assert enrich_args[1] == row.company_id

    # 2) The freshly-computed enrichment was persisted via UPDATE so
    #    a future replan hits the fast path.
    pool_mock.execute.assert_awaited_once()
    sql, json_payload, persisted_id = pool_mock.execute.await_args.args
    assert "UPDATE buildium_events" in sql
    assert "SET enrichment = $1::jsonb" in sql
    assert persisted_id == row.id
    # The persisted JSON is parseable and carries the right event_name.
    persisted = json.loads(json_payload)
    assert persisted["event_name"] == "Task.Created"

    # 3) Planner ran against the re-enriched event, the surface was
    #    coalesced, and the row was flipped to succeeded.
    handler.planner.plan.assert_awaited_once()
    coalesce_mock.assert_awaited_once()
    ok_mock.assert_awaited_once()
    failed_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_planner_row_marks_failed_when_reenrichment_raises() -> None:
    """PR #90 — if the re-enrichment itself raises (e.g. Buildium 502
    on the task fetch, or a schema drift), the row ends at
    ``planner_status='failed'`` with the enrichment error captured in
    ``planner_error``. The planner is NOT called — we can't plan
    without a valid EnrichedEvent."""
    from helixis.webhooks.worker import _process_planner_row

    row = _planner_row()
    row.enrichment = None
    handler = _fake_handler(enrich_raises=ValueError("bad payload from buildium"))

    pool_mock = MagicMock()
    pool_mock.execute = AsyncMock()

    with (
        patch("helixis.webhooks.worker.get_enrichment_handler", return_value=handler),
        patch("helixis.webhooks.worker.get_pool", return_value=pool_mock),
        patch("helixis.webhooks.worker.coalesce_action_surface", new=AsyncMock()) as coalesce_mock,
        patch("helixis.webhooks.worker.mark_planner_succeeded", new=AsyncMock()) as ok_mock,
        patch("helixis.webhooks.worker.mark_planner_failed", new=AsyncMock()) as failed_mock,
    ):
        await _process_planner_row(row)

    handler.planner.plan.assert_not_awaited()
    coalesce_mock.assert_not_awaited()
    ok_mock.assert_not_awaited()
    failed_mock.assert_awaited_once()
    err = failed_mock.await_args.kwargs["error"]
    assert "re-enrichment failed" in err
    assert "bad payload" in err


@pytest.mark.asyncio
async def test_process_planner_row_continues_when_persist_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """PR #90 — persisting the re-enrichment is best-effort. If the
    UPDATE blows up (transient DB hiccup), the worker keeps going to
    the planner using the in-memory ``EnrichedEvent`` — losing the
    persist just means the NEXT replan re-enriches again, not that
    THIS run fails. A warning is logged."""
    import logging

    from helixis.webhooks.worker import _process_planner_row

    row = _planner_row()
    row.enrichment = None
    handler = _fake_handler()

    pool_mock = MagicMock()
    pool_mock.execute = AsyncMock(side_effect=RuntimeError("connection lost"))

    with (
        patch("helixis.webhooks.worker.get_enrichment_handler", return_value=handler),
        patch("helixis.webhooks.worker.get_pool", return_value=pool_mock),
        patch("helixis.webhooks.worker.coalesce_action_surface", new=AsyncMock()),
        patch("helixis.webhooks.worker.mark_planner_succeeded", new=AsyncMock()) as ok_mock,
        patch("helixis.webhooks.worker.mark_planner_failed", new=AsyncMock()) as failed_mock,
        caplog.at_level(logging.WARNING, logger="helixis.webhooks.worker"),
    ):
        await _process_planner_row(row)

    # Planner still ran — persist failure is non-fatal.
    handler.planner.plan.assert_awaited_once()
    ok_mock.assert_awaited_once()
    failed_mock.assert_not_awaited()
    # And we logged a warning about it.
    assert any(
        "could not persist re-enrichment" in record.getMessage().lower()
        or "could not persist" in record.getMessage().lower()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_process_planner_row_uses_persisted_enrichment_when_present() -> None:
    """PR #90 — the post-PR-#83 happy path: ``enrichment`` is already
    populated. The worker reconstructs the EnrichedEvent from the
    persisted dict and does NOT call ``enricher.enrich`` or issue an
    UPDATE on the enrichment column. Pin this so PR #90 doesn't add
    a Buildium round-trip to every planner row in production."""
    from helixis.webhooks.worker import _process_planner_row

    row = _planner_row()  # enrichment populated by default fixture
    assert row.enrichment is not None  # sanity check the fixture
    handler = _fake_handler()

    pool_mock = MagicMock()
    pool_mock.execute = AsyncMock()

    with (
        patch("helixis.webhooks.worker.get_enrichment_handler", return_value=handler),
        patch("helixis.webhooks.worker.get_pool", return_value=pool_mock),
        patch("helixis.webhooks.worker.coalesce_action_surface", new=AsyncMock()),
        patch("helixis.webhooks.worker.mark_planner_succeeded", new=AsyncMock()) as ok_mock,
    ):
        await _process_planner_row(row)

    # The fast path: no re-enrichment, no UPDATE on enrichment column.
    handler.enricher.enrich.assert_not_awaited()
    pool_mock.execute.assert_not_awaited()
    # Planner still ran on the persisted enrichment dict.
    handler.planner.plan.assert_awaited_once()
    ok_mock.assert_awaited_once()


# ── Test 7 — unexpected exception is caught ──────────────────────


@pytest.mark.asyncio
async def test_process_planner_row_unexpected_exception_marks_failed() -> None:
    """Any exception (not just PlannerError / Timeout) is caught and
    recorded as 'failed'. ``run_worker``'s ``gather(...)`` shape
    would crash the whole batch otherwise."""
    from helixis.webhooks.worker import _process_planner_row

    row = _planner_row()
    handler = _fake_handler(plan_raises=ZeroDivisionError("oh no"))

    with (
        patch("helixis.webhooks.worker.get_enrichment_handler", return_value=handler),
        patch("helixis.webhooks.worker.get_pool", return_value=MagicMock()),
        patch("helixis.webhooks.worker.coalesce_action_surface", new=AsyncMock()),
        patch("helixis.webhooks.worker.mark_planner_succeeded", new=AsyncMock()),
        patch("helixis.webhooks.worker.mark_planner_failed", new=AsyncMock()) as failed_mock,
    ):
        # Must not raise.
        await _process_planner_row(row)

    failed_mock.assert_awaited_once()
    assert "ZeroDivisionError" in failed_mock.await_args.kwargs["error"]


# ── Test 8 — mark_planner_* SQL shape pinning ────────────────────


@pytest.mark.asyncio
async def test_mark_planner_succeeded_sql_shape() -> None:
    """Pin the UPDATE for the success path so a refactor can't
    silently change the column semantics (e.g. leave the error in)."""
    from helixis.webhooks.queue import mark_planner_succeeded

    captured: dict[str, Any] = {}

    class _FakeConn:
        async def execute(self, sql: str, *args: Any) -> None:
            captured["sql"] = sql
            captured["args"] = args

    @pytest.MonkeyPatch.context()
    def _noop() -> None: ...

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _acquire():
        yield _FakeConn()

    pool = MagicMock()
    pool.acquire = _acquire
    event_id = uuid.uuid4()
    await mark_planner_succeeded(pool, event_id)

    sql = captured["sql"]
    assert "planner_status = 'succeeded'" in sql
    assert "planner_error  = NULL" in sql
    assert captured["args"] == (event_id,)


@pytest.mark.asyncio
async def test_mark_planner_timed_out_truncates_long_error() -> None:
    """planner_error column is bounded — long errors get truncated
    so a giant traceback doesn't blow the row size."""
    from helixis.webhooks.queue import mark_planner_timed_out

    captured: dict[str, Any] = {}

    class _FakeConn:
        async def execute(self, _sql: str, *args: Any) -> None:
            captured["args"] = args

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _acquire():
        yield _FakeConn()

    pool = MagicMock()
    pool.acquire = _acquire

    huge = "x" * 5000
    await mark_planner_timed_out(pool, uuid.uuid4(), error=huge)
    assert len(captured["args"][1]) == 2000
