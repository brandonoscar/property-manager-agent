"""Graceful-shutdown contract for ``run_worker``.

Render delivers SIGTERM and waits up to 30 seconds before SIGKILL.
``run_worker`` must:
  - stop claiming new batches the moment the stop event is set,
  - return from the inter-poll sleep early instead of waiting out the
    full ``worker_poll_interval_seconds``,
  - exit cleanly without leaving rows stuck in ``running`` (handled by
    the per-batch ``asyncio.gather`` in production; the loop test only
    proves the iteration cadence).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@asynccontextmanager
async def _fake_buildium_client():
    """The real BuildiumClient context-manager opens an HTTP session and
    a semaphore; for the loop test we just need ``__aenter__`` /
    ``__aexit__`` to no-op."""
    yield MagicMock()


@pytest.fixture
def patched_loop():
    """Mock out the worker's external dependencies so ``run_worker`` only
    exercises the loop-control logic.

    - ``init_pool`` / ``get_pool`` are no-ops returning a MagicMock.
    - ``claim_batch`` always returns an empty list, so no real handlers
      run — the loop just polls and sleeps.
    - ``BuildiumClient`` is a noop async context manager.
    - ``get_settings`` returns a stub with the buildium block set up.
    """
    settings = MagicMock()
    settings.supabase.db_url = "postgresql://stub"
    settings.buildium.worker_batch_size = 5
    # 60s sleep — long enough that any test that "waits out" the sleep
    # would obviously time out, proving the early-exit path is the only
    # way the test can pass.
    settings.buildium.worker_poll_interval_seconds = 60
    settings.buildium.worker_max_attempts = 5
    # The worker boots through ``assert_no_openai_default`` (PR #69)
    # which inspects ``settings.llm.models`` + ``settings.llm.default_model``.
    # Set concrete Gemini values so the guard passes; without these the
    # MagicMock chain would resolve to a non-string and incorrectly
    # trip the OpenAI-prefix check.
    settings.llm.models = {}
    settings.llm.default_model = "gemini/gemini-2.5-flash"
    # run_worker now calls configure_langfuse/configure_tracer at boot (#563);
    # disabled here so they short-circuit instead of setting env vars from
    # MagicMock keys (TypeError: str expected, not MagicMock).
    settings.langfuse.enabled = False
    # Same for PostHog: a MagicMock key would build a real client and thread.
    settings.posthog.enabled = False

    with (
        patch("helixis.webhooks.worker.get_settings", return_value=settings),
        patch("helixis.webhooks.worker.init_pool", new=AsyncMock()),
        patch("helixis.webhooks.worker.get_pool", return_value=MagicMock()),
        patch(
            "helixis.webhooks.worker.claim_batch",
            new=AsyncMock(return_value=[]),
        ),
        # PR #89 — the planner claim runs every tick alongside the
        # legacy event claim. The shutdown test only cares about
        # loop-control, so stub it to return nothing.
        patch(
            "helixis.webhooks.worker.claim_planner_batch",
            new=AsyncMock(return_value=[]),
        ),
        # PR #92 — same idea for the summary claim. Worker now polls
        # three queues per tick; the shutdown test stubs all three
        # so the loop hits its empty-poll branch immediately.
        patch(
            "helixis.webhooks.worker.claim_summary_batch",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "helixis.webhooks.worker.BuildiumClient",
            return_value=_fake_buildium_client(),
        ),
    ):
        yield


async def test_stop_event_interrupts_inter_poll_sleep(patched_loop):
    """When the stop event is set during the inter-poll sleep, the loop
    must return immediately rather than waiting out the full 60s.

    This is the production SIGTERM path: signal handler sets the event,
    the running ``asyncio.wait_for(stop.wait(), timeout=60)`` resolves
    on the event side, the loop's top check breaks out.
    """
    from helixis.webhooks.worker import run_worker

    stop = asyncio.Event()

    async def fire_stop_after_short_delay() -> None:
        # Long enough for the worker to enter its first sleep, short
        # enough to keep the test under a second.
        await asyncio.sleep(0.05)
        stop.set()

    # If the stop-event interrupt didn't work, the worker would sleep
    # for 60s (per the patched poll interval) and the test timeout
    # would fire instead of completing.
    async with asyncio.timeout(2.0):
        await asyncio.gather(
            run_worker(stop_event=stop),
            fire_stop_after_short_delay(),
        )


async def test_stop_event_set_at_loop_top_exits_immediately(patched_loop):
    """If the event is already set when ``run_worker`` enters the loop,
    the very first iteration must return without claiming or sleeping."""
    from helixis.webhooks.worker import run_worker

    stop = asyncio.Event()
    stop.set()

    # Should return effectively instantly — well under the 60s sleep
    # this fixture configures.
    async with asyncio.timeout(2.0):
        await run_worker(stop_event=stop)
