"""The eval's card reader must measure what the drain actually guarantees.

WHY THIS FILE EXISTS
--------------------
``140_card_payload_is_scrubbed_and_grounded`` asserts that no raw Buildium
identifier reaches a card. ``scrub_card_payload`` is what delivers that — but
it deliberately does NOT scrub values under ``STRUCTURAL_CARD_KEYS``, and its
own comment says why for the one that matters here:

    prompt  a next-step chip's prompt is the text CLICKING IT SENDS, so
            "Lease #99797" -> "Lease" makes the follow-up unanswerable.

``_card_text`` flattened every string including those, so the case asserted a
guarantee the product does not make. A table-only roll passed; a roll that
also offered a next-steps chip failed — and ``"99797"`` is digit-dominant, so
``_is_disclosure_needle`` classes it unconditionally SAFETY: never re-rolled,
gating even a quarantined case (gotcha 65). Gotcha 56 measured that chip on
FIVE of six turns, so the coin was weighted toward the failure.

The fix is NOT to weaken the assertion — that leaves the case decorative, and
it is the only thing in the corpus reading card content at all. It is to point
it at the bytes the guarantee covers (gotcha 85: an eval must measure the
surface the user is on).

WHAT IS PINNED
--------------
1. The two exemption sets are ONE set, imported rather than retyped. Two
   copies drift (gotchas 73/96), and drifting APART in the permissive
   direction silently re-opens exactly this hole (gotcha 45).
2. The coin flip itself, end to end, through the REAL builders and the REAL
   scrubber — because a hand-built fixture would only prove the walker agrees
   with itself (gotcha 45's rule pointed at a behavioural test).
3. Both directions. A walker that skips everything passes the prohibition and
   silently breaks ``card_must_contain``; a walker that skips nothing is the
   defect (gotchas 38/51).
"""

from __future__ import annotations

import ast
import json
import pathlib

from helixis.agents.reply_hygiene import STRUCTURAL_CARD_KEYS, scrub_card_payload
from helixis.agents.visual_tools import build_next_steps_payload, build_table_payload
from helixis.evals.agent_flow import _card_text

_AGENT_FLOW = pathlib.Path("src/helixis/evals/agent_flow.py")
_GOLDEN = pathlib.Path("src/helixis/evals/golden")

# The identifier 140 forbids, and the tenant it requires. Taken from the case
# file itself below rather than trusted from here.
_CASE_ID = "140_card_payload_is_scrubbed_and_grounded"


def _case() -> dict:
    path = _GOLDEN / f"{_CASE_ID}.json"
    assert path.exists(), f"{path} — the case this file guards has moved or been renamed"
    return json.loads(path.read_text(encoding="utf-8"))


def _cards_for_a_140_shaped_run(*, with_chip: bool) -> list:
    """The real builders, through the real drain-side scrubber.

    The table is what the case asks the model for; the chip is what gotcha 56
    measured on five of six turns. Both carry the same lease number, which is
    the whole point — one of them is prose and one of them is a click target.
    """
    cards = [
        build_table_payload(
            title="Delinquent tenants",
            columns=["Tenant", "Balance"],
            rows=[
                ["Marisol Vega", "$1,240.00"],
                ["Desmond Achebe (Lease #99797)", "$412.75"],
            ],
        )
    ]
    if with_chip:
        cards.append(
            build_next_steps_payload(
                [["Draft a late notice", "Draft a late notice for Desmond Achebe on lease 99797"]]
            )
        )
    return [scrub_card_payload(card) for card in cards]


# --------------------------------------------------------------------------
# 1. One set, not two.
# --------------------------------------------------------------------------


def test_the_eval_reads_the_scrubbers_own_exemption_set() -> None:
    """Imported, never retyped — and the import is what makes them one rule.

    A local copy in ``agent_flow`` would pass every behavioural test in this
    file on the day it was written and go stale the first time somebody
    narrows the scrubber, in the direction that re-opens the gate silently.
    """
    tree = ast.parse(_AGENT_FLOW.read_text(encoding="utf-8"))

    imported_from = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and any(alias.name == "STRUCTURAL_CARD_KEYS" for alias in node.names)
    }
    assert imported_from == {"helixis.agents.reply_hygiene"}, (
        "agent_flow must import STRUCTURAL_CARD_KEYS from reply_hygiene, so the "
        f"eval and the drain cannot disagree about what is prose. Saw: {imported_from}"
    )

    # And nothing may re-declare it locally, which is how an import becomes
    # decorative without any test noticing.
    assigned = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "STRUCTURAL_CARD_KEYS" not in assigned, (
        "agent_flow re-assigns STRUCTURAL_CARD_KEYS — a second copy of a rule "
        "whose whole job is to be the only copy"
    )


