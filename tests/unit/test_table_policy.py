"""A table beside a verdict is surplus. Pin when it is refused and when it isn't.

The live answer this came from was correct — "no, they're open, and the window
is 14 days" — and then rendered a ten-row table underneath it that answered
nothing. ``show_table``'s docstring already told the model when to use it; the
model read it, had rows in hand, and emitted one anyway. Hence a structural
refusal, and hence these tests.

⚠ The first cut of the classifier passed NINE hand-written cases and failed the
one from production, because the real question buried its ask at the end
("Marcus emailed saying … isn't really a concern. **Is he right?**") while the
pattern was anchored to the start of the string. That case is first here, by
name, so the regression can never be subtle.
"""

from __future__ import annotations

import pytest

from helixis.agents.table_policy import (
    REFUSAL,
    bind_table_policy,
    classify_table_need,
    reset_table_policy,
    tables_are_surplus,
)

#: Verbatim from the 2026-08-25 chat that motivated the module.
THE_REAL_QUESTION = (
    "Marcus emailed saying he closed out the ceiling stain work orders last "
    "Thursday, and that our escalation window is 30 days so the backlog isn't "
    "really a concern. Is he right?"
)


def test_the_question_from_production_is_refused() -> None:
    """The one case that matters. It failed the first implementation."""
    assert classify_table_need(THE_REAL_QUESTION) is False


@pytest.mark.parametrize(
    "question",
    [
        "Is that invoice correct?",
        "Should we file on Riverside 101?",
        "Did Marcus close those tickets?",
        "Do we have a policy on this?",
        "What do we do about the hot water complaint?",
        "Why is this still open?",
        "Can we charge a pet fee here?",
        "Was the vendor insured when we dispatched them?",
        # The ask trailing a paragraph of context — the shape that broke v1.
        "I looked at the ledger this morning. Should we escalate?",
        "Dana says no hot water at Maple Court since Friday. What do we do?",
    ],
)
def test_judgment_questions_refuse_a_table(question: str) -> None:
    assert classify_table_need(question) is False


@pytest.mark.parametrize(
    "question",
    [
        "List the open work orders",
        "Show me every unpaid bill",
        "Which units are behind on rent?",
        "How many leases expire this quarter?",
        "Give me a breakdown of maintenance spend by property",
        "Compare the two invoices",
        "All the open work orders please",
        "Rank the properties by open work orders",
    ],
)
def test_list_questions_keep_their_table(question: str) -> None:
    assert classify_table_need(question) is None


@pytest.mark.parametrize(
    "question",
    [
        # ⚠ The exact question used to validate chat_probe, 2026-08-25.
        "Which lease needs attention most urgently, and why?",
        "Is it worth replacing, and why?",
        "What happened here and why?",
    ],
)
def test_a_trailing_why_is_a_judgment(question: str) -> None:
    """⚠ Found by the first REAL question, after eleven synthetic ones passed.

    Two independent reasons this slipped: the alternation was ``why\\s+``,
    which requires trailing whitespace and so never matched "why?" at all
    (gotcha 30's family — a whitespace assumption in a VERBOSE pattern that
    still compiles and still matches something); and ``_JUDGMENT`` is anchored
    to sentence boundaries, while "…, and why?" follows a COMMA, so no anchor
    could reach it even once the whitespace was fixed.

    Two separate defects producing one silent miss is exactly why the fix is a
    dedicated pattern rather than another alternative inside the anchored one.
    """
    assert classify_table_need(question) is False


@pytest.mark.parametrize(
    "question",
    [
        "List the open work orders and why each is stuck",
        "Show me which units are behind, and why",
    ],
)
def test_a_list_request_still_wins_over_a_trailing_why(question: str) -> None:
    """The asymmetry must survive the new rule: an explicit row request keeps
    its table even when the sentence also asks why."""
    assert classify_table_need(question) is None


def test_a_list_request_outranks_a_judgment_opener() -> None:
    """⚠ The asymmetry, stated as a test.

    "Is he right about which units are behind?" is judgment-shaped AND
    list-shaped. The list wins: a wrongly-suppressed table costs one follow-up
    question, a wrongly-emitted one costs every reader of every judgment answer
    a paragraph of noise. Suppression is the more expensive mistake to make
    wrongly, so it needs the clearer signal.
    """
    assert classify_table_need("Is he right about which units are behind?") is None


@pytest.mark.parametrize(
    "question",
    ["Draft a note to the vendor", "Riverside Townhomes 101", "", "Tell me about Garden Row"],
)
def test_unrecognised_questions_get_no_opinion(question: str) -> None:
    """``None`` means "say nothing", so an unbound run is byte-for-byte
    unchanged. That narrowness is what lets this ship without a tuning pass —
    the same contract ``classify_effort`` uses."""
    assert classify_table_need(question) is None


def test_nothing_is_bound_until_a_question_is_recognised() -> None:
    token = bind_table_policy("Show me the open work orders")
    try:
        assert token is None, "a list question must not bind anything"
        assert tables_are_surplus() is False
    finally:
        reset_table_policy(token)


def test_binding_and_resetting_is_symmetric() -> None:
    assert tables_are_surplus() is False
    token = bind_table_policy(THE_REAL_QUESTION)
    try:
        assert token is not None
        assert tables_are_surplus() is True
    finally:
        reset_table_policy(token)
    assert tables_are_surplus() is False, "the policy must not leak past the run"


@pytest.mark.asyncio
async def test_the_tool_actually_refuses_under_the_policy() -> None:
    """Reads BOTH sides — the classifier AND the tool that consults it.

    Testing them separately is what let the ``next_steps`` enum bug reach
    production (gotcha 28): each half was well covered, and nothing exercised
    the seam between them.
    """
    from helixis.agents.visual_tools import _show_table_impl

    token = bind_table_policy(THE_REAL_QUESTION)
    try:
        result = await _show_table_impl("Ceiling work orders", ["Ref", "Status"], [["1", "New"]])
        assert result == REFUSAL
    finally:
        reset_table_policy(token)


@pytest.mark.asyncio
async def test_the_tool_still_works_for_a_list_question() -> None:
    from helixis.agents.visual_tools import _show_table_impl

    token = bind_table_policy("Show me every open work order")
    try:
        result = await _show_table_impl("Open work orders", ["Ref", "Status"], [["1", "New"]])
        assert result != REFUSAL
    finally:
        reset_table_policy(token)


def test_the_refusal_says_what_to_do_instead() -> None:
    """A bare "no" invites a retry, which costs a model turn and lands in the
    same place (gotcha 32: a refusal is free to the ledger, expensive on the
    clock)."""
    assert "prose" in REFUSAL.lower()
    assert "judgment" in REFUSAL.lower()
