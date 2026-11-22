"""The multi-turn input a golden case declares, and the two harnesses using it.

Measured 2026-09-06: **0 of 153 golden cases exercised more than one turn**.
Neither harness had a key for prior turns, so the entire conversational-state
class was untestable by construction — which is why the demo take's follow-up
questions failed with nothing in the suite to catch them.

⚠ Both sides are read (gotcha 28). The builder is exercised directly, AND an
AST guard asserts each harness actually passes its result to `Runner.run` —
a behavioural test of the builder cannot see a harness that keeps handing the
bare string (gotcha 62's rule; gotcha 66 is the same defect one level up,
where a binding existed in production and never reached the harness that
measured it).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from helixis.evals.case_history import HistoryError, build_run_input

_EVALS = Path(__file__).resolve().parents[2] / "src" / "helixis" / "evals"

_HISTORY = [
    {"role": "user", "content": "Which tenants are behind on rent?"},
    {"role": "assistant", "content": "Six leases are past due at 200 E Colfax and five others."},
]


def test_no_history_returns_the_bare_string() -> None:
    """The 153 existing cases must be byte-for-byte unaffected.

    A change to the eval harness that alters what EVERY case sends is a
    change to every case's meaning — the new key has to be inert when absent.
    """
    assert build_run_input("hello", None) == "hello"
    assert build_run_input("hello", []) == "hello"


def test_the_current_turn_is_appended_as_the_last_user_item() -> None:
    items = build_run_input("Which of those have open maintenance?", _HISTORY)

    assert isinstance(items, list)
    assert [i["role"] for i in items] == ["user", "assistant", "user"]
    assert items[-1]["content"] == "Which of those have open maintenance?"
    # The prior turns survive verbatim — a history the harness paraphrased
    # would measure a conversation the product never had.
    assert items[0]["content"] == _HISTORY[0]["content"]
    assert items[1]["content"] == _HISTORY[1]["content"]


def test_a_history_ending_on_a_user_turn_is_refused() -> None:
    """Two user messages back to back is not a shape any thread produces.

    Accepting it would let a case measure a conversation that cannot happen,
    and then read the model's confusion as a product defect.
    """
    with pytest.raises(HistoryError, match="end with an assistant turn"):
        build_run_input("next", [{"role": "user", "content": "first"}])


def test_a_history_starting_on_an_assistant_turn_is_refused() -> None:
    with pytest.raises(HistoryError, match="start with a user turn"):
        build_run_input("next", [{"role": "assistant", "content": "unprompted"}])


def test_a_tool_role_is_refused_with_the_reason() -> None:
    """The SDK rejects a bare tool item (no matching call id, PR #72), and
    production collapses persisted tool rows into the assistant turn. A case
    author reaching for `role: tool` needs to be told that, not silently
    given a broken run."""
    with pytest.raises(HistoryError, match="assistant turn's own text"):
        build_run_input(
            "next",
            [
                {"role": "user", "content": "q"},
                {"role": "tool", "content": "buildium_list_tenants -> []"},
            ],
        )


@pytest.mark.parametrize(
    "bad",
    [
        "not a list",
        [["role", "user"]],
        [{"role": "user"}],
        [{"role": "user", "content": "   "}],
    ],
)
def test_malformed_history_raises_rather_than_degrading(bad: object) -> None:
    """⚠ The direction that matters. Swallowing a malformed history into "no
    history" runs the case as a SINGLE-turn case — the exact shape it was
    written to replace — and it passes, for the wrong reason."""
    with pytest.raises(HistoryError):
        build_run_input("next", bad)


def _runner_input_arg(source: str) -> list[str]:
    """The expression each `Runner.run(input=...)` call passes, as source."""
    tree = ast.parse(source)
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "run":
            continue
        for kw in node.keywords:
            if kw.arg == "input":
                out.append(ast.unparse(kw.value))
    return out


@pytest.mark.parametrize("module", ["orchestrator_routing.py", "agent_flow.py"])
def test_the_harness_actually_passes_the_built_input(module: str) -> None:
    """AST guard on the CALL SITE.

    Wiring a builder and then not using it is this repo's most repeated
    defect — `comms_triage` complete and called by nothing (gotcha 62),
    `cap_specialist_tool` bound only in production (gotcha 66). Every test
    above would stay green against a harness that kept sending the bare
    string.
    """
    source = (_EVALS / module).read_text(encoding="utf-8")

    calls = _runner_input_arg(source)
    assert calls, f"{module} no longer calls Runner.run(input=...)"
    assert "run_input" in calls, (
        f"{module} passes {calls} to Runner.run — not the value `build_run_input` "
        "produced, so `input.history` would be silently ignored"
    )

    built = [
        n
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "build_run_input"
    ]
    assert built, f"{module} no longer builds its run input from the case history"
