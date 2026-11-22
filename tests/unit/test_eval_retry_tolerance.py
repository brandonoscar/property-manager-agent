"""Single-run eval flake tolerance (#240) — test_evals_pass no longer red-gates
on LLM quality variance, but still fails hard on safety breaches.

Pins ``_run_case_with_flake_tolerance`` (re-roll quality-only failures, never
safety/skip) and the routing-assertion safety/quality tagging that drives it.
"""

from __future__ import annotations

import pytest

from helixis.evals import runner as ev_runner
from helixis.evals.orchestrator_routing import RoutingRunResult, _check_routing_assertions


def _result(*, passed: bool, critical: bool = True, skipped: bool = False) -> ev_runner.CaseResult:
    assertions = (
        []
        if (passed or skipped)
        else [ev_runner.AssertionResult(name="x", passed=False, critical=critical)]
    )
    return ev_runner.CaseResult(case_id="c", assertions=assertions, skipped=skipped)


def _stub_run_case(monkeypatch, sequence: list[ev_runner.CaseResult]) -> dict:
    state = {"n": 0}

    async def _fake(_case: dict, *, judge: bool = False) -> ev_runner.CaseResult:
        r = sequence[state["n"]]
        state["n"] += 1
        return r

    monkeypatch.setattr(ev_runner, "run_case", _fake)
    return state


@pytest.mark.asyncio
async def test_quality_flake_is_rerolled_then_passes(monkeypatch) -> None:
    state = _stub_run_case(
        monkeypatch, [_result(passed=False, critical=False), _result(passed=True)]
    )
    out = await ev_runner._run_case_with_flake_tolerance({"id": "c"})
    assert out.passed is True
    assert state["n"] == 2  # one re-roll


@pytest.mark.asyncio
async def test_persistent_quality_failure_still_fails(monkeypatch) -> None:
    seq = [_result(passed=False, critical=False) for _ in range(5)]
    state = _stub_run_case(monkeypatch, seq)
    out = await ev_runner._run_case_with_flake_tolerance({"id": "c"})
    assert out.passed is False
    # 1 initial + _EVAL_FLAKE_RETRIES attempts, then gives up.
    assert state["n"] == 1 + ev_runner._EVAL_FLAKE_RETRIES


@pytest.mark.asyncio
async def test_quarantined_quality_failure_skips_reroll_in_full_suite(monkeypatch) -> None:
    # In the full-suite gate (skip_quarantined_reroll=True), a quarantined
    # case's quality-only failure is non-gating, so re-rolling it just burns
    # Gemini calls — take the first result as-is. ``orchestrator_routing`` is
    # model-driven → quarantined-by-default.
    state = _stub_run_case(monkeypatch, [_result(passed=False, critical=False) for _ in range(3)])
    out = await ev_runner._run_case_with_flake_tolerance(
        {"id": "c", "case_type": "orchestrator_routing"}, skip_quarantined_reroll=True
    )
    assert out.passed is False
    assert state["n"] == 1  # no re-roll for a quarantined case in the full-suite gate


@pytest.mark.asyncio
async def test_quarantined_case_still_rerolls_in_targeted_run(monkeypatch) -> None:
    # A targeted --case run (browser-sweep / §4 grids) does NOT skip: those count
    # each CLI run externally, so the internal re-roll must stay (default flag).
    state = _stub_run_case(
        monkeypatch, [_result(passed=False, critical=False), _result(passed=True)]
    )
    out = await ev_runner._run_case_with_flake_tolerance(
        {"id": "c", "case_type": "orchestrator_routing"}  # skip_quarantined_reroll defaults False
    )
    assert out.passed is True
    assert state["n"] == 2  # re-rolled once, then passed — sweep/grid path unchanged


@pytest.mark.asyncio
async def test_explicit_single_run_gate_true_still_rerolls_in_full_suite(monkeypatch) -> None:
    # A model-driven case opted BACK into gating (single_run_gate: true) is NOT
    # quarantined, so it re-rolls even in the full-suite gate (it would gate).
    state = _stub_run_case(
        monkeypatch, [_result(passed=False, critical=False), _result(passed=True)]
    )
    out = await ev_runner._run_case_with_flake_tolerance(
        {"id": "c", "case_type": "orchestrator_routing", "single_run_gate": True},
        skip_quarantined_reroll=True,
    )
    assert out.passed is True
    assert state["n"] == 2  # re-rolled once, then passed


@pytest.mark.asyncio
async def test_safety_failure_is_never_rerolled(monkeypatch) -> None:
    state = _stub_run_case(monkeypatch, [_result(passed=False, critical=True)])
    out = await ev_runner._run_case_with_flake_tolerance({"id": "c"})
    assert out.passed is False
    assert out.failure_kind == "safety"
    assert state["n"] == 1  # no re-roll for safety


@pytest.mark.asyncio
async def test_pass_first_try_no_reroll(monkeypatch) -> None:
    state = _stub_run_case(monkeypatch, [_result(passed=True)])
    out = await ev_runner._run_case_with_flake_tolerance({"id": "c"})
    assert out.passed is True
    assert state["n"] == 1


@pytest.mark.asyncio
async def test_skip_no_reroll(monkeypatch) -> None:
    state = _stub_run_case(monkeypatch, [_result(passed=False, skipped=True)])
    out = await ev_runner._run_case_with_flake_tolerance({"id": "c"})
    assert out.skipped is True
    assert state["n"] == 1


def test_routing_assertions_tagged_safety_vs_quality() -> None:
    res = RoutingRunResult(
        last_agent_name="Web Agent", final_output="here you go", handoff_trail=["Web Agent"]
    )
    expected = {
        "must_route_to": "Web Agent",
        "must_not_route_to": ["Buildium Agent"],
        "first_handoff_must_be": "Web Agent",
        "reply_must_contain": ["here"],
        "reply_must_not_contain": ["password"],
    }
    _check_routing_assertions(result=res, expected=expected)
    crit = {a.name: a.critical for a in res.assertions}
    # Quality (LLM-variable) — retryable.
    assert crit["last_agent_name == 'Web Agent'"] is False
    assert crit["first_handoff_must_be == 'Web Agent'"] is False
    assert crit["reply_must_contain[0]='here'"] is False
    # ⚠ RE-POINTED 2026-09-11, not deleted. This assertion is right about the
    # property (the harness splits LLM-variable expectations from prohibitions)
    # and was pinned to a classification that had `must_not_route_to` on the
    # wrong side. It compares the TERMINAL agent only — a run that dispatched
    # the forbidden specialist and handed back PASSES it — so it is the same
    # varies-run-to-run fact `must_route_to` records, not a prohibition. See
    # the reasoning at its builder in orchestrator_routing.
    assert crit["must_not_route_to[0] != 'Buildium Agent'"] is False
    # Safety (real prohibitions) — never re-rolled.
    assert crit["reply_must_not_contain[0]='password'"] is True
