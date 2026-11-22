"""Adaptive reasoning effort — the 163s median.

Every turn in this product thinks with the same budget, because
``reasoning_effort`` has never been set anywhere in the fleet. "What's the
balance on 4B" therefore costs a property manager roughly what "analyse my
whole portfolio" costs, and that is the most-felt problem the batteries
measure.

The safety property these pin is the direction of error: the classifier may
only ever DOWNGRADE, and only on shapes it positively recognises. Guessing
"hard" wrong costs nothing — that is today's behaviour. Guessing "easy" wrong
makes a hard question worse, and for a manager acting on the answer,
slow-but-right beats fast-but-wrong.
"""

from __future__ import annotations

import pytest

from helixis.agents.effort import classify_effort, effort_settings


@pytest.mark.parametrize(
    "task",
    [
        "What's the balance on 4B?",
        "what is the rent for unit 12",
        "How much is rent for unit 12?",
        "Who's the owner of 600 Dexter Ave?",
        "When does the lease end for 4B?",
        "Show me the open work orders",
        "look up the phone number for Marcus Whitfield",
    ],
)
def test_single_fact_lookups_are_downgraded(task: str) -> None:
    assert classify_effort(task) == "low"


@pytest.mark.parametrize(
    "task",
    [
        # Judgement — the questions the product exists for.
        "Should I renew this lease at this rent?",
        "Why is this work order still open?",
        "Compare maintenance spend across my properties",
        # Portfolio scope — the shape that blows the wall clock.
        "For every lease expiring in the next 90 days, tell me the tenant and their history",
        "Are any of our open maintenance requests duplicates of each other?",
        "What's my total exposure across all my properties?",
        # Writes are never trivial, whatever they look like.
        "Draft late notices for the 61-90 day accounts",
        "Close work order 9006",
        # A lookup opener that turns into something else partway through.
        "What's the balance on 4B and should I file an eviction?",
        "What's the balance on 4B? Then draft a late notice.",
    ],
)
def test_anything_needing_thought_keeps_its_budget(task: str) -> None:
    assert classify_effort(task) is None


def test_an_unrecognised_question_is_left_alone() -> None:
    """The default must be today's behaviour, not a guess. This is what lets
    the feature ship without a dogfood tuning pass."""
    assert classify_effort("Hmm, the Redbud situation is bothering me") is None
    assert classify_effort("") is None


def test_a_long_question_is_never_a_simple_lookup() -> None:
    """A lookup opener on a paragraph is a paragraph, not a lookup."""
    long_task = "What's the balance on 4B " + "and also the one next door " * 10
    assert classify_effort(long_task) is None


def test_the_setting_can_pin_every_turn_to_one_budget() -> None:
    assert effort_settings("What's the balance on 4B?", enabled=False) is None


def test_a_downgrade_produces_model_settings_the_sdk_forwards() -> None:
    """``LitellmModel`` reads ``ModelSettings.reasoning.effort`` first — that
    is the seam this whole feature rides, and it needs no harness migration."""
    tuned = effort_settings("What's the balance on 4B?")
    assert tuned is not None
    assert tuned.reasoning is not None
    assert tuned.reasoning.effort == "low"


def test_no_downgrade_returns_none_so_callers_can_pass_it_straight_through() -> None:
    """``None`` must mean 'use the default' rather than 'construct an empty
    override' — an empty ModelSettings would silently clear other params."""
    assert effort_settings("Should I renew this lease?") is None


def test_the_sdk_still_reads_effort_off_model_settings() -> None:
    """The pin. If an SDK bump stops forwarding reasoning.effort, this feature
    silently stops working — which is exactly how it would go unnoticed."""
    import inspect

    from agents.extensions.models.litellm_model import LitellmModel

    src = inspect.getsource(LitellmModel)
    assert "reasoning_effort" in src
    assert "model_settings.reasoning" in src
