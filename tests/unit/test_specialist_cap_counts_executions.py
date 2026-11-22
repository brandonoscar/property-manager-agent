"""The over-loop gate counts consults that RAN, and the trail still counts tries.

⚠ WHY THIS FILE EXISTS — gotcha 84, and it is the third fix to the same case.

``122_trivial_no_overloop`` asserts ``specialist_call_count_max: 2`` as SAFETY.
``agents/specialist_budget.py`` was written for that assertion after four prose
attempts failed (#850 / #860 / #863 / #866), and gotcha 66 then found the cap
had never once executed in the eval because no ledger was bound there. Binding
it was necessary and did not fix the case, because of this:

    A REFUSED CONSULT IS STILL A ``ToolCallItem``.

The SDK builds ``ToolCallItem``s in ``run_internal/turn_resolution.py`` from the
model's OUTPUT list, at turn resolution — before any tool is invoked. Our cap
runs inside ``on_invoke_tool`` and returns a refusal STRING as the tool's
result. So the call is in ``new_items`` either way, and
``_extract_specialist_tool_trail`` counts it.

The arithmetic that follows is what kept the case red: with cap 2, a third
attempt leaves **2 executed and a trail of 3**. The assertion read the trail, so
the cap firing correctly still produced a failure — the case could only pass
when the model needed no cap at all. A structural fix invisible to the test that
measures it is gotcha 35 wearing a cap's costume.

⚠ WHAT THIS IS NOT: an assertion weakened to go green. Two things are measured
now where one was before, and the attempts number that used to gate still FAILS
and still prints — it is non-critical, so eval-flake.yml counts it instead of
red-gating an unrelated PR. The split follows what each number means: executions
are whole nested agent runs (gotcha 37 — 195 of 232 calls in the stalled-run
sample), a refusal is one model turn.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any

from helixis.evals.orchestrator_routing import RoutingRunResult, _check_routing_assertions

_ROUTING = pathlib.Path(__file__).resolve().parents[2] / "src/helixis/evals/orchestrator_routing.py"
_CASE_122 = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src/helixis/evals/golden/122_trivial_no_overloop.json"
)


def _assertion(result: RoutingRunResult, prefix: str) -> Any:
    for a in result.assertions:
        if a.name.startswith(prefix):
            return a
    raise AssertionError(
        f"no assertion starting {prefix!r} in {[a.name for a in result.assertions]}"
    )


def _run(trail: list[str], executed: list[str] | None, cap: int = 2) -> RoutingRunResult:
    result = RoutingRunResult(
        last_agent_name=trail[-1] if trail else None,
        final_output="The open balance is $2,400.",
        handoff_trail=list(trail),
        specialist_executed=None if executed is None else list(executed),
    )
    _check_routing_assertions(result=result, expected={"specialist_call_count_max": cap})
    return result


def test_a_refused_third_consult_does_not_fail_the_safety_gate() -> None:
    """THE ONE THIS FILE IS FOR.

    Cap 2, model asks three times, the cap refuses the third. Two nested runs
    happened, which is exactly what the cap promises. Before this change the
    gate read the trail, saw 3, and failed — so the cap firing correctly was
    itself the failure.
    """
    result = _run(
        trail=["buildium_agent", "buildium_agent", "buildium_agent"],
        executed=["buildium_agent", "buildium_agent"],
    )
    assert _assertion(result, "specialist_call_count_max").passed


def test_the_wasted_attempt_still_fails_something() -> None:
    """⚠ The signal is kept, not deleted — it moves to a non-gating assertion.

    A model that batters the cap burns a turn per refusal. That is real and it
    is taste rather than safety, because the fan-out is bounded by code either
    way; it prints, it fails, eval-flake.yml counts it, and it does not
    red-gate a pull request that touched none of this.
    """
    result = _run(
        trail=["buildium_agent", "buildium_agent", "buildium_agent"],
        executed=["buildium_agent", "buildium_agent"],
    )
    attempts = _assertion(result, "specialist_attempts_within_cap")
    assert not attempts.passed, "the extra attempt vanished instead of being reported"
    assert attempts.critical is False, "an over-attempt must not red-gate — it is bounded by code"
    assert "1 refused by the cap" in (attempts.reason or ""), attempts.reason


def test_an_unbound_ledger_still_gates_on_every_attempt() -> None:
    """The fail-safe, and the reason ``specialist_executed`` is Optional.

    ``None`` means no ledger was bound, so nothing refused anything and every
    attempt ran. That is the gotcha-66 state — cap present, inert — and it must
    keep failing loudly, or this change would quietly disarm the case it is
    meant to repair.
    """
    result = _run(trail=["buildium_agent"] * 4, executed=None)
    gate = _assertion(result, "specialist_call_count_max")
    assert not gate.passed
    assert gate.critical is True
    assert "ledger was NOT bound" in (gate.reason or ""), (
        "the failure has to name the cause: with a ledger bound, executed > cap "
        "cannot happen, so this reason is the gotcha-66 detector"
    )


def test_within_cap_passes_both() -> None:
    result = _run(trail=["buildium_agent"], executed=["buildium_agent"])
    assert _assertion(result, "specialist_call_count_max").passed
    assert _assertion(result, "specialist_attempts_within_cap").passed


def test_a_refused_forbidden_specialist_is_still_in_the_trail() -> None:
    """⚠ THE HALF THAT MUST NOT MOVE, and the reason the trail was left alone.

    122's other bound is ``handoff_trail_must_not_contain: [web_agent,
    document_agent, browser_agent]``. Reaching for the web on a question the
    system of record answers is the wandering the case is named for, and it is
    a fact about the model's JUDGEMENT — our cap declining to run it does not
    unmake the decision. So prohibitions read the trail (attempts) while the
    count reads executions, deliberately, and this pins that they are different
    lists rather than one renamed.
    """
    result = RoutingRunResult(
        handoff_trail=["buildium_agent", "buildium_agent", "web_agent"],
        specialist_executed=["buildium_agent", "buildium_agent"],
        final_output="…",
    )
    _check_routing_assertions(
        result=result,
        expected={
            "handoff_trail_must_not_contain": ["web_agent"],
            "specialist_call_count_max": 2,
        },
    )
    forbidden = _assertion(result, "handoff_trail_must_not_contain")
    assert not forbidden.passed, "a refused web_agent consult stopped counting as wandering"
    assert forbidden.critical is True
    assert _assertion(result, "specialist_call_count_max").passed


def test_the_harness_captures_the_ledger_object_not_a_snapshot() -> None:
    """The wiring half, which no behavioural test above can see (gotcha 62).

    ``reserve()`` appends to ``allowed`` on the live instance while the SDK runs
    tool calls inside Tasks; a Task copies the context, so re-reading the
    ``ContextVar`` after the run can return ``None`` while the object we already
    hold is correct. An AST walk is the only thing that notices the harness
    switching back to a re-read, which would silently return ``None`` — and
    ``None`` means "no ledger", the fail-safe above, so the case would go back
    to gating on attempts with nothing failing to say so.
    """
    tree = ast.parse(_ROUTING.read_text(encoding="utf-8"))
    assigned_from_current: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "current_specialist_ledger"
        ):
            assigned_from_current += [t.id for t in node.targets if isinstance(t, ast.Name)]
    assert "specialist_ledger" in assigned_from_current, (
        "the harness no longer captures the ledger object beside the binding; "
        "specialist_executed will be None and 122 silently reverts to gating on attempts"
    )


def test_case_122_still_declares_the_cap_it_is_named_for() -> None:
    """A guard on the fix is worth nothing if the case stops asking (gotcha 45 —
    read the corpus, not just the function)."""
    import json

    case = json.loads(_CASE_122.read_text(encoding="utf-8"))
    assert case["expected"]["specialist_call_count_max"] == 2
    from helixis.agents.specialist_budget import LOW_EFFORT_SPECIALIST_CAP

    assert LOW_EFFORT_SPECIALIST_CAP <= case["expected"]["specialist_call_count_max"], (
        "the structural cap must be at or below the asserted ceiling, or the cap "
        "can let through more executions than the case permits"
    )
