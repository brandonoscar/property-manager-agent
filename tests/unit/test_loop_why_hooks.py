"""Reasoning-loop "why" hooks (W4 task #24) — dogfood-prep step 1.

Pins the four captures: derived depth + stop reason in the turn summary,
ordered specialist trail with per-sub-run latency + token attribution (the
SpendTracker contextvar mechanism), and footer-compliance classification.
Plus the wiring: loop-mode tools are instrumented, the handoff path pays
nothing, and the summary is emitted at every orchestrator exit path.
"""

from __future__ import annotations

import pytest
from agents import FunctionTool

from helixis.agents.orchestrator import SPECIALIST_AGENT_NAMES, create_orchestrator
from helixis.observability.loop_hooks import (
    LoopStats,
    LoopTokenAttributor,
    bind_loop_stats,
    classify_footer,
    instrument_specialist_tool,
    reset_loop_stats,
    turn_summary_payload,
)
from helixis.settings import Settings

_FOOTERED = (
    "Bob owes $2,400.\n\n"
    "Sources: buildium: $2,400 past due\n"
    "Confidence: likely — no payment posted\n"
    "Unverified: none\n"
)


def _tool(invoke, name: str = "gmail_agent") -> FunctionTool:
    return FunctionTool(
        name=name,
        description="d",
        params_json_schema={"type": "object", "properties": {}},
        on_invoke_tool=invoke,
        strict_json_schema=False,
    )


def _usage(total: int):
    return {"usage": {"total_tokens": total}}


# ── footer classification ────────────────────────────────────────


def test_classify_footer_three_way() -> None:
    assert classify_footer(_FOOTERED) == "emitted"
    assert classify_footer("Sources: x\nplain text, no other lines") == "malformed"
    assert classify_footer("just an answer with no footer at all") == "missing"


# ── instrumentation + token attribution ──────────────────────────


@pytest.mark.asyncio
async def test_invoke_records_ordered_call_with_latency() -> None:
    async def ok(ctx, payload):
        return "result"

    stats = LoopStats()
    token = bind_loop_stats(stats)
    try:
        wrapped = instrument_specialist_tool(_tool(ok))
        assert await wrapped.on_invoke_tool(None, "{}") == "result"
    finally:
        reset_loop_stats(token)

    assert [c.name for c in stats.calls] == ["gmail_agent"]
    assert stats.calls[0].duration_ms is not None  # closed, not in flight


@pytest.mark.asyncio
async def test_tokens_attribute_to_the_inflight_call_else_orchestrator() -> None:
    attributor = LoopTokenAttributor()

    async def sub_run(ctx, payload):
        # An LLM completion INSIDE the specialist sub-run — the wrapped invoke
        # labelled this context, so tokens land on the call.
        await attributor.async_log_success_event({}, _usage(120), None, None)
        return "ok"

    stats = LoopStats()
    token = bind_loop_stats(stats)
    try:
        wrapped = instrument_specialist_tool(_tool(sub_run, name="buildium_agent"))
        await wrapped.on_invoke_tool(None, "{}")
        # A completion OUTSIDE any specialist invoke — the orchestrator's own.
        await attributor.async_log_success_event({}, _usage(30), None, None)
    finally:
        reset_loop_stats(token)

    assert stats.calls[0].tokens == 120
    assert stats.orchestrator_tokens == 30
    assert stats.total_tokens == 150


@pytest.mark.asyncio
async def test_unbound_stats_make_everything_a_noop() -> None:
    ran = []

    async def ok(ctx, payload):
        ran.append(1)
        return "ok"

    wrapped = instrument_specialist_tool(_tool(ok))
    assert await wrapped.on_invoke_tool(None, "{}") == "ok"  # passthrough
    await LoopTokenAttributor().async_log_success_event({}, _usage(50), None, None)
    assert ran == [1]  # nothing recorded anywhere, nothing raised


@pytest.mark.asyncio
async def test_exceptions_propagate_unchanged_and_call_still_closes() -> None:
    async def boom(ctx, payload):
        raise RuntimeError("specialist died")

    stats = LoopStats()
    token = bind_loop_stats(stats)
    try:
        wrapped = instrument_specialist_tool(_tool(boom))
        with pytest.raises(RuntimeError, match="specialist died"):
            await wrapped.on_invoke_tool(None, "{}")
    finally:
        reset_loop_stats(token)
    # The fenced failure_error_function depends on the exception reaching the
    # SDK; the call record must still close so the trail stays truthful.
    assert stats.calls[0].duration_ms is not None


