"""The specialist cap must be BOUND on the path the eval actually runs.

`122_trivial_no_overloop` is a SAFETY case (`specialist_call_count_max: 2` is
`critical=True`, so it gates every roll and is never re-rolled) and it kept
reding main. Gotcha 34 records four failed attempts to fix it in prose
(#850 docstrings, #860 result footer, #863 TRIAGE, #866 refusal escalation)
and then a structural cap — `agents/specialist_budget.py` — built for it
specifically.

**That cap had never once executed in the case that measures it.**

`create_orchestrator` wraps every specialist in `cap_specialist_tool`, and the
eval calls `create_orchestrator`. But the wrapper is a deliberate no-op unless
a ledger is bound::

    ledger = _ledger.get()
    if ledger is None:  # not a trivial question — today's behaviour exactly
        return await _invoke_as_specialist(...)

and `bind_specialist_ledger` lives inside `run_agent_streaming`, which
`evals/orchestrator_routing.py` does not call — it goes `create_orchestrator()`
→ `Runner.run()` directly. Capped tools, no ledger, no cap.

⚠ **Why the existing tests could not see it.** `test_specialist_budget.py`
binds the ledger itself before exercising the cap, so the primitive is well
covered and its one production call site was invisible to them. That is
gotcha 62's rule — *pair every behavioural test of a new helper with an AST
guard on its call site* — and gotcha 35's, *a case that has never run is not a
passing case*. Both halves were individually green the whole time.

So this file asserts the WIRING, not the behaviour: that the eval binds, that
it resets, and that the two paths agree about the flag that gates it. A
behavioural test cannot see "wired to nothing" (the `comms_triage` precedent).
"""

from __future__ import annotations

import ast
import inspect
import pathlib

from helixis.agents import orchestrator as orchestrator_mod
from helixis.agents.specialist_budget import (
    LOW_EFFORT_SPECIALIST_CAP,
    bind_specialist_ledger,
    reset_specialist_ledger,
)
from helixis.evals import orchestrator_routing as routing_mod

_ROUTING_SRC = pathlib.Path(routing_mod.__file__).read_text(encoding="utf-8")


def _calls_named(source: str, name: str) -> list[ast.Call]:
    """Every call to ``name`` in ``source``, found on the AST.

    Not a substring scan: this module's own comments quote
    ``bind_specialist_ledger`` in order to explain the guard, and `ruff format`
    is free to wrap any call across lines (gotchas 40 / 49).
    """
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            ident = getattr(func, "id", None) or getattr(func, "attr", None)
            if ident == name:
                out.append(node)
    return out


def test_the_routing_eval_binds_the_ledger() -> None:
    """The load-bearing assertion. Without this call the cap is inert and 122
    measures unbudgeted behaviour — which is what it did until 2026-08-30."""
    assert _calls_named(_ROUTING_SRC, "bind_specialist_ledger"), (
        "evals/orchestrator_routing.py no longer binds the specialist ledger. "
        "cap_specialist_tool is a no-op without one, so 122_trivial_no_overloop "
        "is back to measuring uncapped fan-out and will red-gate as SAFETY."
    )


def test_the_routing_eval_resets_what_it_bound() -> None:
    """A ContextVar leaked across cases would let one case's cap bind during
    another's run — the suite runs many cases in one process."""
    assert _calls_named(_ROUTING_SRC, "reset_specialist_ledger")


def test_production_still_binds_it_too() -> None:
    """Reads BOTH sides (gotcha 28). If the production call site is deleted,
    mirroring it in the eval would be testing fiction."""
    prod = inspect.getsource(orchestrator_mod)
    assert _calls_named(prod, "bind_specialist_ledger"), (
        "run_agent_streaming no longer binds the ledger — the eval would now be "
        "asserting a cap production does not apply"
    )


def test_both_sides_gate_on_the_same_flag() -> None:
    """⚠ The eval must not engage the cap under conditions production does not.

    Production passes ``enabled=settings.agent.adaptive_effort``. If the eval
    hardcoded ``enabled=True`` it would cap runs that production leaves
    uncapped, and 122 would pass on behaviour no user ever gets — the
    expired-justification shape from gotcha 48, arriving as a false green.
    """
    for source, where in (
        (_ROUTING_SRC, "the eval"),
        (inspect.getsource(orchestrator_mod), "prod"),
    ):
        call = _calls_named(source, "bind_specialist_ledger")[0]
        flags = [kw for kw in call.keywords if kw.arg == "enabled"]
        assert flags, f"{where} binds the ledger without an explicit enabled= gate"
        assert "adaptive_effort" in ast.unparse(flags[0].value), (
            f"{where} gates the specialist cap on something other than "
            "settings.agent.adaptive_effort — the two paths have drifted"
        )


def test_the_cap_matches_the_case_it_was_built_for() -> None:
    """Pins the constant against 122's own assertion so the fix and the thing
    it is measured by cannot drift into measuring different numbers."""
    import json

    case = json.loads(
        (
            pathlib.Path(__file__).resolve().parents[2]
            / "src/helixis/evals/golden/122_trivial_no_overloop.json"
        ).read_text(encoding="utf-8")
    )
    assert case["expected"]["specialist_call_count_max"] == LOW_EFFORT_SPECIALIST_CAP


def test_the_ledger_actually_binds_for_122s_question() -> None:
    """The precondition the whole cap rests on, exercised rather than assumed.

    ⚠ This was the first hypothesis for why 122 kept failing — that
    `classify_effort` did not recognise the question, the way gotcha 30's
    `re.VERBOSE` bug silently unmatched half its input. It was WRONG: the
    classifier returns 'low' here. Kept as a guard because if it ever stops
    doing so, the cap goes quietly inert again and the symptom is identical.
    """
    import json

    from helixis.agents.specialist_budget import _ledger

    case = json.loads(
        (
            pathlib.Path(__file__).resolve().parents[2]
            / "src/helixis/evals/golden/122_trivial_no_overloop.json"
        ).read_text(encoding="utf-8")
    )
    token = bind_specialist_ledger(case["input"]["user_message"])
    try:
        ledger = _ledger.get()
        assert ledger is not None, (
            "classify_effort no longer recognises 122's question as one-fact, so "
            "the ledger is unbound and the cap is inert — 122 will red-gate again"
        )
        assert ledger.cap == LOW_EFFORT_SPECIALIST_CAP
    finally:
        reset_specialist_ledger(token)
