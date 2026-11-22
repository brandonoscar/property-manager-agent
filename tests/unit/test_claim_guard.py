"""Absence claims checked against what was actually searched.

Battery v2's Q9 is the case: the agent told the user a task did not exist
"anywhere in the portfolio" at ``Confidence: likely``, while task 39431 —
"URGENT: water heater leaking into the closet" — sat in Buildium at priority
Low. The tool had shown 25 of 179 rows and the model read the 25 as the world.

These pin two things: the guard fires only on the PAIR (absence claim over a
truncated read), and when it fires it touches only the footer.
"""

from __future__ import annotations

import pytest

from helixis.agents.claim_guard import (
    TRUNCATION_MARKER,
    apply_scope_caveat,
    claims_absence,
    needs_scope_caveat,
    saw_truncated_read,
)

_FOOTER = (
    "Sources: buildium: searched open tasks\n"
    "Confidence: likely — no matching task in the list\n"
    "Unverified: none"
)


@pytest.mark.parametrize(
    "answer",
    [
        "There is no task about a water heater anywhere in the portfolio.",
        "I couldn't find a matching work order.",
        "No records match that tenant name.",
        "That task doesn't exist in Buildium.",
        "Nothing found for unit 88Z.",
    ],
)
def test_absence_shapes_are_recognised(answer: str) -> None:
    assert claims_absence(answer)


@pytest.mark.parametrize(
    "answer",
    [
        "The balance on 4B is $2,400.",
        "There are 3 open work orders at 600 Dexter Ave.",
        "No problem — I've drafted that for you.",  # 'no' but not an absence CLAIM
    ],
)
def test_ordinary_answers_are_not_absence_claims(answer: str) -> None:
    assert not claims_absence(answer)


def test_the_truncation_marker_matches_what_the_tools_actually_emit() -> None:
    """The marker is a contract with ``_truncate_footer``. If that wording
    changes, this fails rather than the guard silently never matching again."""
    from helixis.agents.buildium_tools import _truncate_footer

    emitted = _truncate_footer(total=179, limit=25)
    assert emitted is not None
    assert saw_truncated_read(emitted)
    assert TRUNCATION_MARKER in emitted
    assert _truncate_footer(total=5, limit=25) is None


def test_it_takes_both_signals_to_fire() -> None:
    """An absence claim backed by complete reads is the product working. A
    truncated read on a question claiming nothing is also fine. Only the pair
    is a defect — firing on either alone would make the caveat wallpaper."""
    absence = "No matching task anywhere in the portfolio."
    present = "The balance on 4B is $2,400."

    assert needs_scope_caveat(absence, truncated_reads=True)
    assert not needs_scope_caveat(absence, truncated_reads=False)
    assert not needs_scope_caveat(present, truncated_reads=True)


def test_a_fired_caveat_downgrades_confidence_and_records_the_scope() -> None:
    answer = f"No such task exists anywhere in the portfolio.\n\n{_FOOTER}"
    out = apply_scope_caveat(answer)

    assert "Confidence: unverified" in out
    assert "Confidence: likely" not in out
    assert "truncated" in out
    assert "Unverified: none" not in out


def test_the_answer_body_is_never_touched() -> None:
    """#271: no hard runtime gate on model prose. Downgrading a confidence
    word is reversible and legible; rewriting the answer is neither."""
    body = "No such task exists anywhere in the portfolio."
    out = apply_scope_caveat(f"{body}\n\n{_FOOTER}")
    assert out.startswith(body)


def test_an_already_honest_answer_is_left_alone() -> None:
    """An answer that reached 'unverified' on its own needs nothing from us —
    overwriting it would just churn the text."""
    answer = (
        "No matching task found.\n\n"
        "Sources: buildium: partial list\n"
        "Confidence: unverified — the list was truncated\n"
        "Unverified: the rest of the portfolio"
    )
    out = apply_scope_caveat(answer)
    assert out.count("Confidence:") == 1
    assert "unverified — the list was truncated" in out


def test_an_existing_unverified_line_is_appended_to_not_replaced() -> None:
    answer = (
        "No matching task found.\n\n"
        "Sources: buildium\n"
        "Confidence: likely — nothing in the list\n"
        "Unverified: could not check email — Gmail tool failed"
    )
    out = apply_scope_caveat(answer)
    assert "Gmail tool failed" in out
    assert "truncated" in out


def test_a_footerless_answer_still_gets_told() -> None:
    """Most turns aren't cross-source and carry no footer. The absence claim
    still needs qualifying — silently letting it stand is the Q9 failure."""
    out = apply_scope_caveat("No such task exists anywhere in the portfolio.")
    assert "truncated" in out


def test_empty_input_is_a_no_op() -> None:
    assert apply_scope_caveat("") == ""
    assert not claims_absence("")
    assert not saw_truncated_read("")


# ── absence claimed about a source that was never read ──────────────────────
#
# The 2026-08-25 live probe found the second way to assert absence without
# evidence. Verbatim, from a run that made ZERO tool calls in nine seconds:
#
#   "No documented approval-limit policy exists. Neither company Drive nor the
#    company profile has any record of staff spending/authorization limits…
#    'Hank' isn't a recognized vendor."
#
# SOP-01 was in the connected Drive the whole time, listing three spend tiers
# in §4 and dispatching plumbing to Hank the Handyman in §3. Every clause of
# that paragraph was false, and it was phrased as a completed lookup.

