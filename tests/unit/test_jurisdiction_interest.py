"""A deposit-interest CONDITION must never render as a bare claim that
interest is owed.

Coverage item 225. ``_deposit_line`` read::

    bits.append("interest owed" if interest else "no interest owed")

and ``deposit.interest_owed`` is a bool OR a qualifying string. A non-empty
string is truthy, so Illinois' ``"required for buildings with 25+ units"``
rendered as a flat ``interest owed`` — byte-identical to Connecticut's
unconditional ``True``. **A manager with a four-unit building in Illinois was
told interest is owed on a deposit when it is not**, in a legal snapshot,
about money that belongs to a resident.

Measured against the shipped corpus before changing anything: **42 of 51
states are bool and 9 carry a string**, and every string is a NARROWING
condition — unit counts (IL 25+, NY 6+), holding periods (NH 1 year, ND 9
months, PA year 2), a deposit threshold (NM), an accounting option (FL), and
one rate rule (OH). So the collapse was wrong in the expensive direction on
all nine: it over-claims.

Same shape as gotcha 51's FORMER tenant — a qualifier the row already held,
dropped on the way to prose — pointed at a statute instead of a person.

⚠ These guards read the CORPUS, not hand-written inputs (gotcha 45). A test
feeding ``_interest_phrase`` a string it made up would pass forever while a
tenth string state added to the JSON quietly collapsed; what has to be
measured is what the shipped data actually contains. Both directions are
pinned, because rendering EVERY state conditionally would be a fresh false
claim about the 42 unconditional ones (gotcha 38's rule).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from helixis.geo.jurisdiction import (
    NOT_ENCODED,
    _interest_phrase,
    format_compact,
    lookup,
)

_RULES = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "helixis"
    / "geo"
    / "data"
    / "jurisdiction_rules.json"
)


def _states() -> dict[str, dict[str, Any]]:
    return json.loads(_RULES.read_text(encoding="utf-8"))["states"]


def _interest_values() -> dict[str, Any]:
    """Every state that encodes ``deposit.interest_owed``, keyed by abbr."""
    out: dict[str, Any] = {}
    for abbr, block in _states().items():
        deposit = block.get("deposit")
        if isinstance(deposit, dict) and "interest_owed" in deposit:
            out[abbr] = deposit["interest_owed"]
    return out


def _deposit_line_for(abbr: str) -> str:
    rendered = format_compact(lookup(abbr), property_label="Test Property")
    lines = [ln for ln in rendered.splitlines() if ln.startswith("- Deposit:")]
    assert len(lines) == 1, f"{abbr}: expected one deposit line, got {lines!r}"
    return lines[0]


def test_the_corpus_still_contains_both_shapes() -> None:
    """The scan found something, and BOTH kinds of value.

    Without this the two tests below can pass by collecting nothing — a
    renamed key or a moved nesting reads as green (gotcha 49's rule, and the
    same wrong-nesting scan that reported "zero mixed fields" while the real
    count was 42 bool / 9 str).
    """
    values = _interest_values()
    assert len(values) >= 40, f"only {len(values)} states encode interest_owed"
    bools = [v for v in values.values() if isinstance(v, bool)]
    strings = [v for v in values.values() if isinstance(v, str)]
    assert bools, "no bool interest_owed values found — is the nesting right?"
    assert strings, "no string interest_owed values found — is the nesting right?"


def test_every_string_state_renders_its_condition_verbatim() -> None:
    """The defect's own direction: a condition must reach the screen.

    Asserting on the CONDITION TEXT rather than on the word CONDITIONALLY is
    deliberate — a renderer that printed the marker and dropped the statute's
    words would satisfy a marker-only check while telling the manager nothing
    they could act on.
    """
    for abbr, value in sorted(_interest_values().items()):
        if not isinstance(value, str):
            continue
        line = _deposit_line_for(abbr)
        assert value in line, f"{abbr}: condition {value!r} missing from {line!r}"
        assert "CONDITIONALLY" in line, f"{abbr}: condition not marked in {line!r}"


def test_unconditional_states_are_not_marked_conditional() -> None:
    """The OTHER direction, which is how this fix could go wrong.

    Marking all 51 states conditional would close the reported gap and open a
    fresh one: 42 states owe interest unconditionally or not at all, and
    hedging those is a false claim in the opposite direction (gotcha 38 — test
    the thing a guard must catch AND the thing it must spare).
    """
    checked = 0
    for abbr, value in sorted(_interest_values().items()):
        if not isinstance(value, bool):
            continue
        line = _deposit_line_for(abbr)
        assert "CONDITIONALLY" not in line, f"{abbr}: bool {value} hedged in {line!r}"
        expected = "interest owed" if value else "no interest owed"
        assert expected in line, f"{abbr}: expected {expected!r} in {line!r}"
        checked += 1
    assert checked >= 40, f"only {checked} bool states exercised"


def _rendered_interest_phrase(abbr: str) -> str:
    """The interest BIT of a state's deposit line, with its citation removed.

    ⚠ **STRIPPING THE CITATION IS THE WHOLE POINT, and the first cut of this
    test did not.** It compared everything after the word "penalty", which for
    Connecticut ends ``(C.G.S. §47a-21)`` and for Illinois ``(765 ILCS 710)``
    — so the two strings differed on the STATUTE NUMBER whatever the interest
    phrasing said, and the test passed against the very defect it names. The
    mutation battery is what surfaced it: reverting to the pre-fix ternary
    failed one test where it should have failed two (gotcha 90's family — a
    verdict that reads as coverage and is not).
    """
    line = _deposit_line_for(abbr)
    citation = (_states()[abbr].get("deposit") or {}).get("citation")
    if citation:
        line = line.replace(f"({citation})", "")
    segments = [seg.strip() for seg in line.split(";") if "interest owed" in seg]
    assert len(segments) == 1, f"{abbr}: expected one interest segment in {line!r}"
    return segments[0]


def test_a_true_state_and_a_string_state_do_not_render_alike() -> None:
    """The DISTINGUISHING assertion — the two tests above can both pass while
    the two cases still read identically to a model summarising the row.

    Connecticut is unconditional ``True``; Illinois is ``25+ units``. Under the
    pre-fix ternary both rendered the bare words ``interest owed``, which is
    exactly the harm: nothing on screen told the Illinois manager their
    building was under the threshold.
    """
    values = _interest_values()
    assert values.get("CT") is True, "fixture drift: CT is no longer unconditional"
    assert isinstance(values.get("IL"), str), "fixture drift: IL is no longer a string"

    assert _rendered_interest_phrase("CT") != _rendered_interest_phrase("IL")


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_condition_is_unknown_rather_than_no(blank: str) -> None:
    """Blank must not answer a question the data declines to answer.

    An empty string is falsy, so the old expression rendered it as
    ``no interest owed`` — an assertion the corpus does not support, on the
    side where being wrong costs a tenant money (gotchas 55/57's
    decline-to-assert).
    """
    out = _interest_phrase(blank)
    assert NOT_ENCODED in out
    assert "no interest owed" not in out


def test_interest_owed_is_still_the_only_mixed_field() -> None:
    """A SECOND mixed bool/str field would be a second silent collapse.

    ``_interest_phrase`` handles one field. Any other field that starts
    carrying a qualifying string gets rendered by a plain truthiness check
    somewhere else in this module, which is exactly the defect this file
    exists for — so a new one has to fail here and be looked at rather than
    ship quietly. Scanned rather than assumed, because the audit note's own
    scan reported zero while the real count was nine.
    """
    types: dict[tuple[str, str], set[str]] = {}
    for block in _states().values():
        for group_name, group in block.items():
            if not isinstance(group, dict):
                continue
            for field_name, value in group.items():
                if isinstance(value, (dict, list)):
                    continue
                types.setdefault((group_name, field_name), set()).add(type(value).__name__)

    mixed = {k for k, seen in types.items() if {"bool", "str"} <= seen}
    assert types, "scanned no fields — is the nesting right?"
    assert mixed == {("deposit", "interest_owed")}, (
        f"a new mixed bool/str field appeared: {sorted(mixed)}. Whatever renders "
        "it needs the _interest_phrase treatment, or it collapses a condition "
        "into a bare claim the way deposit.interest_owed did."
    )
