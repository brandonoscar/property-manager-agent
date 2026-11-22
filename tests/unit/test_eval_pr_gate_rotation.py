"""The tier-2 rotation half of the per-PR eval cost gate.

WHAT THIS EXISTS TO PROTECT (measured 2026-09-16, real corpus, 163 cases):

    billed 152 · safety 128 · tier-1 79 · tier-2 49
    merge (no seed)        139 selected   <- the FULL safety set, unchanged
    PR push, slice 12      102 selected   <- 27% fewer, all 79 tier-1 present

Before the narrowing, a PR touching agent or eval code selected **147 of 152
billed cases** — because ``reply_must_not_be_empty`` is carried by 97 of them
and the hint list matched it on the substring ``must_not``. A gate that defers
five cases is a gate in name only, and nothing printed a number that said so
(gotcha 45: a control whose failure mode is "silently permits more").

The rotation can fail in TWO directions and only one of them is loud:

  * permits MORE — the narrowing stops working and spend returns to 147. Costs
    money, nothing breaks, nobody notices.
  * permits LESS — a write-escape or disclosure case slips into tier 2 and
    starts taking turns. THAT is the dangerous one, and it is why the corpus
    guards below assert tier-1 completeness at every slice size rather than
    checking the classifier against hand-written inputs (gotcha 45's rule: the
    function was self-consistent the last time this class of control broke; it
    was pointed at something that no longer existed).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from helixis.evals.runner import (
    _DEFAULT_PR_SLICE,
    _TIER1_KEYS,
    _WRITE_NAME_SEGMENTS,
    _apply_pr_gate,
    _has_safety_assertion,
    _is_disclosure_needle,
    _load_cases,
    _rotating_slice,
    _rotation_slice_size,
    case_makes_billed_call,
    pr_gate_tier1,
    select_pr_gate_cases,
)

CI_YML = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


# --------------------------------------------------------------------------
# corpus fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def corpus() -> list[dict[str, Any]]:
    cases = _load_cases(None)
    # gotchas 49/100/101/114/128 — a scan that finds zero is a claim to check,
    # not a result. A bad glob or a wrong CWD would otherwise make every
    # assertion below vacuously true.
    assert len(cases) > 100, f"only {len(cases)} golden cases loaded — bad glob?"
    return cases


@pytest.fixture(scope="module")
def tier1_ids(corpus: list[dict[str, Any]]) -> set[str]:
    ids = {
        str(c["id"])
        for c in corpus
        if case_makes_billed_call(c) and _has_safety_assertion(c) and pr_gate_tier1(c)
    }
    assert len(ids) > 40, f"only {len(ids)} tier-1 cases — the classifier is broken"
    return ids


def _select(cases, *, seed: str, size: int) -> set[str]:
    return {
        str(c["id"])
        for c in select_pr_gate_cases(
            cases, set(), eval_code_changed=True, rotation_seed=seed, slice_size=size
        )
    }


# --------------------------------------------------------------------------
# the two guarantees
# --------------------------------------------------------------------------


def test_a_merge_runs_every_safety_case(corpus: list[dict[str, Any]]) -> None:
    """The seed IS the switch, and unset means the pre-rotation gate exactly.

    ci.yml leaves HELIXIS_EVAL_PR_ROTATE unset on a push to main, so this is the
    MERGE path. It is the whole reason rotation is honest rather than merely
    cheap: no case reaches main unrun, and what a PR defers is one iteration.
    """
    selected = _select(corpus, seed="", size=_DEFAULT_PR_SLICE)
    every_safety = {
        str(c["id"]) for c in corpus if case_makes_billed_call(c) and _has_safety_assertion(c)
    }
    assert every_safety, "no safety cases in the corpus — the fixture is wrong"
    assert every_safety <= selected, sorted(every_safety - selected)


def test_tier1_is_never_deferred_at_any_slice_size(
    corpus: list[dict[str, Any]], tier1_ids: set[str]
) -> None:
    """A write escaping its gate, an identifier on screen, a fair-housing
    violation, an over-action cap, a REVERSE_REPORT write-guard.

    ``size=0`` is the important one: it is what a repo VARIABLE set to 0 (or a
    typo the fallback declines to rescue) produces, and it must still run tier 1
    rather than quietly deferring the whole safety battery.
    """
    for size in (0, 1, _DEFAULT_PR_SLICE, 500):
        for seed in ("abc1234", "def5678", "0" * 40):
            missing = tier1_ids - _select(corpus, seed=seed, size=size)
            assert not missing, f"slice={size} seed={seed}: {sorted(missing)}"


def test_the_gate_actually_defers_something(corpus: list[dict[str, Any]]) -> None:
    """The canary for "permits more" — a gate that defers nothing is the defect
    this change exists to fix, wearing the fix's name.

    Pinned as a RELATIONSHIP, not a literal (gotcha 96): the measured numbers are
    139 on a merge and 102 on a push, and asserting either would fail on the next
    golden case somebody writes.
    """
    merge = _select(corpus, seed="", size=_DEFAULT_PR_SLICE)
    push = _select(corpus, seed="abc1234", size=_DEFAULT_PR_SLICE)
    assert push < merge, "rotation selected everything a merge does — nothing rotated"
    # A tenth is a floor, not a target: it only has to be big enough that a
    # narrowing which silently stopped working cannot pass.
    assert len(merge) - len(push) >= len(merge) // 10


def test_the_same_commit_reproduces_and_two_commits_differ(
    corpus: list[dict[str, Any]],
) -> None:
    """Re-running a red PR job must cover the same cases, or a flake and a
    regression are indistinguishable. Two different pushes must NOT.
    """
    a1 = _select(corpus, seed="abc1234", size=_DEFAULT_PR_SLICE)
    a2 = _select(corpus, seed="abc1234", size=_DEFAULT_PR_SLICE)
    b = _select(corpus, seed="def5678", size=_DEFAULT_PR_SLICE)
    assert a1 == a2
    assert a1 != b, "two commits picked the same slice — the seed is not read"


# --------------------------------------------------------------------------
# the classifier, both directions (gotchas 38/51)
# --------------------------------------------------------------------------


def test_the_narrowed_keys_do_not_buy_a_live_model_call() -> None:
    assert not _has_safety_assertion({"expected": {"reply_must_not_be_empty": True}})
    assert not _has_safety_assertion({"expected": {"must_not_route_to": ["gmail_agent"]}})
    # ...and the other direction, or a narrowing that swallowed everything would
    # pass the two assertions above perfectly while deferring the whole battery.
    assert _has_safety_assertion({"expected": {"judge_safety_gate": True}})
    assert _has_safety_assertion({"expected": {"handoff_trail_must_not_contain": ["gmail_agent"]}})
    # A case carrying BOTH still runs — the narrowed key is ignored, not poisonous.
    assert _has_safety_assertion(
        {"expected": {"reply_must_not_be_empty": True, "tool_call_count_max": 2}}
    )


def test_write_verbs_match_name_segments_not_substrings() -> None:
    """``text`` as a substring matches any tool whose name contains ``context``.

    That would promote unrelated reads into the never-rotate tier — failing in
    the EXPENSIVE direction, which nothing surfaces (gotcha 40).
    """
    assert "text" in _WRITE_NAME_SEGMENTS  # the distinguishing input needs it
    assert pr_gate_tier1({"expected": {"tool_call_trail_must_not_contain": ["send_text_message"]}})
    assert not pr_gate_tier1(
        {"expected": {"tool_call_trail_must_not_contain": ["buildium_get_context"]}}
    )


def test_a_disclosure_needle_is_digit_dominant() -> None:
    """Same discriminator the verdict side uses (gotcha 65). A record number is a
    disclosure; a sentence that happens to contain a year is not."""
    assert _is_disclosure_needle("99797")
    assert _is_disclosure_needle("555-0142")
    assert not _is_disclosure_needle("you have no work orders")
    assert not _is_disclosure_needle("0 open work orders")  # a CLAIM, not a leak
    assert not _is_disclosure_needle("")


def test_tier1_keys_are_keys_the_hint_list_actually_matches() -> None:
    """A tier-1 key the hints never produce is a licence nobody needs — it reads
    as coverage and can never fire (gotcha 63's stale-exemption shape)."""
    for key in _TIER1_KEYS:
        assert _has_safety_assertion({"expected": {key: ["x"]}}), key


# --------------------------------------------------------------------------
# the slice-size dial
# --------------------------------------------------------------------------


def test_an_unparseable_slice_size_falls_back_rather_than_rotating_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo in the repo VARIABLE must not silently mean "defer all of tier 2
    on every push" — which reads normally in the log and costs coverage."""
    monkeypatch.setenv("HELIXIS_EVAL_PR_SLICE", "twelve")
    assert _rotation_slice_size() == _DEFAULT_PR_SLICE
    monkeypatch.setenv("HELIXIS_EVAL_PR_SLICE", "")
    assert _rotation_slice_size() == _DEFAULT_PR_SLICE
    monkeypatch.delenv("HELIXIS_EVAL_PR_SLICE")
    assert _rotation_slice_size() == _DEFAULT_PR_SLICE
    # A deliberate value is honoured, including the deliberate zero.
    monkeypatch.setenv("HELIXIS_EVAL_PR_SLICE", "4")
    assert _rotation_slice_size() == 4
    monkeypatch.setenv("HELIXIS_EVAL_PR_SLICE", "0")
    assert _rotation_slice_size() == 0
    monkeypatch.setenv("HELIXIS_EVAL_PR_SLICE", "-3")
    assert _rotation_slice_size() == 0


def test_a_zero_slice_takes_none_rather_than_everything() -> None:
    """The dial must be MONOTONIC. The first cut read ``size <= 0`` as "no cap"
    and returned every rotatable case, so the tightest-looking setting an
    operator can type delivered the loosest behaviour — and it was invisible,
    because the log still printed ``slice=0`` and the run still passed. Found by
    a test failing for a reason nobody predicted, not by review.
    """
    rotatable = [{"id": f"c{i}"} for i in range(10)]
    assert _rotating_slice(rotatable, seed="s", size=0) == []
    assert _rotating_slice(rotatable, seed="s", size=-1) == []
    # ...and the other direction, or a function that returned [] unconditionally
    # would pass the two assertions above and silently delete tier 2 entirely.
    assert len(_rotating_slice(rotatable, seed="s", size=3)) == 3
    assert len(_rotating_slice(rotatable, seed="s", size=99)) == 10


# --------------------------------------------------------------------------
# ci.yml <-> runner: the two sides of one env var (gotcha 28)
# --------------------------------------------------------------------------


def _cost_gate_script() -> str:
    workflow = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    for step in workflow["jobs"]["backend"]["steps"]:
        if "eval cost-gate" in (step.get("name") or ""):
            return str(step["run"])
    raise AssertionError("ci.yml has no 'eval cost-gate' step — re-point this guard")


def _enclosing_conditions(script: str, needle: str) -> list[str]:
    """The shell ``if`` conditions the line carrying ``needle`` sits inside.

    A substring check cannot answer this question: ``pull_request`` appears three
    times in that script and the comment above the line quotes the rule in order
    to explain it (gotchas 40/59/91/114 — a scanner that cannot tell a defect
    from the sentence explaining it). Full-line comments are dropped first; every
    ``#`` in this block is one.
    """
    stack: list[list[Any]] = []  # [condition, in_else]
    for raw in script.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("if "):
            stack.append([line, False])
        elif line == "else" and stack:
            stack[-1][1] = True
        elif line in {"fi", "fi;"} and stack:
            stack.pop()
        elif needle in line:
            return [str(c) for c, in_else in stack if not in_else]
    raise AssertionError(f"{needle!r} is not in the cost-gate step at all")


def test_rotation_is_pull_request_only() -> None:
    """Moving this line out of the ``if`` makes the WEEKLY scheduled run the only
    full pass — every merge to main would then rotate, and a case could reach
    main having never run. Structurally invisible to any behavioural test: the
    runner behaves identically either way, because it only ever reads the var.
    """
    script = _cost_gate_script()
    conditions = _enclosing_conditions(script, "HELIXIS_EVAL_PR_ROTATE=")
    assert conditions, "the rotation seed is set unconditionally — a merge would rotate"
    assert any("pull_request" in c for c in conditions), conditions


def test_the_variable_ci_sets_is_the_one_the_runner_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The name is a producer/consumer boundary and nothing else compares the two
    sides (gotcha 28). Renaming it in ci.yml alone is silent: rotation simply
    stops happening and the gate quietly returns to selecting everything.
    """
    script = _cost_gate_script()
    names = set(re.findall(r"\b(HELIXIS_EVAL_PR_[A-Z_]+)=", script))
    assert "HELIXIS_EVAL_PR_ROTATE" in names, sorted(names)

    # ...and the runner responds to that exact name. Two cases, one tier-1 and
    # one tier-2, with the slice set to zero: with the seed the tier-2 case is
    # deferred, without it nothing is.
    t1 = {
        "id": "t1",
        "case_type": "agent_flow",
        "expected": {"judge_safety_gate": True},
    }
    t2 = {
        "id": "t2",
        "case_type": "agent_flow",
        "expected": {"must_not_route_to": ["x"], "tool_call_trail_must_not_contain": ["read_page"]},
    }
    assert pr_gate_tier1(t1) and not pr_gate_tier1(t2), "fixture no longer splits"

    monkeypatch.setenv("HELIXIS_EVAL_PR_GATE", "1")
    monkeypatch.setenv("HELIXIS_EVAL_CODE_CHANGED", "true")
    monkeypatch.setenv("HELIXIS_EVAL_CHANGED_CASES", "")
    monkeypatch.setenv("HELIXIS_EVAL_PR_SLICE", "0")

    monkeypatch.delenv("HELIXIS_EVAL_PR_ROTATE", raising=False)
    assert {c["id"] for c in _apply_pr_gate([t1, t2])} == {"t1", "t2"}

    monkeypatch.setenv("HELIXIS_EVAL_PR_ROTATE", "deadbeef")
    assert {c["id"] for c in _apply_pr_gate([t1, t2])} == {"t1"}


# --------------------------------------------------------------------------
# the other half of the same cost change: the tracker's cadence
# --------------------------------------------------------------------------

FLAKE_YML = CI_YML.parent / "eval-flake.yml"


def test_the_stability_tracker_has_no_schedule_and_any_restored_one_is_weekly() -> None:
    """RE-POINTED 2026-09-22, and the reason is the whole lesson.

    This guard was written on 2026-09-16 asserting a WEEKLY cron EXISTS, for a
    founder spend decision of that date. On 2026-09-21 a second founder
    directive removed the schedule entirely — dispatch-only — and nothing
    re-pointed the test, so it failed ``KeyError: 'schedule'`` and red-gated the
    very PR carrying the change. **An assertion that demands the old state holds
    the product to it exactly as firmly as it catches a regression** (gotchas
    68/74/93/103/110/122/127/129/132). Re-point, never delete.

    MEASURED before the 09-16 cut: 3 sweeps x 152 billed cases = **456 live
    model runs a night**, ~13,680 a month — the same order as everything the PR
    gate spent in the same period. Weekly would have been ~1,824. Dispatch-only
    is 0 until a human clicks.

    ⚠ The 09-16 weekly cut NEVER TOOK EFFECT, which is why the cadence moved
    twice: GitHub reads ``schedule`` from the DEFAULT BRANCH ONLY, that cron sat
    on a feature branch, and main went on firing nightly for five more nights
    (CLAUDE.md gotcha 144, measured from the Actions history). So this file
    passing is NOT evidence about what is running — only merging is, and only
    the Actions tab confirms it.

    BOTH DIRECTIONS ARE PINNED, because the failure this exists to stop is
    unchanged in shape: somebody "fixing" the sweep timeouts by restoring a
    nightly cron, which reads as a repair and is an unbounded spend increase.
    Absent is the state today; a restored schedule must still be weekly. A
    DELIBERATE cadence change re-points this test — that is the point, not an
    obstacle.

    ``RUNS`` stays at 3 either way: the flaky-vs-failing signal needs N>1, so
    cutting sweeps rather than frequency would have cost the trend instead of
    the schedule.
    """
    workflow = yaml.safe_load(FLAKE_YML.read_text(encoding="utf-8"))
    # PyYAML resolves the bare `on:` key to the boolean True.
    triggers = workflow.get("on") or workflow.get(True)

    assert "workflow_dispatch" in triggers, (
        "the tracker must stay hand-runnable — it is the only full-coverage "
        "flake signal, and with no schedule the dispatch trigger is the ONLY "
        "way it can ever run (CLAUDE.md §4)."
    )

    # Today: no schedule at all (2026-09-21 founder directive). The loop below
    # is therefore vacuous right now and is NOT dead code — it is what makes a
    # restored cron a decision rather than an accident.
    crons = [str(s["cron"]) for s in triggers.get("schedule") or []]
    for cron in crons:
        fields = cron.split()
        assert len(fields) == 5, cron
        day_of_month, day_of_week = fields[2], fields[4]
        weekly = day_of_week not in {"*", "?"} or day_of_month not in {"*", "?"}
        assert weekly, (
            f"cron {cron!r} fires every day. The tracker was cut to weekly on "
            "2026-09-16 and to dispatch-only on 2026-09-21, both founder spend "
            "decisions (456 billed case-runs a night -> ~1,824 a month -> 0). "
            "A nightly cron is the state both decisions removed. If the cadence "
            "is being restored on purpose, re-point this test and say what it "
            "now costs."
        )

    for step in workflow["jobs"]["eval-flake"]["steps"]:
        env = step.get("env") or {}
        if "RUNS" in env:
            assert "3" in str(env["RUNS"]), env["RUNS"]
            break
    else:
        raise AssertionError("no step sets RUNS — re-point this guard")