from helixis.agents.claim_guard import (  # noqa: E402
    apply_no_evidence_caveat,
    apply_source_caveat,
    needs_evidence_caveat,
    unsupported_source_claims,
)

#: The exact sentence that shipped. If the guard ever stops catching this,
#: the regression is the live bug, not a hypothetical.
LIVE_Q02 = (
    "No documented approval-limit policy exists. Neither company Drive nor the "
    "company profile has any record of staff spending/authorization limits, so "
    'there\'s nothing to confirm a "$1,000 limit" against.'
)
LIVE_Q06 = (
    "there's no documented approval/spending-authority policy on file for this "
    "company (no policies have been uploaded)."
)


def test_the_live_sentence_is_caught() -> None:
    assert unsupported_source_claims(LIVE_Q02, set()) == ["Google Drive"]


def test_the_live_sentence_that_names_no_source_is_caught_by_the_floor() -> None:
    """q06 generalised a Buildium miss into a claim about uploaded policies
    without naming Drive at all — so the source-scoped rule can't see it. The
    zero-evidence floor is what covers that shape."""
    assert unsupported_source_claims(LIVE_Q06, set()) == []
    assert needs_evidence_caveat(LIVE_Q06, gathered_any=False)


def test_a_source_that_was_actually_read_is_never_flagged() -> None:
    """The guard must be silent on the answer working correctly — an absence
    claim backed by a real consult is a finding, not a defect."""
    assert unsupported_source_claims(LIVE_Q02, {"drive_agent"}) == []


def test_naming_a_source_without_claiming_anything_is_spared() -> None:
    """⚠ Both halves, per gotcha 38: test what it must catch AND what it must
    spare. A guard that fires on every mention of Drive is noise, and noise
    gets ignored."""
    for benign in (
        "You could check Drive for the SOP if you want the exact wording.",
        "I found SOP-01 in Google Drive; it lists three spend tiers.",
        "The calendar shows a viewing at 2pm on Thursday.",
        "I emailed the vendor via Gmail and the thread is open.",
    ):
        assert unsupported_source_claims(benign, set()) == [], benign


def test_a_lowercase_drive_is_not_google_drive() -> None:
    """⚠ Case-SENSITIVE on the bare form, the same trick and the same reason as
    reply_hygiene's state-code rule (gotcha 38): under IGNORECASE this sentence
    reads as a claim that Google Drive is empty."""
    assert (
        unsupported_source_claims(
            "It is a short drive to the property, and no one was home.", set()
        )
        == []
    )


def test_gmail_and_calendar_are_covered_too() -> None:
    """Drive is where it was found; it is not where it is unique to."""
    assert unsupported_source_claims("I checked Gmail and found nothing from Marcus.", set()) == [
        "Gmail"
    ]
    assert unsupported_source_claims("Nothing is on the calendar for that unit.", set()) == [
        "the calendar"
    ]


def test_an_evidenced_source_does_not_excuse_an_unevidenced_one() -> None:
    """A run that read Buildium may speak for Buildium and nothing else."""
    answer = "Buildium shows six open work orders, and Drive has no SOP covering them."
    assert unsupported_source_claims(answer, {"buildium_agent"}) == ["Google Drive"]


def test_the_zero_evidence_floor_needs_both_kinds_of_emptiness() -> None:
    """A run that called tools is not a run that looked nothing up, even if no
    specialist returned. Otherwise a mirror-only answer that legitimately found
    nothing gets a caveat it doesn't deserve."""
    assert not needs_evidence_caveat(LIVE_Q02, gathered_any=True)
    assert needs_evidence_caveat(LIVE_Q02, gathered_any=False)


def test_an_answer_claiming_nothing_is_never_caveated() -> None:
    """Most turns assert no absence at all, and must pass through untouched
    even when the run genuinely read nothing."""
    assert not needs_evidence_caveat("Rent is due on the first of each month.", gathered_any=False)


def test_the_caveat_says_which_source_and_why() -> None:
    out = apply_source_caveat("Drive has no record of that policy.", ["Google Drive"])
    assert "Google Drive" in out
    assert "not successfully read on this turn" in out
    assert "Drive has no record of that policy." in out, "the body is never rewritten"


def test_two_unread_sources_read_grammatically() -> None:
    out = apply_source_caveat("x", ["Google Drive", "Gmail"])
    assert "Google Drive and Gmail" in out
    assert "they were not" in out


def test_the_source_caveat_downgrades_a_confident_footer() -> None:
    answer = (
        "Drive holds no such policy.\n\nSources: drive\nConfidence: confident\nUnverified: none"
    )
    out = apply_source_caveat(answer, ["Google Drive"])
    assert "Confidence: unverified" in out
    assert "Confidence: confident" not in out


def test_no_sources_is_a_no_op() -> None:
    assert apply_source_caveat("anything", []) == "anything"


def test_the_no_evidence_caveat_says_nothing_was_read() -> None:
    out = apply_no_evidence_caveat(LIVE_Q02)
    assert "no source was read on this turn" in out
    assert LIVE_Q02.split(".")[0] in out, "the body is never rewritten"
