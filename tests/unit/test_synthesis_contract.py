"""SYNTHESIZE footer contract — parser/validator + prompt pins (W2 slice 2).

F6 (calibrated uncertainty) and F7 (receipts) land as a machine-readable
three-line footer. These tests pin the parser semantics, the validator's
violation set, and the prompt phrases that teach the contract (plus the folded
F3 hypothesis and F4 agreement-as-evidence lines).
"""

from __future__ import annotations

from helixis.agents.orchestrator import REASONING_LOOP_ADDENDUM
from helixis.agents.synthesis_contract import (
    CONFIDENCE_VOCABULARY,
    parse_synthesis_footer,
    validate_synthesis,
)

_GOOD = """The tenant owes $2,400 and has promised to pay by Friday.

Sources: buildium: $2,400 past due on unit 4B; gmail: Bob's Jul 14 promise-to-pay
Confidence: likely — the promise is stated but no payment has posted yet
Unverified: tenant's claim that a partial payment was mailed
"""


# ── parser ───────────────────────────────────────────────────────


def test_well_formed_footer_parses() -> None:
    f = parse_synthesis_footer(_GOOD)
    assert f.well_formed
    assert f.sources == [
        "buildium: $2,400 past due on unit 4B",
        "gmail: Bob's Jul 14 promise-to-pay",
    ]
    assert f.confidence == "likely"
    assert "no payment has posted" in f.confidence_reason
    assert f.unverified == ["tenant's claim that a partial payment was mailed"]


def test_unverified_none_parses_to_empty() -> None:
    text = (
        "Answer.\n\nSources: buildium: balance\n"
        "Confidence: confident — corroborated\nUnverified: none\n"
    )
    f = parse_synthesis_footer(text)
    assert f.well_formed
    assert f.unverified == []


def test_last_footer_wins_over_quoted_example() -> None:
    # A reply that QUOTES an example footer earlier must still parse the real
    # one at the end.
    text = (
        'Earlier I showed the format ("Confidence: confident — example").\n\n'
        "Real answer here.\n\n"
        "Sources: gmail: the owner's reply\n"
        "Confidence: unverified — single uncorroborated source\n"
        "Unverified: owner's claimed approval\n"
    )
    f = parse_synthesis_footer(text)
    assert f.confidence == "unverified"
    assert f.sources == ["gmail: the owner's reply"]


def test_missing_footer_is_not_well_formed() -> None:
    f = parse_synthesis_footer("Just a plain answer with no footer.")
    assert not f.well_formed
    assert f.confidence is None and f.sources == []


# ── validator ────────────────────────────────────────────────────


def test_valid_answer_has_no_violations() -> None:
    assert validate_synthesis(_GOOD) == []


def test_each_missing_line_is_a_named_violation() -> None:
    v = validate_synthesis("No footer at all.")
    joined = " ".join(v)
    assert "Sources" in joined and "Confidence" in joined and "Unverified" in joined
    assert len(v) == 3


def test_out_of_vocabulary_confidence_flagged() -> None:
    text = _GOOD.replace("Confidence: likely —", "Confidence: definitely —")
    v = validate_synthesis(text)
    assert any("not in" in x for x in v)
    assert "definitely" in " ".join(v)


def test_confidence_without_reason_flagged() -> None:
    text = _GOOD.replace(
        "Confidence: likely — the promise is stated but no payment has posted yet",
        "Confidence: likely",
    )
    v = validate_synthesis(text)
    assert any("no reason clause" in x for x in v)


def test_vocabulary_is_the_three_calibration_words() -> None:
    assert CONFIDENCE_VOCABULARY == ("confident", "likely", "unverified")


# ── prompt pins (the contract is taught, not just parsed) ────────


def test_addendum_teaches_the_footer() -> None:
    for label in ("Sources:", "Confidence:", "Unverified:"):
        assert label in REASONING_LOOP_ADDENDUM, label
    for word in CONFIDENCE_VOCABULARY:
        assert word in REASONING_LOOP_ADDENDUM, word
    # Unsourced material claims are named a defect (F7 enforcement seed).
    assert "defect" in REASONING_LOOP_ADDENDUM


def test_addendum_folds_in_hypothesis_and_corroboration() -> None:
    text = REASONING_LOOP_ADDENDUM.lower()
    # F3: a working hypothesis with kill-criteria, held during ANALYZE.
    assert "working hypothesis" in text
    assert "confirm\n   or kill" in text or "confirm or kill" in text.replace("\n   ", " ")
    # F4: agreement stated as evidence + verify-before-trust.
    assert "corroboration is evidence" in text
    assert "second source" in text
