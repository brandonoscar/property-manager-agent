"""Tests for the Phase A.2.5 ActionSurface schema (v2).

We exercise: (a) v2 round-trip with first_followup, (b) v1
backwards-compatibility — surfaces that don't carry first_followup
(the two production rows that already shipped) must still validate,
(c) FollowupOption's needs_followup → followup_hint required
invariant, (d) FollowupQuestion option-count bounds, (e)
DoneSignal's done=true invariant.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from helixis.agent.planner import (
    ActionSurface,
    DoneSignal,
    FollowupOption,
    FollowupQuestion,
    ProposedAction,
)

# ── ActionSurface schema_version ─────────────────────────────────


def test_v2_action_surface_round_trips_with_first_followup() -> None:
    raw = {
        "schema_version": 2,
        "summary": "Tenant Josh Gil reported a leak under the kitchen sink.",
        "actions": [
            {
                "tool_id": "send_tenant_message",
                "label": "Message Josh",
                "intent": "Send a message to Josh confirming plumber dispatch",
                "parameters": {
                    "tenant_id": 82336,
                    "subject": "Plumber on the way",
                    "body": "Hi Josh — plumber is on the way.",
                },
                "first_followup": {
                    "prompt": "What tone should the message take?",
                    "options": [
                        {
                            "label": "Reassuring",
                            "description": "Apologetic, detailed timeline.",
                            "parameter_patch": {"body": "Hi Josh, sorry…"},
                            "needs_followup": False,
                            "followup_hint": None,
                        },
                        {
                            "label": "Short",
                            "parameter_patch": {"body": "Plumber on the way."},
                            "needs_followup": False,
                        },
                    ],
                    "allow_freeform": True,
                },
            }
        ],
        "freeform_placeholder": "e.g. 'Call preferred plumber directly'",
    }
    surface = ActionSurface.model_validate(raw)
    assert surface.schema_version == 2
    assert surface.actions[0].first_followup is not None
    assert len(surface.actions[0].first_followup.options) == 2
    # Round-trips cleanly to dict.
    dumped = surface.model_dump(mode="json")
    assert dumped["actions"][0]["first_followup"]["prompt"].startswith("What tone")


def test_v1_action_surface_without_first_followup_still_validates() -> None:
    """Production rows that shipped under v1 (no first_followup) must
    keep parsing here — otherwise the inbox can't render them."""
    raw_v1 = {
        "schema_version": 1,
        "summary": "Tenant Josh Gil reported a leak under the kitchen sink.",
        "actions": [
            {
                "tool_id": "update_buildium_task",
                "label": "Set priority to High",
                "intent": "Raise priority to High",
                "parameters": {"priority": "High", "category": "Plumbing"},
            }
        ],
        "freeform_placeholder": "e.g. 'Call preferred plumber directly'",
    }
    surface = ActionSurface.model_validate(raw_v1)
    assert surface.schema_version == 1
    assert surface.actions[0].first_followup is None


def test_action_surface_accepts_up_to_6_actions() -> None:
    """Cap raised 4 → 6 for the dynamic-card surface — the planner can
    render up to 6 cards when the signals justify it."""
    raw = {
        "schema_version": 2,
        "summary": "Six actions is the new ceiling.",
        "actions": [
            {
                "tool_id": "update_buildium_task",
                "label": f"Action {i}",
                "intent": f"Do action {i}",
                "parameters": {"priority": "Normal"},
            }
            for i in range(6)
        ],
        "freeform_placeholder": "hint hint hint",
    }
    surface = ActionSurface.model_validate(raw)
    assert len(surface.actions) == 6


