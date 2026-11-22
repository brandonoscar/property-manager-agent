"""Unit tests for ``observability.reason_codes`` (loose-ends batch).

The helper is telemetry glue: what matters is (a) it never raises, (b) it
resolves the trace id (explicit arg beats context beats no-op), (c) the
event/score shapes stay stable for the Langfuse dashboard, and (d) the
vocabulary stays closed (unknown slugs still emit but warn).
"""

from __future__ import annotations

from unittest.mock import patch

from helixis.observability.context import trace_context
from helixis.observability.reason_codes import REASON_CODES, record_reason_code


def test_vocabulary_contains_the_wired_codes() -> None:
    # Each of these has a live call site; removing one from the vocabulary
    # without unwiring the seam would spam the unknown-slug warning.
    assert {
        "planner_signal_dropped",
        "buildium_auth_error",
        # The 403 sibling. It shared buildium_auth_error until they were split,
        # so "how many customers need to reconnect" was counting customers
        # whose connection is fine and whose API key is missing a permission.
        "buildium_permission_denied",
        "rr_write_blocked",
        "budget_exceeded",
        "stream_stall",
    } <= REASON_CODES


def test_explicit_trace_id_records_event_and_score() -> None:
    with (
        patch("helixis.observability.reason_codes.tracing.record_event") as event,
        patch("helixis.observability.reason_codes.tracing.record_score") as score,
    ):
        record_reason_code("stream_stall", detail="no stream event in 90s", trace_id="t-123")
    event.assert_called_once()
    kwargs = event.call_args.kwargs
    assert kwargs["trace_id"] == "t-123"
    assert kwargs["name"] == "reason_code"
    assert kwargs["level"] == "WARNING"
    assert kwargs["payload"] == {"code": "stream_stall", "detail": "no stream event in 90s"}
    score.assert_called_once()
    skw = score.call_args.kwargs
    assert skw["trace_id"] == "t-123"
    assert skw["name"] == "reason:stream_stall"
    assert skw["value"] == 1.0


def test_falls_back_to_the_current_trace_context() -> None:
    with (
        patch("helixis.observability.reason_codes.tracing.record_event") as event,
        patch("helixis.observability.reason_codes.tracing.record_score"),
        trace_context(company_id="c1", session_id="s1", trace_id="ctx-trace"),
    ):
        record_reason_code("budget_exceeded")
    assert event.call_args.kwargs["trace_id"] == "ctx-trace"


def test_no_trace_anywhere_is_a_quiet_no_op_on_langfuse() -> None:
    # Outside any run (e.g. the worker planner path with tracing off) the
    # helper must still log-and-return without touching the tracing layer.
    with (
        patch("helixis.observability.reason_codes.tracing.record_event") as event,
        patch("helixis.observability.reason_codes.tracing.record_score") as score,
    ):
        record_reason_code("planner_signal_dropped", detail="2 dropped")
    event.assert_not_called()
    score.assert_not_called()


def test_unknown_code_still_emits_but_warns(caplog) -> None:
    with (
        patch("helixis.observability.reason_codes.tracing.record_event") as event,
        patch("helixis.observability.reason_codes.tracing.record_score"),
        caplog.at_level("WARNING"),
    ):
        record_reason_code("not_a_real_code", trace_id="t-1")
    event.assert_called_once()  # the datapoint is never dropped
    assert any("not in REASON_CODES" in r.message for r in caplog.records)


def test_never_raises_even_when_tracing_explodes() -> None:
    with patch(
        "helixis.observability.reason_codes.tracing.record_event",
        side_effect=RuntimeError("langfuse down"),
    ):
        record_reason_code("stream_stall", trace_id="t-1")  # must not raise


def test_detail_is_truncated() -> None:
    with (
        patch("helixis.observability.reason_codes.tracing.record_event") as event,
        patch("helixis.observability.reason_codes.tracing.record_score") as score,
    ):
        record_reason_code("rr_write_blocked", detail="x" * 2000, trace_id="t-1")
    assert len(event.call_args.kwargs["payload"]["detail"]) == 500
    assert len(score.call_args.kwargs["comment"]) == 200
