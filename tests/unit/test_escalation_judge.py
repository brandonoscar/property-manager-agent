"""Escalation reply-quality LLM-judge (#218) — non-gating eval metric.

Pins the judge's parsing/clamping + best-effort failure behavior, and that a
judge score never affects a case's pass/fail (it is reported, never asserted).
"""

from __future__ import annotations

from typing import Any

import pytest

from helixis.evals import runner as ev_runner
from helixis.evals.escalation_judge import EscalationJudgeScore, judge_escalation_reply


def _fake_completion(content: str):
    """Build a fake litellm response whose .choices[0].message.content is set."""

    class _Msg:
        def __init__(self, c: str) -> None:
            self.content = c

    class _Choice:
        def __init__(self, c: str) -> None:
            self.message = _Msg(c)

    class _Resp:
        def __init__(self, c: str) -> None:
            self.choices = [_Choice(c)]

    async def _fn(**_kwargs: Any) -> _Resp:
        return _Resp(content)

    return _fn


@pytest.mark.asyncio
async def test_judge_parses_and_clamps() -> None:
    fn = _fake_completion(
        '{"names_gate_honestly": 0.9, "states_attempt_and_remainder": 1.5, '
        '"gives_next_step": -0.2, "overall": 0.8, "rationale": "clear and honest"}'
    )
    score = await judge_escalation_reply(
        user_message="get permit status", reply="I hit a login wall...", completion_fn=fn
    )
    assert score is not None
    assert score.names_gate_honestly == 0.9
    assert score.states_attempt_and_remainder == 1.0  # clamped from 1.5
    assert score.gives_next_step == 0.0  # clamped from -0.2
    assert score.rationale == "clear and honest"


@pytest.mark.asyncio
async def test_judge_does_not_request_temperature_zero() -> None:
    """Regression: the judge runs on claude-sonnet-5, which 422s on any
    temperature != 1 (litellm.UnsupportedParamsError). A hardcoded temp=0.0
    made every live judge call throw → swallowed → silent None (no score ever
    printed). Pin temperature=1.0 + drop_params so it survives the prod model
    and future model swaps."""
    captured: dict[str, Any] = {}

    async def _capture(**kwargs: Any) -> Any:
        captured.update(kwargs)

        class _Resp:
            choices = [
                type(
                    "C",
                    (),
                    {"message": type("M", (), {"content": '{"overall": 0.5}'})()},
                )()
            ]

        return _Resp()

    await judge_escalation_reply(user_message="x", reply="a real reply", completion_fn=_capture)
    assert captured.get("temperature") == 1.0
    assert captured.get("drop_params") is True


@pytest.mark.asyncio
async def test_empty_reply_scores_zero() -> None:
    score = await judge_escalation_reply(user_message="x", reply="   ")
    assert score is not None
    assert score.overall == 0.0


@pytest.mark.asyncio
async def test_malformed_json_returns_none() -> None:
    score = await judge_escalation_reply(
        user_message="x", reply="something", completion_fn=_fake_completion("not json at all")
    )
    assert score is None


@pytest.mark.asyncio
async def test_engine_exception_returns_none() -> None:
    async def _boom(**_kwargs: Any) -> Any:
        raise RuntimeError("provider down")

    score = await judge_escalation_reply(user_message="x", reply="y", completion_fn=_boom)
    assert score is None


def test_judge_score_is_non_gating() -> None:
    """A low judge score must NOT fail a case whose assertions all pass."""
    case = ev_runner.CaseResult(
        case_id="061",
        assertions=[ev_runner.AssertionResult(name="ok", passed=True)],
        escalation_judge=EscalationJudgeScore(0.0, 0.0, 0.0, 0.0, "terrible reply"),
    )
    assert case.passed is True
    assert case.failure_kind is None
