"""Haiku fast router (upgrade 2) — routing-only precheck, dark by default.

Pins the design invariant: FAIL OPEN. The router may only ever save a
hop — any doubt, garbage, error, timeout, or disabled capability routes
to the full orchestrator, and with the flag off it never even calls the
classifier.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.agents import fast_router
from helixis.settings import Settings


def _settings(**agent_over: object) -> Settings:
    agent = {"fast_router": True, **agent_over}
    return Settings(env="test", agent=agent, memory={"enabled": False})


def _completion_reply(text: object) -> MagicMock:
    choice = MagicMock()
    choice.message.content = text
    resp = MagicMock()
    resp.choices = [choice]
    return resp


# ── parse_route: pure, total, fail-open ──────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BUILDIUM", "buildium"),
        ("buildium", "buildium"),
        ("  Buildium.\n", "buildium"),
        ("ANALYST", "analyst"),
        ("analyst!", "analyst"),
        ("ORCHESTRATOR", "orchestrator"),
        ("BUILDIUM_AGENT", "orchestrator"),  # not the exact token — fail open
        ("The answer is BUILDIUM", "orchestrator"),  # leading prose — fail open
        ("", "orchestrator"),
        ("   ", "orchestrator"),
        (None, "orchestrator"),
        (42, "orchestrator"),
    ],
)
def test_parse_route(raw: object, expected: str) -> None:
    assert fast_router.parse_route(raw) == expected


# ── route(): flag gating, classification, fail-open ─────────────────


def test_flag_off_never_calls_the_classifier() -> None:
    acompletion = AsyncMock()
    with patch("litellm.acompletion", acompletion):
        out = asyncio.run(
            fast_router.route("what units are at Riverside?", _settings(fast_router=False))
        )
    assert out == "orchestrator"
    acompletion.assert_not_awaited()


def test_buildium_classification_routes_buildium() -> None:
    with patch("litellm.acompletion", AsyncMock(return_value=_completion_reply("BUILDIUM"))):
        out = asyncio.run(fast_router.route("what units are at Riverside?", _settings()))
    assert out == "buildium"


def test_analyst_requires_analysis_enabled() -> None:
    # Classifier says analyst, but the capability is dark on this deploy —
    # fall open so the orchestrator's hand-off surfaces the proper message.
    with patch("litellm.acompletion", AsyncMock(return_value=_completion_reply("ANALYST"))):
        settings = _settings()
        assert settings.analysis.enabled is False
        out = asyncio.run(fast_router.route("which property is most at risk?", settings))
    assert out == "orchestrator"

    with patch("litellm.acompletion", AsyncMock(return_value=_completion_reply("ANALYST"))):
        settings = Settings(
            env="test",
            agent={"fast_router": True},
            analysis={"enabled": True},
            memory={"enabled": False},
        )
        out = asyncio.run(fast_router.route("which property is most at risk?", settings))
    assert out == "analyst"


def test_classifier_exception_fails_open() -> None:
    with patch("litellm.acompletion", AsyncMock(side_effect=RuntimeError("api down"))):
        out = asyncio.run(fast_router.route("what units are at Riverside?", _settings()))
    assert out == "orchestrator"


def test_classifier_timeout_fails_open() -> None:
    async def _slow(**_kw: object) -> MagicMock:
        await asyncio.sleep(5)
        return _completion_reply("BUILDIUM")

    with patch("litellm.acompletion", _slow):
        out = asyncio.run(
            fast_router.route("what units are at Riverside?", _settings(fast_router_timeout_s=0.05))
        )
    assert out == "orchestrator"


def test_garbage_reply_fails_open() -> None:
    with patch(
        "litellm.acompletion",
        AsyncMock(return_value=_completion_reply("I think you should ask Buildium")),
    ):
        out = asyncio.run(fast_router.route("hm", _settings()))
    assert out == "orchestrator"


def test_task_is_truncated_before_classification() -> None:
    acompletion = AsyncMock(return_value=_completion_reply("ORCHESTRATOR"))
    with patch("litellm.acompletion", acompletion):
        asyncio.run(fast_router.route("x" * 10_000, _settings()))
    sent = acompletion.await_args.kwargs["messages"][1]["content"]
    assert len(sent) == 2000  # cheap call stays cheap on pasted walls of text