def test_the_walker_actually_consults_the_set() -> None:
    """An import nothing reads is the ``comms_triage`` failure (gotcha 62).

    A behavioural test cannot tell "skipped because structural" from "this
    fixture happened not to contain it", so the call site is checked directly.
    """
    tree = ast.parse(_AGENT_FLOW.read_text(encoding="utf-8"))
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_card_text"
    )
    names = {node.id for node in ast.walk(fn) if isinstance(node, ast.Name)}
    assert "STRUCTURAL_CARD_KEYS" in names, (
        "_card_text no longer reads STRUCTURAL_CARD_KEYS — it is back to "
        "measuring bytes the drain deliberately leaves alone"
    )


# --------------------------------------------------------------------------
# 2. The coin flip, end to end.
# --------------------------------------------------------------------------


def test_a_chip_naming_the_lease_does_not_fail_the_safety_needle() -> None:
    """THE DEFECT. Same run, one extra card, opposite verdict.

    Verified against the real scrubber rather than a fixture: the table cell
    IS cleaned, so the product's guarantee holds on the surface it promises,
    and only the click target keeps the number.
    """
    case = _case()
    forbidden = (case["expected"]["card_must_not_contain"])[0]

    cleaned = _cards_for_a_140_shaped_run(with_chip=True)

    # The guarantee the case is really about: the rendered cell is clean.
    cell = cleaned[0]["rows"][1]["c0"]["value"]
    assert forbidden not in cell, f"the drain stopped scrubbing table cells: {cell!r}"

    # The exemption the case must not assert against.
    chip_prompt = cleaned[1]["steps"][0]["prompt"]
    assert forbidden in chip_prompt, (
        "the chip prompt no longer carries the identifier — if scrub_card_payload "
        "has started scrubbing 'prompt', this whole file is measuring a state that "
        "no longer exists and the exemption comment in reply_hygiene is stale"
    )

    assert forbidden not in _card_text(cleaned), (
        f"{forbidden!r} reached the eval's card text from a structural field. "
        "That is a SAFETY gate firing on a guarantee the product does not make."
    )


def test_the_table_only_roll_and_the_chip_roll_agree() -> None:
    """The tell that it was a coin flip: two rolls, one verdict, before and after.

    A fix that made BOTH rolls fail would be just as wrong as one that made
    both pass for the wrong reason, so they are compared to each other rather
    than each asserted alone.
    """
    forbidden = (_case()["expected"]["card_must_not_contain"])[0]
    without = forbidden in _card_text(_cards_for_a_140_shaped_run(with_chip=False))
    with_chip = forbidden in _card_text(_cards_for_a_140_shaped_run(with_chip=True))
    assert without == with_chip is False, (
        "offering a next-steps chip changes this case's SAFETY verdict — "
        f"table-only={without}, table+chip={with_chip}"
    )


# --------------------------------------------------------------------------
# 3. Both directions.
# --------------------------------------------------------------------------


def test_the_quality_needle_still_finds_the_tenant() -> None:
    """A walker that skips everything passes the prohibition and breaks this.

    ``card_must_contain`` is the half that says the card is actually an
    answer; a skip-everything walker turns 140 into a case that can only ever
    report the absence of things.
    """
    required = (_case()["expected"]["card_must_contain"])[0]
    text = _card_text(_cards_for_a_140_shaped_run(with_chip=True))
    assert required in text, f"{required!r} is no longer visible to the eval: {text[:200]!r}"


def test_visible_chip_text_is_still_measured() -> None:
    """The chip's LABEL is prose and is scrubbed, so it stays in scope.

    Skipping the whole card rather than the exempt keys would be the lazy fix
    and would blind the eval to every chip a model writes.
    """
    text = _card_text(_cards_for_a_140_shaped_run(with_chip=True))
    assert "Draft a late notice" in text, (
        "the chip's visible label dropped out of the eval's view — the fix "
        "skipped the card instead of the structural fields inside it"
    )


def test_an_ordinary_prose_identifier_is_still_caught() -> None:
    """THE NEGATIVE THAT MATTERS. The gate must still be able to fail.

    An unscrubbed identifier in a non-structural field is the regression this
    case exists for, and a fix that cannot see one has removed the case
    rather than corrected it.
    """
    leaky = [{"kind": "agent_table", "title": "Lease #99797 summary", "rows": []}]
    assert "99797" in _card_text(leaky), (
        "_card_text can no longer see an identifier in ordinary card prose — "
        "140 has been silently switched off"
    )


def test_the_exemption_covers_only_keys_the_scrubber_names() -> None:
    """Every exempt key must be one the scrubber genuinely leaves alone.

    Reads the SET through the scrubber rather than trusting the constant, so
    a key added to the frozenset but handled differently by the function
    cannot quietly widen what the eval refuses to look at.
    """
    for key in sorted(STRUCTURAL_CARD_KEYS):
        probe = {key: "Lease #99797"}
        assert scrub_card_payload(probe)[key] == "Lease #99797", (
            f"{key!r} is in STRUCTURAL_CARD_KEYS but the scrubber rewrites it — "
            "the eval is skipping a field that is actually cleaned, which hides "
            "a real leak rather than respecting a real exemption"
        )
