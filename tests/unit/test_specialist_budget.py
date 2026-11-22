"""The structural cap on specialist fan-out for one-fact questions.

``122_trivial_no_overloop`` is a SAFETY case — ``specialist_call_count_max``
is built ``critical=True``, so it gates every roll and cannot be re-rolled.
It has now survived four attempts to fix it with prose (#850 docstrings, #860
result footer, #863 TRIAGE step, #866 refusal escalation); the TRIAGE step was
merged nine hours before the nightly that recorded it failing 2 of 3 runs.

These pin the two things that make a cap a cap rather than another sentence:
it binds only on questions we're confident are trivial, and it reserves its
slot BEFORE awaiting (gotcha 25 — the SDK runs a turn's tool calls
concurrently, and check-then-act around an await caps nothing).
"""

from __future__ import annotations

import asyncio
from typing import Any

from helixis.agents.specialist_budget import (
    LOW_EFFORT_SPECIALIST_CAP,
    SpecialistLedger,
    bind_specialist_ledger,
    cap_specialist_tool,
    current_specialist_ledger,
    reset_specialist_ledger,
)

_TRIVIAL = "What's the current open balance for the tenant in unit 4B?"
_COMPLEX = (
    "Compare last quarter's maintenance spend against the budget, then tell me "
    "which vendors we should renegotiate with and draft the emails."
)


class _FakeTool:
    """Minimal stand-in for a specialist FunctionTool."""

    def __init__(self, name: str = "buildium_agent") -> None:
        self.name = name
        self.description = "d"
        self.params_json_schema: dict[str, Any] = {}
        self.strict_json_schema = True
        self.calls = 0

    async def on_invoke_tool(self, ctx: Any, payload: str) -> str:
        self.calls += 1
        await asyncio.sleep(0)  # a real specialist awaits; the cap must not rely on not doing so
        return f"consulted {self.name}"


# ── it binds only where we're confident ───────────────────────────────────


def test_a_trivial_question_gets_a_ledger() -> None:
    token = bind_specialist_ledger(_TRIVIAL)
    try:
        assert current_specialist_ledger() is not None
    finally:
        reset_specialist_ledger(token)


def test_a_real_question_is_left_completely_alone() -> None:
    """The safe default, and the reason this ships without a tuning pass: a
    question we don't positively recognise as trivial behaves exactly as it
    does today, cap absent entirely."""
    token = bind_specialist_ledger(_COMPLEX)
    try:
        assert current_specialist_ledger() is None
    finally:
        reset_specialist_ledger(token)


def test_the_feature_flag_disables_it_entirely() -> None:
    token = bind_specialist_ledger(_TRIVIAL, enabled=False)
    try:
        assert current_specialist_ledger() is None
    finally:
        reset_specialist_ledger(token)


# ── the cap actually binds ────────────────────────────────────────────────


def test_consults_past_the_cap_are_refused() -> None:
    ledger = SpecialistLedger()
    for _ in range(LOW_EFFORT_SPECIALIST_CAP):
        assert ledger.reserve("buildium_agent") is None
    refusal = ledger.reserve("web_agent")
    assert refusal is not None
    assert "STOP CONSULTING" in refusal
    assert ledger.used == LOW_EFFORT_SPECIALIST_CAP


def test_the_cap_matches_what_case_122_asserts() -> None:
    """If these drift apart the case keeps failing while the cap reports
    success — the fix and its test would be measuring different things."""
    import json
    import pathlib

    case = json.loads(
        (
            pathlib.Path(__file__).parents[2]
            / "src/helixis/evals/golden/122_trivial_no_overloop.json"
        ).read_text()
    )
    assert case["expected"]["specialist_call_count_max"] == LOW_EFFORT_SPECIALIST_CAP


def test_the_refusal_tells_the_model_to_answer_not_to_try_elsewhere() -> None:
    """Battery v4's lesson: a refusal that only says "no" costs a model turn
    and buys nothing (gotcha 32). It has to name the next action."""
    ledger = SpecialistLedger(cap=0)
    refusal = ledger.reserve("web_agent") or ""
    assert "ANSWER" in refusal
    assert "cannot answer" in refusal  # the honest out, so it isn't forced to invent


# ── the concurrency property, which is the whole point ────────────────────


def test_parallel_consults_in_one_turn_cannot_all_pass() -> None:
    """THE test. The SDK dispatches a turn's tool calls concurrently, so ten
    of them can read the ledger before any of them finishes. ``call_budget``'s
    first cut failed exactly here — every parallel call saw an empty ledger
    and executed (gotcha 25). Reserving before the await is what fixes it.
    """

    async def go() -> tuple[int, int]:
        tool = _FakeTool()
        capped = cap_specialist_tool(tool)
        token = bind_specialist_ledger(_TRIVIAL)
        try:
            results = await asyncio.gather(*(capped.on_invoke_tool(None, "{}") for _ in range(10)))
            refused = sum(1 for r in results if "STOP CONSULTING" in str(r))
            return tool.calls, refused
        finally:
            reset_specialist_ledger(token)

    executed, refused = asyncio.run(go())
    assert executed == LOW_EFFORT_SPECIALIST_CAP, f"cap did not bind: {executed} executed"
    assert refused == 10 - LOW_EFFORT_SPECIALIST_CAP


def test_an_unbound_ledger_never_refuses_anything() -> None:
    """No ledger = no cap. A non-trivial question must reach every specialist
    it wants, exactly as today."""

    async def go() -> int:
        tool = _FakeTool()
        capped = cap_specialist_tool(tool)
        token = bind_specialist_ledger(_COMPLEX)
        try:
            await asyncio.gather(*(capped.on_invoke_tool(None, "{}") for _ in range(10)))
            return tool.calls
        finally:
            reset_specialist_ledger(token)

    assert asyncio.run(go()) == 10


# ── wiring + hygiene ──────────────────────────────────────────────────────


def test_the_orchestrator_wraps_its_specialist_tools() -> None:
    """A cap nobody applied is not a cap. Pins that the loop branch actually
    composes ``cap_specialist_tool`` around the instrumented specialist."""
    import inspect

    from helixis.agents import orchestrator

    src = inspect.getsource(orchestrator)
    assert "cap_specialist_tool(" in src
    assert "bind_specialist_ledger(" in src
    assert "reset_specialist_ledger(" in src


def test_a_non_function_tool_passes_through_untouched() -> None:
    class _Sentinel:
        name = "x"

    sentinel = _Sentinel()
    assert cap_specialist_tool(sentinel) is sentinel


def test_the_summary_reports_what_happened() -> None:
    """Measured, not inferred — battery v4 could only diagnose the read loop
    because the budget reported itself."""
    ledger = SpecialistLedger()
    ledger.reserve("buildium_agent")
    ledger.reserve("web_agent")
    ledger.reserve("document_agent")
    s = ledger.summary()
    assert s["specialist_calls"] == 2
    assert s["trail"] == ["buildium_agent", "web_agent"]
    assert s["refused"] == {"document_agent": 1}
    assert s["hit_cap"] is True
