"""Rubric LLM-judge (Gate-2) — pins parsing/clamping, the independent-model
resolution (no self-grading), the best-effort failure posture, the binary
compliance predicate, and that the seed trace set validates as ADVISORY-ONLY
(below the 50-trace gating floor) end-to-end.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from helixis.evals import rubric_judge as rj
from helixis.evals.judge_validation import (
    MIN_TRACES,
    load_labeled_traces,
    validate,
    validate_judge_over_traces,
)
from helixis.settings import Settings


def _settings(*, default: str = "anthropic/claude-sonnet-5", key: str = "sk-ant-xxx") -> Settings:
    s = Settings()
    s.llm.default_model = default
    s.llm.anthropic_api_key = key
    return s


def _fake_completion(content: str):
    async def _fn(**_kwargs: Any) -> Any:
        class _Msg:
            def __init__(self, c: str) -> None:
                self.content = c

        class _Choice:
            def __init__(self, c: str) -> None:
                self.message = _Msg(c)

        class _Resp:
            def __init__(self, c: str) -> None:
                self.choices = [_Choice(c)]

        return _Resp(content)

    return _fn


# ── parsing / clamping ───────────────────────────────────────────────


def test_parse_rubric_clamps_and_truncates() -> None:
    score = rj.parse_rubric(
        {
            "correctness": 1.5,
            "groundedness": -0.2,
            "completeness": 0.7,
            "tone": 0.8,
            "safety_compliance": 1.0,
            "actionability": 0.9,
            "overall": 0.85,
            "rationale": "x" * 400,
        }
    )
    assert score is not None
    assert score.correctness == 1.0  # clamped from 1.5
    assert score.groundedness == 0.0  # clamped from -0.2
    assert len(score.rationale) == 300


def test_parse_rubric_none_on_non_dict() -> None:
    assert rj.parse_rubric("not a dict") is None
    assert rj.parse_rubric(None) is None


def test_is_compliant_threshold() -> None:
    base = dict(
        correctness=1.0,
        groundedness=1.0,
        completeness=1.0,
        tone=1.0,
        actionability=1.0,
        overall=1.0,
    )
    assert rj.RubricScore(safety_compliance=1.0, rationale="", **base).is_compliant is True
    assert rj.RubricScore(safety_compliance=0.5, rationale="", **base).is_compliant is True
    assert rj.RubricScore(safety_compliance=0.0, rationale="", **base).is_compliant is False


# ── pack_trace ───────────────────────────────────────────────────────


def test_pack_trace_includes_context_only_when_present() -> None:
    with_ctx = rj.pack_trace(user_message="q", reply="a", context="tool said X")
    assert "tool said X" in with_ctx and "Context" in with_ctx
    without = rj.pack_trace(user_message="q", reply="a")
    assert "Context" not in without


# ── independent judge model (no self-grading) ────────────────────────


def test_judge_model_defaults_to_independent_opus() -> None:
    model = rj.judge_model(_settings())
    assert model == "anthropic/claude-opus-4-8"
    assert model != _settings().llm.default_model  # not the product model


def test_judge_model_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HELIXIS_JUDGE_MODEL", "anthropic/claude-opus-4-7")
    assert rj.judge_model(_settings()) == "anthropic/claude-opus-4-7"


def test_judge_model_warns_on_self_grading(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Force the judge to equal the product model → must warn (SELF-GRADING).
    monkeypatch.setenv("HELIXIS_JUDGE_MODEL", "anthropic/claude-sonnet-5")
    with caplog.at_level("WARNING"):
        model = rj.judge_model(_settings(default="anthropic/claude-sonnet-5"))
    assert model == "anthropic/claude-sonnet-5"
    assert any("SELF-GRADING" in r.message for r in caplog.records)


# ── score_content: best-effort, never raises ─────────────────────────


@pytest.mark.asyncio
async def test_score_content_parses_valid_json() -> None:
    fn = _fake_completion(
        '{"correctness": 0.9, "groundedness": 0.8, "completeness": 0.7, '
        '"tone": 1.0, "safety_compliance": 1.0, "actionability": 0.9, '
        '"overall": 0.88, "rationale": "solid"}'
    )
    score = await rj.score_content("packed trace", settings=_settings(), completion_fn=fn)
    assert score is not None
    assert score.overall == 0.88
    assert score.is_compliant is True


@pytest.mark.asyncio
async def test_score_content_empty_returns_none() -> None:
    assert await rj.score_content("   ", settings=_settings()) is None


@pytest.mark.asyncio
async def test_score_content_malformed_json_returns_none() -> None:
    fn = _fake_completion("not json")
    assert await rj.score_content("x", settings=_settings(), completion_fn=fn) is None


# ── loads_lenient: tolerant JSON extraction (the fn=1 recovery) ───────
#
# A strict json.loads on the model's response was the swallow that scored a
# COMPLIANT reply as a violation in the live validation run (fp path → the
# fn=1). The judge is instructed to emit a bare object, but occasionally fences
# it or adds a stray sentence — recover those instead of failing closed.

_VALID_RUBRIC = (
    '{"correctness": 0.9, "groundedness": 0.8, "completeness": 0.7, '
    '"tone": 1.0, "safety_compliance": 1.0, "actionability": 0.9, '
    '"overall": 0.88, "rationale": "solid"}'
)


def test_loads_lenient_plain_object() -> None:
    assert rj.loads_lenient('{"a": 1}') == {"a": 1}


def test_loads_lenient_strips_json_code_fence() -> None:
    fenced = f"```json\n{_VALID_RUBRIC}\n```"
    data = rj.loads_lenient(fenced)
    assert data["safety_compliance"] == 1.0


def test_loads_lenient_strips_bare_code_fence() -> None:
    fenced = f"```\n{_VALID_RUBRIC}\n```"
    assert rj.loads_lenient(fenced)["overall"] == 0.88


def test_loads_lenient_extracts_object_from_prose() -> None:
    wrapped = f"Here is my assessment:\n{_VALID_RUBRIC}\nHope that helps."
    assert rj.loads_lenient(wrapped)["tone"] == 1.0


def test_loads_lenient_extracts_outermost_object_with_nested_braces() -> None:
    # A rationale containing a brace must not truncate the outermost object —
    # the greedy {.*} must reach the LAST closing brace.
    raw = '{"safety_compliance": 0.0, "rationale": "steers via {race}", "overall": 0.1}'
    data = rj.loads_lenient(f"prefix {raw} suffix")
    assert data["safety_compliance"] == 0.0
    assert data["rationale"] == "steers via {race}"


def test_loads_lenient_raises_on_no_object() -> None:
    with pytest.raises(ValueError):
        rj.loads_lenient("there is no json here at all")


def test_loads_lenient_raises_on_non_string() -> None:
    with pytest.raises(ValueError):
        rj.loads_lenient(None)


@pytest.mark.asyncio
async def test_score_content_recovers_fenced_json() -> None:
    """A ```json-fenced response (the exact live-validation swallow) is now
    parsed into a real score instead of failing closed to None — this is the
    fn=1 recovery, pushing TPR toward 100%."""
    fn = _fake_completion(f"```json\n{_VALID_RUBRIC}\n```")
    score = await rj.score_content("packed trace", settings=_settings(), completion_fn=fn)
    assert score is not None
    assert score.overall == 0.88
    assert score.is_compliant is True


@pytest.mark.asyncio
async def test_score_content_recovers_prose_wrapped_json() -> None:
    fn = _fake_completion(f"Sure — here you go: {_VALID_RUBRIC}")
    score = await rj.score_content("packed trace", settings=_settings(), completion_fn=fn)
    assert score is not None
    assert score.is_compliant is True


@pytest.mark.asyncio
async def test_score_content_provider_exception_returns_none() -> None:
    async def _boom(**_kwargs: Any) -> Any:
        raise RuntimeError("provider down")

    assert await rj.score_content("x", settings=_settings(), completion_fn=_boom) is None


# ── compliance predicate (the injected judge_fn) ─────────────────────


def _score(safety: float) -> rj.RubricScore:
    return rj.RubricScore(
        correctness=1.0,
        groundedness=1.0,
        completeness=1.0,
        tone=1.0,
        safety_compliance=safety,
        actionability=1.0,
        overall=1.0,
        rationale="",
    )


def test_compliance_predicate_true_on_compliant() -> None:
    async def _scorer(_content: str) -> rj.RubricScore:
        return _score(1.0)

    assert rj.compliance_predicate(scorer=_scorer)("x") is True


def test_compliance_predicate_false_on_violation() -> None:
    async def _scorer(_content: str) -> rj.RubricScore:
        return _score(0.0)

    assert rj.compliance_predicate(scorer=_scorer)("x") is False


def test_compliance_predicate_fails_closed_on_none() -> None:
    """An unreachable judge (None) must count as NOT-proven-compliant, so it can
    never validate as perfectly safe."""

    async def _scorer(_content: str) -> rj.RubricScore | None:
        return None

    assert rj.compliance_predicate(scorer=_scorer)("x") is False


# ── seed traces validate as ADVISORY-ONLY (below the 50-floor) ───────


def test_runner_rubric_report_prints_dims_and_mean(capsys: pytest.CaptureFixture[str]) -> None:
    """The non-gating rubric report prints per-dimension scores + a mean line, so
    a weak axis (e.g. groundedness) is visible without gating anything."""
    from helixis.evals import runner as ev_runner

    cr = ev_runner.CaseResult(
        case_id="111",
        rubric_score=rj.RubricScore(
            correctness=0.9,
            groundedness=0.4,
            completeness=0.8,
            tone=1.0,
            safety_compliance=1.0,
            actionability=0.9,
            overall=0.83,
            rationale="grounding weak",
        ),
    )
    ev_runner._print_rubric_judge_report([cr])
    out = capsys.readouterr().out
    assert "Answer quality (rubric LLM-judge, non-gating" in out
    assert "111:" in out
    assert "grou=0.40" in out  # the weak dimension is surfaced
    assert "mean:" in out


def test_runner_rubric_report_silent_when_no_scores(capsys: pytest.CaptureFixture[str]) -> None:
    from helixis.evals import runner as ev_runner

    ev_runner._print_rubric_judge_report([ev_runner.CaseResult(case_id="x")])
    assert capsys.readouterr().out == ""


def _seed_path() -> Path:
    return Path(rj.__file__).parent / "judge_traces" / "fair_housing_compliance.jsonl"


def test_corpus_clears_the_count_floor_and_is_balanced() -> None:
    """The fair-housing corpus is now at/above the gating floor (>=50 labels)
    with both classes represented — so a validated judge on it CAN gate."""
    traces = load_labeled_traces(_seed_path())
    assert len(traces) >= MIN_TRACES
    labels = [t.label for t in traces]
    assert {True, False} <= set(labels)  # noqa: E712 — bool set membership
    # Roughly balanced so TPR and TNR are both meaningfully measurable.
    positives = sum(labels)
    assert 0.3 <= positives / len(labels) <= 0.7


def test_corpus_can_validate_a_perfect_judge() -> None:
    """A judge that agrees with every human label VALIDATES (gating) on this
    corpus — proving the label set is large + balanced enough to certify a
    good judge, not just track it."""
    traces = load_labeled_traces(_seed_path())
    labels = [t.label for t in traces]
    result = validate(labels, labels)  # perfect predictions
    assert result.is_valid is True
    assert result.tpr == 1.0 and result.tnr == 1.0
    assert result.reasons == ()


def test_corpus_rejects_a_lazy_always_compliant_judge() -> None:
    """The other half of the gate: a judge that rubber-stamps everything as
    compliant is NOT validated — it misses every violation, so TNR collapses."""
    traces = load_labeled_traces(_seed_path())
    result = validate_judge_over_traces(traces, lambda _c: True)
    assert result.is_valid is False
    assert result.tnr == 0.0  # caught none of the violations
