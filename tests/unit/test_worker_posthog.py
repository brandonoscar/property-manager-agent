"""The worker starts and flushes PostHog itself.

The worker captures "webhook processed" / "webhook failed" (``ph_capture`` in
``webhooks/worker.py``), but only the API's FastAPI lifespan called
``configure_posthog``. The worker is a separate process, so every one of those
captures was a no-op whatever the environment said: PostHog had received no
server-side event at all, ever (measured 2026-10-23: 5,199 events since April,
every one from a browser).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .test_webhook_worker_shutdown import patched_loop  # noqa: F401 — the fixture


async def test_run_worker_configures_posthog_with_its_settings(patched_loop):  # noqa: F811
    from helixis.webhooks import worker

    stop = asyncio.Event()
    stop.set()
    with patch.object(worker, "configure_posthog") as configure:
        async with asyncio.timeout(2.0):
            await worker.run_worker(stop_event=stop)

    configure.assert_called_once_with(worker.get_settings())


async def test_shutdown_flushes_posthog_even_when_the_loop_raises() -> None:
    """The flush sits in the same ``finally`` as Sentry's, so a crash does not
    lose the events that explain it."""
    from helixis.webhooks import worker

    with (
        patch.object(worker, "run_worker", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch.object(worker, "shutdown_posthog") as posthog_down,
        patch.object(worker, "shutdown_sentry"),
        patch.object(worker, "shutdown_langfuse"),
        patch.object(worker.tracing, "flush", MagicMock()),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await worker._run_with_signal_handlers()

    posthog_down.assert_called_once_with()
