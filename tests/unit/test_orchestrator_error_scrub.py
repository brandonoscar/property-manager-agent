"""Regression guard: the orchestrator's terminal error path must not leak the
raw exception string to the user (LED-026).

Driving run_agent_streaming to its catch-all needs the full SDK streaming
harness, which is disproportionate for a user-facing string scrub. Instead we
pin the specific regression: the generic handler previously did
``error_event(str(e), recoverable=False)``, publishing a DB-driver / SDK
internal string straight into the chat bubble. It must publish a fixed friendly
message instead — while str(e) stays server-side (logger.exception + PostHog +
orchestrator_runs.error_message).
"""

from __future__ import annotations

import inspect

from helixis.agents import orchestrator


def test_generic_error_path_does_not_publish_raw_exception() -> None:
    src = inspect.getsource(orchestrator)
    # The exact leak we removed: the raw exception in a user-facing error event.
    assert "error_event(str(e)" not in src, (
        "the terminal error handler must not publish the raw exception to the "
        "user — use a fixed friendly message and keep str(e) server-side (LED-026)"
    )


def test_str_e_is_still_retained_server_side() -> None:
    # The exception detail must NOT be dropped entirely — it stays in the
    # server-side telemetry (PostHog error property + orchestrator_runs).
    src = inspect.getsource(orchestrator)
    assert "str(e)[:200]" in src  # PostHog error property
    assert "error_message=str(e)" in src  # orchestrator_runs failed-run record