@pytest.mark.asyncio
async def test_attributor_never_raises_on_garbage() -> None:
    stats = LoopStats()
    token = bind_loop_stats(stats)
    try:
        await LoopTokenAttributor().async_log_success_event({}, object(), None, None)
        await LoopTokenAttributor().async_log_success_event(
            {}, {"usage": {"total_tokens": "not-a-number"}}, None, None
        )
    finally:
        reset_loop_stats(token)
    assert stats.total_tokens == 0


# ── the turn summary ─────────────────────────────────────────────


def test_summary_derives_depth_from_the_trail() -> None:
    # Baseline round-1 relabel: descriptive none/single_source/multi_source
    # by distinct gather calls (step-zero recall excluded) — the old binary
    # carried no information beyond the counts.
    recall_only = LoopStats()
    recall_only.open_call("check_known_state").duration_ms = 5
    s = turn_summary_payload(
        recall_only, footer_status="missing", specialist_names=SPECIALIST_AGENT_NAMES
    )
    assert s["depth"] == "none"
    assert s["known_state_checked"] is True
    assert s["specialist_count"] == 0

    single = LoopStats()
    single.open_call("buildium_agent").tokens = 27_000
    one = turn_summary_payload(
        single, footer_status="emitted", specialist_names=SPECIALIST_AGENT_NAMES
    )
    assert one["depth"] == "single_source"

    multi = LoopStats()
    multi.open_call("buildium_agent").tokens = 400
    multi.open_call("gmail_agent")
    d = turn_summary_payload(
        multi, footer_status="emitted", specialist_names=SPECIALIST_AGENT_NAMES
    )
    assert d["depth"] == "multi_source"
    assert d["specialist_count"] == 2
    assert [c["name"] for c in d["specialist_calls"]] == ["buildium_agent", "gmail_agent"]
    assert d["stop_reason"] == "completed"


def test_summary_carries_stop_reason_and_latency() -> None:
    stats = LoopStats()
    stats.stop_reason = "wall_clock_deadline"
    payload = turn_summary_payload(
        stats,
        footer_status="missing",
        specialist_names=SPECIALIST_AGENT_NAMES,
        latency_ms=180_000,
    )
    assert payload["stop_reason"] == "wall_clock_deadline"
    assert payload["latency_ms"] == 180_000


# ── wiring ───────────────────────────────────────────────────────


def _settings(*, reasoning_loop: bool) -> Settings:
    return Settings(
        env="test",
        llm={"default_model": "openai/gpt-4o", "openai_api_key": "k"},
        memory={"enabled": False},
        agent={"reasoning_loop": reasoning_loop},
    )


def test_loop_mode_tools_are_instrumented_handoff_mode_untouched() -> None:
    loop_tools = create_orchestrator(_settings(reasoning_loop=True)).tools
    instrumented = {t.name for t in loop_tools if getattr(t, "_loop_instrumented", False)}
    # Every specialist tool AND the known-state channel carry the marker…
    assert SPECIALIST_AGENT_NAMES <= instrumented
    assert "check_known_state" in instrumented
    # …while plain base tools (memory, table) stay unwrapped.
    assert "search_memory" not in instrumented

    # Live handoff path: check_known_state (added 2026-08-17, instrumented so
    # the trail shows step-zero recall there too) is the ONLY wrapped tool —
    # specialists stay handoffs, base tools stay unwrapped.
    handoff_tools = create_orchestrator(_settings(reasoning_loop=False)).tools
    live_instrumented = {t.name for t in handoff_tools if getattr(t, "_loop_instrumented", False)}
    assert live_instrumented == {"check_known_state"}


def test_summary_emitted_at_every_exit_path() -> None:
    # Source-level pin (run_agent_streaming can't be driven end-to-end in a
    # unit test — the fence-chokepoint precedent): the summary fires on the
    # success path and on every failure handler, each with its stop reason.
    import inspect

    from helixis.agents import orchestrator

    src = inspect.getsource(orchestrator.run_agent_streaming)
    assert src.count("_emit_loop_turn_summary(") >= 5  # def + 4 call sites
    for reason in ("llm_transient_error", "wall_clock_deadline", "stall", "max_turns"):
        assert reason in src
    assert "reasoning_loop.turn_summary" in src
    assert "reasoning_loop.footer_compliant" in src
    # Stats are bound only when the loop actually runs — handoff turns pay
    # nothing. The gate reads the per-question ``loop_mode`` rather than the
    # deployment flag (``loop_mode_for``), so a recognised one-fact lookup on a
    # loop-enabled deployment binds nothing either. Measuring a loop that did
    # not run would put zeros into the turn-summary series and make the
    # dashboard read as a regression.
    bind_at = src.index("bind_loop_stats(")
    gate_at = src.rindex("if loop_mode:", 0, bind_at)
    assert gate_at < bind_at
