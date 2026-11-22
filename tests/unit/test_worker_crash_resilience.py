"""Crash-resilience contract for ``run_worker`` (webhook worker).

Root cause class for the Render "helixis-webhooks-worker exited with
status 1" alert (2026-06-30): the loop's claim calls hit the DB raw, so
one transient Supabase/pooler failure propagated out of ``run_worker``
and killed the process. These tests pin the survival behavior:

* a claim call raising ``Exception`` → the loop logs, backs off, and
  keeps polling (never raises out of ``run_worker``);
* a claim call raising ``SystemExit`` (gotcha 7 — library ``sys.exit``)
  → same survival;
* a handler raising ``SystemExit`` → the event lands in retry/dead,
  the batch survives;
* ``init_pool`` failing transiently at boot → retried, not fatal;
* the stale-``running`` janitor runs at boot and its failure does not
  take the loop down.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from helixis.webhooks.queue import ClaimedEvent


@asynccontextmanager
async def _fake_buildium_client():
    yield MagicMock()


def _stub_settings() -> MagicMock:
    settings = MagicMock()
    settings.supabase.db_url = "postgresql://stub"
    settings.buildium.worker_batch_size = 5
    settings.buildium.worker_poll_interval_seconds = 60
    settings.buildium.worker_max_attempts = 5
    settings.llm.models = {}
    settings.llm.default_model = "gemini/gemini-2.5-flash"
    # run_worker now calls configure_langfuse/configure_tracer at boot (#563);
    # disabled here so they short-circuit instead of trying to set env vars from
    # MagicMock keys (TypeError: str expected, not MagicMock).
    settings.langfuse.enabled = False
    # Same for PostHog: a MagicMock key would build a real client and thread.
    settings.posthog.enabled = False
    return settings


def _base_patches(claim_batch: AsyncMock, extra: dict[str, object] | None = None):
    """The worker's collaborators, mocked down to loop-control logic.
    Mirrors tests/unit/test_webhook_worker_shutdown.py's fixture, with
    an injectable ``claim_batch`` + instant sleeps so backoff paths
    don't slow the suite."""
    patches = [
        patch("helixis.webhooks.worker.get_settings", return_value=_stub_settings()),
        patch("helixis.webhooks.worker.init_pool", new=AsyncMock()),
        patch("helixis.webhooks.worker.get_pool", return_value=MagicMock()),
        patch("helixis.webhooks.worker.claim_batch", new=claim_batch),
        patch("helixis.webhooks.worker.claim_planner_batch", new=AsyncMock(return_value=[])),
        patch("helixis.webhooks.worker.claim_summary_batch", new=AsyncMock(return_value=[])),
        patch(
            "helixis.webhooks.worker.reset_stale_running",
            new=AsyncMock(return_value=(0, 0)),
        ),
        patch(
            "helixis.webhooks.worker.BuildiumClient",
            return_value=_fake_buildium_client(),
        ),
        # Collapse both the error backoff and the inter-poll sleep so
        # the loop spins through its iterations instantly.
        patch("helixis.webhooks.worker._interruptible_sleep", new=AsyncMock()),
    ]
    for target, value in (extra or {}).items():
        patches.append(patch(target, new=value))
    return patches


async def _run_with(patches, **kwargs) -> None:
    from helixis.webhooks.worker import run_worker

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        async with asyncio.timeout(5.0):
            await run_worker(**kwargs)


async def test_transient_claim_error_does_not_kill_worker():
    """claim_batch raising twice must not escape run_worker — the loop
    backs off and keeps polling until the empty-poll exit."""
    claim = AsyncMock(side_effect=[ConnectionError("pooler blip"), OSError("net"), [], []])
    await _run_with(_base_patches(claim), stop_after_empty_polls=2)
    # 2 failures + 2 empty polls — the loop survived both errors.
    assert claim.await_count == 4


async def test_systemexit_from_claim_does_not_kill_worker():
    """Gotcha 7 class: a library sys.exit(1) inside the claim path must
    be survived like any transient error."""
    claim = AsyncMock(side_effect=[SystemExit(1), [], []])
    await _run_with(_base_patches(claim), stop_after_empty_polls=2)
    assert claim.await_count == 3


async def test_janitor_failure_does_not_kill_worker():
    """The stale-running janitor is best-effort: if its DB call raises,
    claims must still run and the loop must still exit cleanly."""
    claim = AsyncMock(return_value=[])
    janitor = AsyncMock(side_effect=RuntimeError("janitor db error"))
    patches = _base_patches(
        claim,
        extra={"helixis.webhooks.worker.reset_stale_running": janitor},
    )
    await _run_with(patches, stop_after_empty_polls=1)
    assert janitor.await_count == 1
    assert claim.await_count == 1