def test_action_surface_accepts_summary_highlights() -> None:
    """UI refinement #2b — optional list of exact summary substrings to
    emphasize. Optional (rows written before it still parse) and capped."""
    surface = ActionSurface.model_validate(
        {
            "schema_version": 2,
            "summary": "Dana Whitfield wants to renew her lease at 214 Maple Ave.",
            "actions": [],
            "freeform_placeholder": "hint hint",
            "summary_highlights": ["Dana Whitfield", "renew her lease", "214 Maple Ave"],
        }
    )
    assert surface.summary_highlights == ["Dana Whitfield", "renew her lease", "214 Maple Ave"]
    no_hl = ActionSurface.model_validate(
        {
            "schema_version": 2,
            "summary": "A short summary with no highlights.",
            "actions": [],
            "freeform_placeholder": "hint hint",
        }
    )
    assert no_hl.summary_highlights is None


def test_action_surface_rejects_more_than_6_actions() -> None:
    raw = {
        "schema_version": 2,
        "summary": "Seven actions is one too many.",
        "actions": [
            {
                "tool_id": "update_buildium_task",
                "label": f"Action {i}",
                "intent": f"Do action {i}",
                "parameters": {"priority": "Normal"},
            }
            for i in range(7)
        ],
        "freeform_placeholder": "hint hint hint",
    }
    with pytest.raises(ValidationError):
        ActionSurface.model_validate(raw)


# ── FollowupOption invariants ────────────────────────────────────


def test_followup_option_requires_hint_when_needs_followup_is_true() -> None:
    """An option that flags needs_followup=true MUST carry a
    followup_hint — without one the deeper LLM call has no prompt
    signal beyond the chain."""
    with pytest.raises(ValidationError) as exc_info:
        FollowupOption.model_validate(
            {
                "label": "Escalate",
                "parameter_patch": {},
                "needs_followup": True,
                # followup_hint omitted on purpose
            }
        )
    assert "followup_hint" in str(exc_info.value)


def test_followup_option_accepts_hint_when_needs_followup_is_true() -> None:
    opt = FollowupOption.model_validate(
        {
            "label": "Escalate",
            "parameter_patch": {},
            "needs_followup": True,
            "followup_hint": "Ask which vendor to dispatch.",
        }
    )
    assert opt.needs_followup is True
    assert opt.followup_hint == "Ask which vendor to dispatch."


def test_followup_option_no_hint_required_when_needs_followup_false() -> None:
    opt = FollowupOption.model_validate(
        {
            "label": "Done",
            "parameter_patch": {"priority": "High"},
        }
    )
    assert opt.needs_followup is False
    assert opt.followup_hint is None


# ── FollowupQuestion bounds ──────────────────────────────────────


def test_followup_question_requires_2_to_5_options() -> None:
    base_opt = {"label": "x", "parameter_patch": {}, "needs_followup": False}

    # 1 option → invalid
    with pytest.raises(ValidationError):
        FollowupQuestion.model_validate(
            {"prompt": "Pick one", "options": [base_opt], "allow_freeform": True}
        )

    # 6 options → invalid
    with pytest.raises(ValidationError):
        FollowupQuestion.model_validate(
            {
                "prompt": "Pick one",
                "options": [base_opt for _ in range(6)],
                "allow_freeform": True,
            }
        )

    # 3 options → valid
    q = FollowupQuestion.model_validate(
        {
            "prompt": "Pick one of these",
            "options": [base_opt, base_opt, base_opt],
            "allow_freeform": True,
        }
    )
    assert len(q.options) == 3


# ── DoneSignal ───────────────────────────────────────────────────


def test_done_signal_must_have_done_true() -> None:
    with pytest.raises(ValidationError):
        DoneSignal.model_validate({"done": False, "merged_parameters": {}})


def test_done_signal_round_trips() -> None:
    sig = DoneSignal.model_validate(
        {"done": True, "merged_parameters": {"priority": "High", "category": "Plumbing"}}
    )
    assert sig.done is True
    assert sig.merged_parameters == {"priority": "High", "category": "Plumbing"}


# ── ProposedAction ───────────────────────────────────────────────


def test_proposed_action_first_followup_optional_defaults_to_none() -> None:
    action = ProposedAction.model_validate(
        {
            "tool_id": "add_internal_note",
            "label": "Add note",
            "intent": "Add internal note",
            "parameters": {"note_body": "x" * 20},
        }
    )
    assert action.first_followup is None
