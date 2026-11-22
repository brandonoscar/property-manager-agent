"""Unit tests for the Langfuse run-tracer (tool/handoff spans).

The tracer only ships data to a live Langfuse, so these tests drive it
with a fake client: assert the right calls are made, that it's a no-op
when off, and that a client failure never propagates into the run.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from helixis.observability import tracing
from helixis.observability.context import TraceContext


def test_no_op_when_client_unset(monkeypatch):
    monkeypatch.setattr(tracing, "_client", None)
    assert tracing.is_active() is False
    # None of these raise or do anything when the tracer is off.
    tracing.start_trace(trace_id="t", name="n", company_id="c", session_id="s")
    tracing.record_tool_span(trace_id="t", name="buildium_create_work_order", output="ok")
    tracing.record_handoff_event(trace_id="t", from_agent="Helixis", to_agent="buildium_agent")
    tracing.flush()


def test_new_trace_id_is_unique():
    assert tracing.new_trace_id() != tracing.new_trace_id()


def test_records_spans_and_events_on_active_client(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(tracing, "_client", client)
    assert tracing.is_active() is True

    tracing.start_trace(
        trace_id="trace-1",
        name="helixis-agent",
        company_id="co",
        session_id="se",
        tags=("orchestrator",),
        user_query="cancel the work order",
    )
    client.trace.assert_called_once()
    assert client.trace.call_args.kwargs["id"] == "trace-1"
    assert client.trace.call_args.kwargs["user_id"] == "co"

    tracing.record_tool_span(
        trace_id="trace-1", name="buildium_create_work_order", output="Created #9001"
    )
    client.span.assert_called_once()
    assert client.span.call_args.kwargs["trace_id"] == "trace-1"
    assert client.span.call_args.kwargs["name"] == "tool:buildium_create_work_order"

    tracing.record_handoff_event(
        trace_id="trace-1", from_agent="Helixis", to_agent="buildium_agent"
    )
    client.event.assert_called_once()
    assert client.event.call_args.kwargs["input"] == {"from": "Helixis", "to": "buildium_agent"}


def test_empty_trace_id_skips_emission(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(tracing, "_client", client)
    tracing.record_tool_span(trace_id="", name="x", output="y")
    client.span.assert_not_called()


def test_client_failure_never_propagates(monkeypatch):
    client = MagicMock()
    client.span.side_effect = RuntimeError("langfuse down")
    client.event.side_effect = RuntimeError("langfuse down")
    client.trace.side_effect = RuntimeError("langfuse down")
    monkeypatch.setattr(tracing, "_client", client)
    # Must not raise — observability is best-effort.
    tracing.start_trace(trace_id="t", name="n", company_id="c", session_id="s")
    tracing.record_tool_span(trace_id="t", name="n", output="o")
    tracing.record_handoff_event(trace_id="t", from_agent="a", to_agent="b")


def test_long_output_is_truncated(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(tracing, "_client", client)
    tracing.record_tool_span(trace_id="t", name="n", output="x" * 5000)
    sent = client.span.call_args.kwargs["output"]
    assert len(sent) <= 2001 and sent.endswith("…")


def test_configure_tracer_off_when_keys_blank(monkeypatch):
    from helixis.settings import Settings

    settings = Settings(env="test", memory={"enabled": False})
    # default langfuse keys are blank ⇒ tracer disabled
    assert tracing.configure_tracer(settings) is False
    assert tracing.is_active() is False


# ── trace_id propagation into LiteLLM metadata ──────────────────


def test_trace_context_metadata_includes_trace_id():
    md = TraceContext(company_id="c", session_id="s", trace_id="trace-9").to_metadata()
    assert md["trace_id"] == "trace-9"
    assert md["existing_trace_id"] == "trace-9"


def test_trace_context_metadata_omits_blank_trace_id():
    md = TraceContext(company_id="c", session_id="s").to_metadata()
    assert "trace_id" not in md
    assert "existing_trace_id" not in md
