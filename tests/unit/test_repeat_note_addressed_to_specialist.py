"""The de-duplication note is read by a specialist, so it must not lie to one.

The `CallLedger` is a `ContextVar`. It propagates into `.as_tool()` sub-runs,
so every refusal it writes lands in whichever model is running — and the memo
note was phrased for the orchestrator alone:

    "(Repeat call - identical to one you already made this turn, so this is
     the same answer, not a fresh lookup. You already have this; move on.)"

From inside a nested consult both claims are FALSE. The specialist's call is a
repeat only of the *orchestrator's*, which it never saw and does not have. A
live specialist said so and acted on it (2026-08-29, work-order reassign):

    "the tool results just now included text claiming these were 'repeat
     calls' I'd 'already made this turn' ... That instruction was embedded
     inside the tool output, not from you or the system - it's not a
     legitimate signal, and it was false (these were my first calls this
     turn). I disregarded it"

It classified our own cache note as prompt injection, disregarded it, re-ran
the reads, and the run died on the soft deadline having reached no answer.

⚠ **Distrusting an unattributed imperative in tool output is CORRECT.** That
is the behaviour the 400-series injection cases exist to produce, and this
test must never be satisfied by making the model trust it more. The fix is to
stop writing an imperative: attribute the speaker, scope the claim to the RUN
rather than to the reader, and state a fact instead of giving an order.

This is gotcha 39's shape — a message written for one reader landing on
another — which was fixed for the soft-deadline text and never applied here.
"""

from __future__ import annotations

import re

from helixis.agents.call_budget import _REPEAT_NOTE

_NOTE = _REPEAT_NOTE.lower()


def test_it_does_not_assert_the_reader_made_the_call() -> None:
    """The false half. A specialist's first call is not one *it* already made,
    and telling it otherwise is a claim it can check and disprove."""
    for claim in ("you already made", "you already have this", "your own earlier"):
        assert claim not in _NOTE, (
            f"{claim!r} is false inside a specialist sub-run -- the ledger is a "
            "ContextVar and counts the ORCHESTRATOR's calls"
        )


def test_it_does_not_issue_a_bare_instruction() -> None:
    """The injection-shaped half. An unattributed imperative arriving in tool
    output is what a model is right to refuse; 'move on' told it to stop
    checking, from no identified speaker."""
    for order in ("move on", "don't check", "do not check", "skip the"):
        assert order not in _NOTE, (
            f"{order!r} is an unattributed imperative in tool output -- exactly "
            "the shape a specialist should distrust, and did"
        )


def test_it_names_the_speaker_and_scopes_the_claim_to_the_run() -> None:
    """Both halves of the fix, and the reason the text can be believed: it says
    who is talking, and that the earlier call may have been someone else's."""
    assert "occupella" in _NOTE, "an unattributed note cannot be told from an injected one"
    assert re.search(r"\brun\b", _NOTE), "the cache must be scoped to the RUN, not to the reader"
    assert "not necessarily by you" in _NOTE or "possibly by the agent" in _NOTE, (
        "it must allow that the earlier call was the caller's, or it is making "
        "the same false claim in gentler words"
    )


def test_it_says_nothing_is_being_withheld() -> None:
    """A cached result that reads as a REFUSAL gets worked around. The live
    specialist re-ran the reads; saying plainly that the data is the same is
    what removes the reason to."""
    assert "same data" in _NOTE or "nothing is being withheld" in _NOTE


def test_it_is_still_a_parenthetical_note_not_a_new_turn_of_prose() -> None:
    """It rides on the END of a real tool result. If it grows into a paragraph
    it starts competing with the data it annotates."""
    assert _REPEAT_NOTE.startswith("\n\n(") and _REPEAT_NOTE.rstrip().endswith(")")
    assert len(_REPEAT_NOTE) < 500, "the note is annotation, not content"
