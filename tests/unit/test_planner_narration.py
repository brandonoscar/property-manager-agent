"""``ActionPlanner.narrate_reasoning`` timeout + fallback (PR #70).

The narration call is decorative — it must never block the write path
or stall the conversation. These tests assert:

1. Happy path: a fast LLM response is returned, trimmed to one line.
2. Timeout path: ``asyncio.TimeoutError`` is caught and the call falls
   back to a generic "Working on {intent}." string.
3. Transient LLM error path: ``litellm.Timeout`` / ``InternalServerError``
   etc. also drop to the fallback.
4. Empty response: a successful call that returns an empty string
   still falls back rather than emitting an empty narration line.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import litellm
import pytest

from helixis.agent.planner import ActionPlanner
from helixis.settings import Settings


@dataclass
class _FakeMessage:
    content: str


@dataclass
class _FakeChoice:
    message: _FakeMessage


@dataclass
class _FakeResponse:
    choices: list[_FakeChoice]


def _settings() -> Settings:
    return Settings(
        env="test",
        memory={"enabled": False},
        api={"host": "127.0.0.1", "port": 8000},
        llm={
            "default_model": "gemini/gemini-2.5-flash",
            "google_api_key": "fake-key",
            "models": {"reasoning": {"model": "gemini/gemini-2.5-flash"}},
        },
    )


@pytest.mark.asyncio
async def test_narrate_reasoning_happy_path_returns_one_line() -> None:
    async def fast_completion(**_kwargs: Any) -> _FakeResponse:
        return _FakeResponse(
            choices=[_FakeChoice(_FakeMessage("Drafting the note before sending it to Buildium."))]
        )

    planner = ActionPlanner(_settings(), completion_fn=fast_completion)
    text = await planner.narrate_reasoning(
        situation="Tenant Mei Chen reported a kitchen sink leak.",
        intent="Draft and send a note to Buildium.",
    )
    assert text == "Drafting the note before sending it to Buildium."


@pytest.mark.asyncio
async def test_narrate_reasoning_trims_multiline_output_to_first_line() -> None:
    """The system prompt asks for one sentence, but the model sometimes
    emits a preamble + sentence. We keep the first line only so the
    narration always fits on one row in the UI."""

    async def chatty_completion(**_kwargs: Any) -> _FakeResponse:
        return _FakeResponse(
            choices=[
                _FakeChoice(
                    _FakeMessage("Here's the narration:\nDrafting the note before sending it.")
                )
            ]
        )

    planner = ActionPlanner(_settings(), completion_fn=chatty_completion)
    text = await planner.narrate_reasoning(
        situation="x",
        intent="draft note",
    )
    assert text == "Here's the narration:"  # first line wins


@pytest.mark.asyncio
async def test_narrate_reasoning_falls_back_on_asyncio_timeout() -> None:
    async def slow_completion(**_kwargs: Any) -> _FakeResponse:
        # Sleep longer than the timeout we pass below.
        await asyncio.sleep(0.5)
        return _FakeResponse(choices=[_FakeChoice(_FakeMessage("too late"))])

    planner = ActionPlanner(_settings(), completion_fn=slow_completion)
    text = await planner.narrate_reasoning(
        situation="x",
        intent="Update the task in Buildium",
        timeout_seconds=0.05,
    )
    # Fallback is "Working on {intent_lower}." per _narration_fallback.
    assert text == "Working on update the task in buildium."


@pytest.mark.asyncio
async def test_narrate_reasoning_falls_back_on_litellm_transient_error() -> None:
    async def flaky_completion(**_kwargs: Any) -> _FakeResponse:
        raise litellm.InternalServerError(message="502", model="gemini/x", llm_provider="gemini")

    planner = ActionPlanner(_settings(), completion_fn=flaky_completion)
    text = await planner.narrate_reasoning(
        situation="x",
        intent="Send the tenant message",
    )
    assert text.startswith("Working on send the tenant message")


@pytest.mark.asyncio
async def test_narrate_reasoning_falls_back_on_empty_response() -> None:
    async def empty_completion(**_kwargs: Any) -> _FakeResponse:
        return _FakeResponse(choices=[_FakeChoice(_FakeMessage("   "))])

    planner = ActionPlanner(_settings(), completion_fn=empty_completion)
    text = await planner.narrate_reasoning(
        situation="x",
        intent="draft note",
    )
    assert text == "Working on draft note."
