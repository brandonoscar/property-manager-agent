"""Unit tests for latency budgets + over-budget alerting (#34).

The alert is best-effort telemetry, so these tests assert the pure decision
(within budget → None; over budget → a ``LatencyBreach``), that it's a no-op
when disabled, that the Langfuse alert fires only on an active tracer, and
that a tracer failure never propagates into the run.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from helixis.observability import latency
from helixis.settings import Settings


def _configure(monkeypatch, *, enabled=True, run_ms=20000, tool_ms=10000):
    settings = Settings(
        env="test",
        memory={"enabled": False},
        latency={"enabled": enabled, "agent_run_ms": run_ms, "tool_call_ms": tool_ms},
    )
    return latency.configure_latency(settings)


def test_configure_active_when_enabled(monkeypatch):
    assert _configure(monkeypatch) is True
    assert latency.is_active() is True


def test_configure_off_when_disabled(monkeypatch):
    assert _configure(monkeypatch, enabled=False) is False
    assert latency.is_active() is False
    # Every check is a no-op while disabled — even a wildly slow op.
    assert latency.check_latency("agent_run", 10_000_000) is None


def test_within_budget_returns_none(monkeypatch):
    _configure(monkeypatch, run_ms=20000, tool_ms=10000)
    assert latency.check_latency("agent_run", 19_999) is None
    assert latency.check_latency("tool_call", 10_000) is None  # equal is within


def test_over_budget_returns_breach(monkeypatch):
    _configure(monkeypatch, tool_ms=10000)
    # Keep tracing inactive so no Langfuse call is attempted.
    from helixis.observability import tracing

    monkeypatch.setattr(tracing, "_client", None)

    breach = latency.check_latency("tool_call", 25_000, label="buildium_read")
    assert breach is not None
    assert breach.kind == "tool_call"
    assert breach.label == "buildium_read"
    assert breach.elapsed_ms == 25_000
    assert breach.budget_ms == 10_000
    assert breach.over_ms == 15_000
    assert breach.over_ratio == 2.5


def test_unknown_kind_is_ignored(monkeypatch):
    _configure(monkeypatch)
    assert latency.check_latency("something_else", 10_000_000) is None


def test_alert_emitted_to_active_tracer(monkeypatch):
    _configure(monkeypatch, tool_ms=1000)
    from helixis.observability import tracing

    client = MagicMock()
    monkeypatch.setattr(tracing, "_client", client)

    breach = latency.check_latency("tool_call", 5000, trace_id="trace-1", label="web_search")
    assert breach is not None
    # A WARNING event to drill into + a numeric score to chart.
    client.event.assert_called_once()
    assert client.event.call_args.kwargs["name"] == "latency_over_budget"
    assert client.event.call_args.kwargs["level"] == "WARNING"
    client.score.assert_called_once()
    assert client.score.call_args.kwargs["name"] == "latency_over_budget:tool_call"
    assert client.score.call_args.kwargs["value"] == 4000.0


def test_no_alert_without_trace_id(monkeypatch):
    _configure(monkeypatch, tool_ms=1000)
    from helixis.observability import tracing

    client = MagicMock()
    monkeypatch.setattr(tracing, "_client", client)
    # A breach with no trace_id still returns, but emits nothing to Langfuse.
    assert latency.check_latency("tool_call", 5000) is not None
    client.event.assert_not_called()
    client.score.assert_not_called()


def test_tracer_failure_never_propagates(monkeypatch):
    _configure(monkeypatch, tool_ms=1000)
    from helixis.observability import tracing

    client = MagicMock()
    client.event.side_effect = RuntimeError("langfuse down")
    client.score.side_effect = RuntimeError("langfuse down")
    monkeypatch.setattr(tracing, "_client", client)
    # Must not raise — alerting is best-effort.
    breach = latency.check_latency("tool_call", 5000, trace_id="t")
    assert breach is not None
