"""Unit tests for the per-PR eval cost gate (runner.select_pr_gate_cases).

Pure selection logic — no model, no CI. Verifies the rule that cuts per-PR Gemini
spend without losing coverage (the nightly full suite runs everything):
  * deterministic (no-Gemini) cases always run — they're free,
  * a case whose golden file changed always runs,
  * safety cases run when agent/eval CODE changed,
  * everything else defers to nightly.
"""

from __future__ import annotations

from helixis.evals.runner import (
    _apply_pr_gate,
    _has_safety_assertion,
    select_pr_gate_cases,
)

GEMINI = "HELIXIS_LLM__GOOGLE_API_KEY"


def _case(cid: str, *, gemini: bool = True, expected: dict | None = None) -> dict:
    # gemini=True → a model-driven case that makes a live call; gemini=False → a
    # deterministic tool_invocation case (e.g. web input-validation) that doesn't.
    if gemini:
        return {
            "id": cid,
            "case_type": "agent_flow",
            "expected": expected or {"reply_must_contain": ["x"]},
        }
    return {
        "id": cid,
        "case_type": "tool_invocation",
        "requires_env": ["HELIXIS_TAVILY__API_KEY"],
        "expected": expected or {"reply_must_contain": ["x"]},
    }


SAFETY = {"tool_call_trail_must_not_contain": ["GMAIL_SEND_EMAIL"]}
QUALITY = {"reply_must_contain": ["hello"], "must_route_to": ["Helixis"]}


def test_has_safety_assertion() -> None:
    assert _has_safety_assertion({"expected": SAFETY})
    assert _has_safety_assertion({"expected": {"reply_must_not_match_regex": ["t"]}})
    assert _has_safety_assertion({"expected": {"tool_call_count_max": 1}})
    assert _has_safety_assertion({"expected": {"reverse_report_unknown_blocks_writes": True}})
    assert not _has_safety_assertion({"expected": QUALITY})
    assert not _has_safety_assertion({"expected": {}})


def test_deterministic_cases_always_run() -> None:
    cases = [_case("011_web", gemini=False, expected=QUALITY)]
    # No changed, no code change — still selected because it costs no Gemini.
    assert select_pr_gate_cases(cases, set(), eval_code_changed=False) == cases


def test_changed_case_always_runs() -> None:
    cases = [_case("402_injection", expected=QUALITY)]
    got = select_pr_gate_cases(cases, {"402_injection"}, eval_code_changed=False)
    assert got == cases


def test_safety_case_runs_only_when_code_changed() -> None:
    cases = [_case("320_negative", expected=SAFETY)]
    assert select_pr_gate_cases(cases, set(), eval_code_changed=True) == cases
    # Docs-only PR: no code change, case unchanged → deferred to nightly.
    assert select_pr_gate_cases(cases, set(), eval_code_changed=False) == []


def test_pure_quality_case_defers_unless_changed() -> None:
    cases = [_case("042_gmail_recent", expected=QUALITY)]
    # Even with a code change, a case with no safety assertion isn't a per-PR gate.
    assert select_pr_gate_cases(cases, set(), eval_code_changed=True) == []
    # ...unless its own golden file changed.
    assert select_pr_gate_cases(cases, {"042_gmail_recent"}, eval_code_changed=True) == cases


def test_realistic_mix() -> None:
    cases = [
        _case("011_web", gemini=False, expected=QUALITY),  # free → run
        _case("402_injection", expected=SAFETY),  # safety → run iff code changed
        _case("042_gmail_recent", expected=QUALITY),  # quality → nightly
        _case("320_negative", expected=SAFETY),  # safety → run iff code changed
    ]
    docs_pr = select_pr_gate_cases(cases, set(), eval_code_changed=False)
    assert {c["id"] for c in docs_pr} == {"011_web"}  # only the free one

    agent_pr = select_pr_gate_cases(cases, set(), eval_code_changed=True)
    assert {c["id"] for c in agent_pr} == {"011_web", "402_injection", "320_negative"}

    eval_pr = select_pr_gate_cases(cases, {"042_gmail_recent"}, eval_code_changed=True)
    assert {c["id"] for c in eval_pr} == {
        "011_web",
        "402_injection",
        "320_negative",
        "042_gmail_recent",
    }


def test_apply_pr_gate_is_noop_without_env(monkeypatch) -> None:
    monkeypatch.delenv("HELIXIS_EVAL_PR_GATE", raising=False)
    cases = [_case("402_injection", expected=SAFETY), _case("042", expected=QUALITY)]
    assert _apply_pr_gate(cases) == cases


def test_apply_pr_gate_filters_with_env(monkeypatch) -> None:
    monkeypatch.setenv("HELIXIS_EVAL_PR_GATE", "1")
    monkeypatch.setenv("HELIXIS_EVAL_CODE_CHANGED", "false")
    monkeypatch.setenv("HELIXIS_EVAL_CHANGED_CASES", "042_gmail_recent")
    cases = [
        _case("402_injection", expected=SAFETY),  # safety, no code change → deferred
        _case("042_gmail_recent", expected=QUALITY),  # changed → run
        _case("011_web", gemini=False, expected=QUALITY),  # free → run
    ]
    got = {c["id"] for c in _apply_pr_gate(cases)}
    assert got == {"042_gmail_recent", "011_web"}