async def test_janitor_swept_at_boot():
    """last_janitor starts at 0.0 — the first iteration must sweep, so
    rows orphaned by the previous worker death are rescued on boot."""
    claim = AsyncMock(return_value=[])
    janitor = AsyncMock(return_value=(3, 1))
    patches = _base_patches(
        claim,
        extra={"helixis.webhooks.worker.reset_stale_running": janitor},
    )
    await _run_with(patches, stop_after_empty_polls=1)
    assert janitor.await_count == 1


async def test_init_pool_retries_then_succeeds():
    """A deploy racing a briefly-unavailable DB retries instead of
    crash-looping; a persistent failure still raises (fail-fast on a
    genuinely wrong DB URL)."""
    init = AsyncMock(side_effect=[ConnectionError("boot blip"), ConnectionError("again"), None])
    claim = AsyncMock(return_value=[])
    patches = _base_patches(claim, extra={"helixis.webhooks.worker.init_pool": init})
    await _run_with(patches, stop_after_empty_polls=1)
    assert init.await_count == 3


async def test_init_pool_persistent_failure_still_raises():
    from helixis.webhooks.worker import _BOOT_INIT_ATTEMPTS

    init = AsyncMock(side_effect=ConnectionError("db is really down"))
    claim = AsyncMock(return_value=[])
    patches = _base_patches(claim, extra={"helixis.webhooks.worker.init_pool": init})
    with pytest.raises(ConnectionError):
        await _run_with(patches, stop_after_empty_polls=1)
    assert init.await_count == _BOOT_INIT_ATTEMPTS


async def test_handler_systemexit_lands_in_retry_not_crash():
    """A poison event whose handler sys.exit()s must be marked for
    retry (or dead at the cap) — never propagate out of the batch."""
    from helixis.webhooks import worker as worker_mod

    event = ClaimedEvent(
        id=uuid4(),
        event_name="Task.Created",
        event_time=datetime.now(UTC),
        account_id=1,
        company_id="c-1",
        entity_ids={},
        payload={},
        attempts=1,
    )

    mark_retry = AsyncMock()
    with (
        patch.object(worker_mod, "dispatch", return_value=AsyncMock(side_effect=SystemExit(1))),
        patch.object(worker_mod, "get_pool", return_value=MagicMock()),
        patch.object(worker_mod, "mark_failed_for_retry", mark_retry),
        patch.object(worker_mod, "ph_capture", MagicMock()),
    ):
        # Must not raise SystemExit out of the processor.
        await worker_mod._process_event(event, api=MagicMock(), max_attempts=5)

    assert mark_retry.await_count == 1


async def test_handler_systemexit_at_cap_marks_dead():
    from helixis.webhooks import worker as worker_mod

    event = ClaimedEvent(
        id=uuid4(),
        event_name="Task.Created",
        event_time=datetime.now(UTC),
        account_id=1,
        company_id="c-1",
        entity_ids={},
        payload={},
        attempts=5,
    )
    mark_dead = AsyncMock()
    with (
        patch.object(worker_mod, "dispatch", return_value=AsyncMock(side_effect=SystemExit(1))),
        patch.object(worker_mod, "get_pool", return_value=MagicMock()),
        patch.object(worker_mod, "mark_dead", mark_dead),
        patch.object(worker_mod, "ph_capture", MagicMock()),
    ):
        await worker_mod._process_event(event, api=MagicMock(), max_attempts=5)

    assert mark_dead.await_count == 1


async def test_reset_stale_running_issues_dead_then_requeue():
    """Pin the janitor's SQL contract: over-cap rows are failed FIRST,
    then remaining stale rows are requeued — and the counts returned
    match the row counts."""
    from helixis.webhooks.queue import reset_stale_running

    calls: list[tuple[str, tuple]] = []

    class _FakeConn:
        async def fetch(self, query: str, *args):
            calls.append((query, args))
            if "status = 'failed'" in query:
                return [{"id": uuid4()}]
            return [{"id": uuid4()}, {"id": uuid4()}]

    class _FakeAcquire:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, *exc):
            return False

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_FakeAcquire())

    requeued, dead = await reset_stale_running(pool, stale_after_seconds=600, max_attempts=5)

    assert (requeued, dead) == (2, 1)
    assert len(calls) == 2
    dead_query, dead_args = calls[0]
    requeue_query, requeue_args = calls[1]
    assert "attempts >= $2" in dead_query
    assert dead_args == (600, 5)
    assert "status = 'pending'" in requeue_query
    assert requeue_args == (600,)
