"""Tests for ``ActionPlanner.followup`` — the Phase A.2.5 deeper-
followup LLM call invoked by ``POST /actions/{i}/followup``.

We exercise four shapes the LLM can produce, and the planner-side
behaviour around each:
  1. Happy path: returns a valid FollowupQuestion.
  2. Terminal: returns a DoneSignal with merged_parameters.
  3. Invalid JSON on first attempt → retry once with nudge → recover.
  4. Two-fail → raises PlannerError (the route translates that to 502).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import litellm
import pytest

from helixis.agent.planner import (
    ActionPlanner,
    DoneSignal,
    FollowupQuestion,
    PlannerError,
    ProposedAction,
)
from helixis.settings import Settings


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the transient-retry backoff instant in tests so the 0.5s
    ``asyncio.sleep`` in ``followup()``'s retry branch doesn't slow
    the suite. Autouse so every test in the file gets it for free."""

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("helixis.agent.planner.action_planner.asyncio.sleep", _instant)


@dataclass
class _FakeMessage:
    content: str


@dataclass
class _FakeChoice:
    message: _FakeMessage


@dataclass
class _FakeResponse:
    choices: list[_FakeChoice]


class _ScriptedCompletion:
    def __init__(self, scripts: list[str]) -> None:
        self._scripts = list(scripts)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        if not self._scripts:
            raise AssertionError("scripted completion ran out of responses")
        return _FakeResponse(choices=[_FakeChoice(_FakeMessage(self._scripts.pop(0)))])


def _settings() -> Settings:
    return Settings(
        env="test",
        memory={"enabled": False},
        api={"host": "127.0.0.1", "port": 8000},
        llm={
            "default_model": "gemini/gemini-2.0-flash",
            "google_api_key": "fake-key",
            "models": {"reasoning": {"model": "gemini/gemini-2.0-flash"}},
        },
    )


def _action() -> ProposedAction:
    return ProposedAction.model_validate(
        {
            "tool_id": "send_tenant_message",
            "label": "Message Josh",
            "intent": "Send message",
            "parameters": {
                "tenant_id": 82336,
                "subject": "Plumber",
                "body": "Hi Josh",
            },
        }
    )


@pytest.mark.asyncio
async def test_followup_happy_path_returns_followup_question() -> None:
    next_question = {
        "prompt": "Should we also schedule the inspection now?",
        "options": [
            {
                "label": "Yes, schedule in 1 week",
                "parameter_patch": {"schedule": "1w"},
                "needs_followup": False,
                "followup_hint": None,
            },
            {
                "label": "No, just send the message",
                "parameter_patch": {},
                "needs_followup": False,
                "followup_hint": None,
            },
        ],
        "allow_freeform": True,
    }
    fake = _ScriptedCompletion([json.dumps(next_question)])
    planner = ActionPlanner(_settings(), completion_fn=fake)

    result = await planner.followup(
        company_id="company-A",
        action=_action(),
        previous_picks=[
            {
                "prompt": "What tone should the message take?",
                "label": "Reassuring",
                "parameter_patch": {"body": "Hi Josh, sorry…"},
            }
        ],
        freeform_hint=None,
        event_summary="Tenant Josh Gil reported a leak.",
    )

    assert isinstance(result, FollowupQuestion)
    assert "schedule the inspection" in result.prompt
    assert len(result.options) == 2
    # LLM saw the chain context in the user message.
    user_msg = fake.calls[0]["messages"][1]["content"]
    assert "Reassuring" in user_msg
    assert "send_tenant_message" in user_msg


@pytest.mark.asyncio
async def test_followup_terminal_returns_done_signal() -> None:
    done = {
        "done": True,
        "merged_parameters": {
            "tenant_id": 82336,
            "subject": "Plumber on the way",
            "body": "Hi Josh, sorry about the leak.",
        },
    }
    fake = _ScriptedCompletion([json.dumps(done)])
    planner = ActionPlanner(_settings(), completion_fn=fake)

    result = await planner.followup(
        company_id="company-A",
        action=_action(),
        previous_picks=[],
        freeform_hint="Tenant has a child with asthma; mold concerns.",
    )

    assert isinstance(result, DoneSignal)
    assert result.done is True
    assert result.merged_parameters["tenant_id"] == 82336


@pytest.mark.asyncio
async def test_followup_recovers_after_one_invalid_response() -> None:
    """Invalid JSON on the first call should trigger one retry with a
    correction nudge; the second valid response is returned."""
    good = {
        "prompt": "Try again — what now?",
        "options": [
            {"label": "A", "parameter_patch": {}, "needs_followup": False},
            {"label": "B", "parameter_patch": {}, "needs_followup": False},
        ],
        "allow_freeform": True,
    }
    fake = _ScriptedCompletion(["not valid json {", json.dumps(good)])
    planner = ActionPlanner(_settings(), completion_fn=fake)

    result = await planner.followup(
        company_id="company-A",
        action=_action(),
        previous_picks=[],
        freeform_hint=None,
    )
    assert isinstance(result, FollowupQuestion)
    assert len(fake.calls) == 2
    # The retry call carries the correction nudge as the trailing message.
    second_messages = fake.calls[1]["messages"]
    assert "not valid" in second_messages[-1]["content"].lower()


@pytest.mark.asyncio
async def test_followup_two_failures_raises_planner_error() -> None:
    fake = _ScriptedCompletion(["nope {", "still nope {"])
    planner = ActionPlanner(_settings(), completion_fn=fake)

    with pytest.raises(PlannerError):
        await planner.followup(
            company_id="company-A",
            action=_action(),
            previous_picks=[],
            freeform_hint=None,
        )


@pytest.mark.asyncio
async def test_followup_retries_on_transient_network_error_then_succeeds() -> None:
    """A single ``litellm.InternalServerError`` (502/503/504) on the
    first call should trigger one retry, after which the parsed
    DoneSignal is returned. Regression for audit Finding #8."""

    class _FlakyCompletion:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def __call__(self, **kwargs: Any) -> _FakeResponse:
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise litellm.InternalServerError(
                    message="502", model="gemini/x", llm_provider="gemini"
                )
            return _FakeResponse(
                choices=[
                    _FakeChoice(
                        _FakeMessage(
                            json.dumps(
                                {
                                    "done": True,
                                    "merged_parameters": {
                                        "tenant_id": 82336,
                                        "subject": "Plumber",
                                        "body": "Hi Josh",
                                    },
                                }
                            )
                        )
                    )
                ]
            )

    fake = _FlakyCompletion()
    planner = ActionPlanner(_settings(), completion_fn=fake)

    result = await planner.followup(
        company_id="company-A",
        action=_action(),
        previous_picks=[],
        freeform_hint=None,
    )
    assert isinstance(result, DoneSignal)
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_followup_raises_planner_error_when_transient_exhausts_retries() -> None:
    """Two consecutive ``litellm.RateLimitError`` raises PlannerError
    with the transient-retry message."""

    async def always_fails(**_kwargs: Any) -> _FakeResponse:
        raise litellm.RateLimitError(message="429", model="gemini/x", llm_provider="gemini")

    planner = ActionPlanner(_settings(), completion_fn=always_fails)

    with pytest.raises(PlannerError, match="exhausted transient retries"):
        await planner.followup(
            company_id="company-A",
            action=_action(),
            previous_picks=[],
            freeform_hint=None,
        )
