"""Quality-regression tracking (Q72) — prompt/model trace tags + summary scores.

The langfuse-render audit (Q72) found the run-tracer's ``record_score`` helper
existed but was wired to a single canary call site, so the Scores view was
empty. This module de-risks that by:

  * tagging the planner trace with the prompt version + model slug so quality
    can be sliced per prompt version, and
  * emitting a ``summary_quality`` score per summarize (1.0 pass / 0.0 shipped-
    flagged) with the flag category in the comment.

These tests cover the pure classifiers (``model_slug`` / ``quality_flag``) and
assert the summarizer actually emits the score with the right value + category.
The pure ``quality_flag`` round-trip guards against the reason strings in
``quality.py`` drifting away from the categories charts depend on.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from helixis.agent.summarizer import Summarizer
from helixis.agent.summarizer.quality import assess_summary_quality, quality_flag
from helixis.observability.tracing import model_slug

# ── model_slug (pure) ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "model_name,expected",
    [
        ("gemini/gemini-2.5-flash", "gemini-2.5-flash"),
        ("gemini/gemini-2.5-flash-lite", "gemini-2.5-flash-lite"),
        ("openai/gpt-4o", "gpt-4o"),
        ("vertex_ai/gemini-1.5-pro", "gemini-1.5-pro"),
        # No provider prefix — pass through bare.
        ("gemini-2.5-flash", "gemini-2.5-flash"),
        # Empty / whitespace-only never yields a blank tag.
        ("", "unknown"),
        ("   ", "unknown"),
        ("gemini/", "unknown"),
    ],
)
def test_model_slug_strips_provider_prefix(model_name: str, expected: str) -> None:
    assert model_slug(model_name) == expected


# ── quality_flag (pure) ────────────────────────────────────────────


def test_quality_flag_none_is_ok() -> None:
    assert quality_flag(None) == "ok"


def test_quality_flag_unrecognised_reason_is_other() -> None:
    assert quality_flag("some brand new reason we don't classify yet") == "other"


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("too short (32 chars; aim for 2-4 sentences)", "too_short"),
        (
            "starts with a generic filler stem instead of the actual "
            "people, place, and problem from the enrichment",
            "filler_opener",
        ),
        ("doesn't mention any concrete fact from the enrichment (100 Main Ave)", "ungrounded"),
    ],
)
def test_quality_flag_maps_known_reasons(reason: str, expected: str) -> None:
    assert quality_flag(reason) == expected


def test_quality_flag_roundtrips_real_assessment_reasons() -> None:
    """Guard against the reason strings drifting from the categories.

    Feed ``assess_summary_quality`` inputs engineered to trigger each
    heuristic, then assert ``quality_flag`` classifies the real reason it
    returns — so if someone edits a reason string without updating the map,
    this fails instead of silently reclassifying a flag to ``other``."""
    enrichment = {
        "task": {"title": "Kitchen sink leak"},
        "unit": {"address_line1": "100 Main Ave"},
    }

    # too_short — under the 60-char floor.
    short = assess_summary_quality("Leak.", {}, enrichment)
    assert short is not None and quality_flag(short) == "too_short"

    # filler_opener — a generic stem, long enough to clear the length floor.
    filler = assess_summary_quality(
        "A task was created this evening and someone will look into it shortly.",
        {},
        enrichment,
    )
    assert filler is not None and quality_flag(filler) == "filler_opener"

    # ungrounded — long, non-filler, but references none of the anchors.
    ungrounded = assess_summary_quality(
        "The weather has been pleasant lately and the garden is looking healthy overall.",
        {},
        enrichment,
    )
    assert ungrounded is not None and quality_flag(ungrounded) == "ungrounded"

    # ok — grounded, long, non-filler.
    ok = assess_summary_quality(
        "A kitchen sink leak was reported at 100 Main Ave and needs a plumber soon.",
        {},
        enrichment,
    )
    assert ok is None and quality_flag(ok) == "ok"


# ── Summarizer emits summary_quality score ─────────────────────────


def _make_response(text: str) -> Any:
    """Build a LiteLLM-shaped response object."""
    return type(
        "Resp",
        (),
        {
            "choices": [
                type(
                    "Choice",
                    (),
                    {"message": type("Msg", (), {"content": text})()},
                )()
            ]
        },
    )()


def _settings_stub() -> Any:
    settings = type("Settings", (), {})()
    llm = type("LLM", (), {})()
    llm.models = {"reasoning": {"model": "gemini/gemini-2.5-flash"}}
    llm.default_model = "gemini/gemini-2.5-flash"
    llm.google_api_key = "test-gemini-key"
    llm.openai_api_key = None
    llm.anthropic_api_key = None
    settings.llm = llm
    return settings


@pytest.mark.asyncio
async def test_summarizer_emits_pass_score_for_good_summary(monkeypatch: Any) -> None:
    scores: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "helixis.agent.summarizer.client.record_score",
        lambda **kw: scores.append(kw),
    )

    text = "Sarah Chen at 123 Main St unit 4B reported a kitchen sink leak this afternoon."
    completion = AsyncMock(return_value=_make_response(text))
    summarizer = Summarizer(settings=_settings_stub(), completion_fn=completion)

    await summarizer.summarize(
        payload={"EventName": "Task.Created"},
        enrichment={"task": {"title": "leak"}},
        company_id="company-q72",
    )

    assert len(scores) == 1
    assert scores[0]["name"] == "summary_quality"
    assert scores[0]["value"] == 1.0
    assert scores[0]["comment"] == "ok"
    # The trace id it scores against is the run's fresh id (non-empty).
    assert scores[0]["trace_id"]


@pytest.mark.asyncio
async def test_summarizer_emits_fail_score_when_shipping_flagged(monkeypatch: Any) -> None:
    scores: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "helixis.agent.summarizer.client.record_score",
        lambda **kw: scores.append(kw),
    )

    # Too-short output on every attempt → ships flagged once out of budget.
    completion = AsyncMock(return_value=_make_response("Leak."))
    summarizer = Summarizer(settings=_settings_stub(), completion_fn=completion, max_retries=1)

    await summarizer.summarize(
        payload={"EventName": "Task.Created"},
        enrichment={"task": {"title": "leak"}},
        company_id="company-q72",
    )

    # One score for the shipped (still-flagged) summary — not one per attempt.
    assert len(scores) == 1
    assert scores[0]["name"] == "summary_quality"
    assert scores[0]["value"] == 0.0
    assert scores[0]["comment"] == "too_short"
