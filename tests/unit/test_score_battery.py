"""Battery scorer — the deterministic half must be exactly right.

The whole point of the fact pass is that it is NOT a judgment call: the
seed is deterministic, so a balance is either the seeded number or it is
wrong. These pin the derivation (never hand-typed), the number matching
(formatting must not count as a factual error), and the judge context.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sb = _load("score_battery", _ROOT / "scripts" / "score_battery.py")
seed = _load("seed_states_for_score", _ROOT / "scripts" / "seed_states.py")
GT = sb.build_ground_truth(seed)


def test_ground_truth_matches_the_seeded_ledger() -> None:
    # Verified against the live account 2026-08-20: the agent's own report
    # listed exactly these ten 61-90 day accounts and this count.
    assert GT.properties == 50
    assert len(GT.overdue) == 10
    assert len(GT.partial) == 10
    assert GT.delinquent_accounts == 20
    assert GT.overdue_total == 30_700
    assert GT.partial_total == 5_640
    assert GT.delinquent_total == 36_340
    assert GT.renewal_window == 10
    # Sorted largest-first, so "the biggest debtor" is index 0.
    assert GT.overdue[0][1] == "Keanu Alameda"
    assert GT.overdue[0][2] == 5_200


def test_number_formatting_is_never_a_factual_error() -> None:
    """ "$36,340" and "36340" are the same fact — only a different NUMBER is
    a miss."""
    exp = sb.expectations(GT)[3]
    for spelling in (
        "we're owed $36,340 across 20 accounts, led by Keanu Alameda",
        "total 36340 over 20 accounts — Keanu Alameda is the largest",
    ):
        results = sb.check_facts(spelling, exp)
        assert all(ok for _label, ok, _d in results), spelling


def test_a_wrong_number_fails_and_says_what_was_expected() -> None:
    exp = sb.expectations(GT)[3]
    results = sb.check_facts(
        "You're owed about $34,000 across 20 accounts, led by Keanu Alameda.", exp
    )
    failed = [(label, detail) for label, ok, detail in results if not ok]
    assert len(failed) == 1
    assert failed[0][0] == "delinquent total"
    assert "36,340" in failed[0][1]  # the expectation is printed, not just "failed"


def test_questions_without_a_computable_answer_carry_a_trap_instead() -> None:
    exp = sb.expectations(GT)
    # The single-unit Phoenix question can't be checked arithmetically; it
    # rides on the judge, so it must carry the trap description.
    assert not exp[8].facts
    assert "single-unit" in exp[8].trap.lower()
    assert sb.check_facts("anything at all", exp[8]) == []  # vacuously passes, by design
    # Every trapped question describes what a strong reply catches.
    for number in (5, 7, 8, 9, 10, 11):
        assert len(exp[number].trap) > 60, number


def test_judge_context_states_the_authoritative_numbers() -> None:
    """Groundedness scoring is meaningless unless the judge can see what the
    data actually says."""
    context = GT.as_context()
    assert "36,340" in context
    assert "Keanu Alameda" in context
    assert "20 delinquent accounts" in context
    assert "50 properties" in context


def test_scored_rows_report_partial_fact_failure() -> None:
    row = sb.Scored(
        number=3,
        question="q",
        answer="a",
        seconds=1.0,
        completed=True,
        facts=[("a", True, ""), ("b", False, "expected X")],
        trap="",
    )
    assert row.facts_passed is False
    assert sb.Scored(1, "q", "a", 1.0, True, [], "").facts_passed is True


def test_the_context_states_what_it_does_not_cover() -> None:
    """Battery v3 scored two CORRECT payables answers at 0.00 groundedness —
    "the context contains no vendor/bill data, every figure is fabricated" —
    against a $26,000 total that was verifiably right. A partial extract that
    doesn't announce itself as partial turns silence into an accusation."""
    context = GT.as_context()
    assert "SCOPE" in context
    assert "vendor bills" in context
    assert "contradicts" in context


# ── absent is not wrong (battery v6, 2026-08-27) ─────────────────────────
#
# The v6 scorecard marked all twelve questions ``done ✅`` while five had
# returned zero characters, then counted their unmatched facts as errors:
# "Q3 — 3 wrong". Q3 was not three wrong answers. It was one absent answer
# counted three times — and, as the backend rows later showed, the run had
# actually produced the correct figures; the harness lost them.
#
# Two different defects meet here, and this file pins the scorer's half: a
# blank run must never be rendered as a factual failure. Absent and wrong
# have different causes and different fixes, and collapsing them makes the
# report accuse the model of errors it did not make.


def _scored(**kw):
    base = dict(number=1, question="q", answer="", seconds=180.0, completed=True, facts=[], trap="")
    base.update(kw)
    return sb.Scored(**base)


def test_a_blank_run_is_not_an_answer_however_the_stream_ended() -> None:
    """``completed`` describes the STREAM terminating, not an answer existing.
    v6 had completed=True alongside stop_reason=wall_clock_deadline and zero
    characters on all five."""
    assert _scored(answer="", completed=True).answered is False
    assert _scored(answer="   \n ", completed=True).answered is False
    assert _scored(answer="a real answer", completed=True).answered is True


def test_a_blank_run_is_reported_absent_not_wrong() -> None:
    blank = _scored(
        number=3,
        answer="",
        completed=True,
        facts=[("delinquent total", False, "not found"), ("account count", False, "not found")],
    )
    report = sb.render_report([blank], GT)
    assert "no answer" in report.lower(), "a blank run must be named as blank"
    assert "2 wrong" not in report, (
        "unmatched facts on an EMPTY answer were rendered as factual errors — "
        "this is the v6 defect, where one absent answer read as N wrong ones"
    )
    assert "NO ANSWER" in report, "the report needs a section the reader cannot miss"


def test_a_real_factual_miss_is_still_reported_as_wrong() -> None:
    """⚠ The fix must not launder genuine errors into 'absent'. A run that
    ANSWERED and got the number wrong is exactly what this scorer is for."""
    miss = _scored(
        number=4,
        answer="You have 12 properties.",
        completed=True,
        facts=[("property count", False, "expected 50")],
    )
    report = sb.render_report([miss], GT)
    assert "1 wrong" in report
    assert "Factual misses" in report


def test_a_rescued_answer_is_marked_as_such() -> None:
    """Counting a deadline synthesis as a clean pass is the opposite lie to
    v6's. It is a real answer and it is not a full one."""
    rescued = _scored(number=5, answer="synthesised text", completed=True, partial=True)
    report = sb.render_report([rescued], GT)
    assert "rescued" in report


def test_a_blank_answer_names_why_it_was_blank() -> None:
    """⚠ v7 and v8 each reported "0 chars" on two questions, and the reason
    lived only in a Render log line at a timestamp. So the scorecard said WHAT
    happened and nothing about WHY — and the why is the only actionable half.

    The three states need different fixes and must read differently:
      no synthesis key   -> the rescue never ran
      cause=ok           -> it produced text that never reached the client
      cause=<failure>    -> it ran and the provider gave nothing back
    """
    never_ran = _scored(number=5, answer="", completed=True)
    ran_empty = _scored(
        number=7,
        answer="",
        completed=True,
        synthesis={"cause": "empty_completion", "finish_reason": "length"},
    )
    lost_in_transit = _scored(number=9, answer="", completed=True, synthesis={"cause": "ok"})

    report = sb.render_report([never_ran, ran_empty, lost_in_transit], GT)
    assert "never ran" in report
    assert "empty_completion" in report and "finish_reason=length" in report
    assert "did not reach the client" in report
