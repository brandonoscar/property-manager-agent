"""Unit tests for the webhook-queue-depth threshold core (LED-019)."""

from __future__ import annotations

from helixis.smoke.checks import (
    _QUEUE_MAX_READY_AGE_S,
    _QUEUE_MAX_READY_DEPTH,
    _queue_health,
)


def test_empty_queue_passes():
    status, detail = _queue_health(0, None)
    assert status == "pass"
    assert "empty" in detail


def test_small_fresh_queue_passes():
    status, _ = _queue_health(5, 3.0)
    assert status == "pass"


def test_old_ready_event_fails():
    # A single ready event older than the age budget = worker not draining.
    status, detail = _queue_health(1, _QUEUE_MAX_READY_AGE_S + 60)
    assert status == "fail"
    assert "not draining" in detail


def test_deep_but_fresh_queue_fails_on_depth():
    status, detail = _queue_health(_QUEUE_MAX_READY_DEPTH + 1, 10.0)
    assert status == "fail"
    assert "depth" in detail


def test_at_thresholds_still_passes():
    # Exactly at (not over) both thresholds is not yet a failure.
    status, _ = _queue_health(_QUEUE_MAX_READY_DEPTH, float(_QUEUE_MAX_READY_AGE_S))
    assert status == "pass"


def test_none_age_with_count_treated_as_zero_age():
    # Defensive: ready_count>0 but null age → treat age as 0, don't crash.
    status, _ = _queue_health(3, None)
    assert status == "pass"
