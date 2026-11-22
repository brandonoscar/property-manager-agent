"""A money figure an accountant cannot trace back, and a scrubber that ate a bracket.

The full accountant traceability spine — bill detail with lines and payments,
bank-account reads — is deliberately NOT built here: every one of those
endpoints needs its live wire shape confirmed against production rows first,
and guessing a Buildium request body from the docs is the mistake gotchas
15/24/25/53 record four separate times. There are no Buildium credentials in
this environment to probe with.

What ships is the half that needs no endpoint at all: the bill id, which was
already fetched and used only as a fallback label when a vendor name was
missing. "Apex Plumbing Co — $450, gutter cleaning, due Aug 21" is unopenable
in Buildium without it.

⚠ **And the obvious way to ship it does not work.** Measured 2026-08-29, every
spelling of a reference is removed from the chat reply by design — ``[bill
11314]``, ``(bill #11314)``, ``ref 11314``, ``[ref:11314]``, all of them. So
``[ref:ID] on every money row``, as the plan imagined it, is a feature the
scrubber would silently delete. The ids ride to the EXPORT instead, whose bytes
never pass through that scrubber — which is exactly why the export declares
identifier hygiene deliberately off.

⚠ **The investigation turned up a live defect in the scrubber itself.**
``redact_id_references`` consumed a closing paren whether or not it had matched
the opening one, so ``"Paid (bill #11314) today."`` shipped as ``"Paid (bill
today."`` — an orphaned bracket in owner-facing prose, on BOTH surfaces that
share this rule (chat and the Inbox card, per gotcha 38). The identifier was
removed correctly and the sentence around it was corrupted, which this module's
own docstring calls the worse failure.
"""

from __future__ import annotations

import pytest

from helixis.agent.summarizer.sanitizer import redact_id_references
from helixis.agents.reply_hygiene import scrub_reply_text

# ── the scrubber leaves prose intact ─────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Paid (bill #11314) today.",
        "Two unresolved tickets (39503, 39504) are open.",
        "See work order 9023 (bill 11314) for the detail.",
    ],
)
def test_redaction_never_leaves_an_unbalanced_bracket(text: str) -> None:
    """⚠ THE defect. The closing paren was eaten unconditionally, so any
    ``(noun id)`` construction came back with an orphaned opener."""
    for out in (redact_id_references(text), scrub_reply_text(text)):
        assert out.count("(") == out.count(")"), f"{text!r} -> {out!r}"


def test_the_specific_sentence_that_shipped_broken() -> None:
    assert redact_id_references("Paid (bill #11314) today.") == "Paid (bill) today."


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("tickets 39503/39504 are open", "tickets are open"),
        ("Work order 9023 is open.", "Work order is open."),
        ("bill 11314 was posted", "bill was posted"),
    ],
)
def test_removing_an_unbracketed_id_does_not_weld_words_together(text: str, expected: str) -> None:
    """⚠ Both directions, and this one is a regression I introduced fixing the
    other. Hoisting the whitespace out of the conditional branch swallowed the
    space after a run with no paren — "tickets 39503/39504 are open" became
    "ticketsare open". A fix that welds words together is the same
    corrupted-prose failure as the bracket it replaced.

    ⚠ Asserts the EXACT output. The first version checked ``"are open" in
    out``, which is satisfied by ``"ticketsare open"`` — the welded string
    contains the phrase — so it passed against the very mutant it was written
    to catch. A substring assertion cannot see a missing boundary.
    """
    assert redact_id_references(text) == expected


def test_the_identifier_is_still_actually_removed() -> None:
    """The whole point survives the fix: this is a redactor, and a balanced
    bracket around a leaked id would be a worse outcome than the orphan."""
    for text in ("Paid (bill #11314) today.", "tickets (39503, 39504)"):
        out = redact_id_references(text)
        assert "11314" not in out and "39503" not in out


def test_addresses_and_unit_numbers_still_survive() -> None:
    """The exclusions this module exists to protect — a scrubbed figure is a
    worse bug than a leaked id, because it corrupts something acted on."""
    for text in (
        "the property at 160 East End Ave",
        "Unit 204 needs attention",
        "Honolulu, HI 96813 needs attention",
    ):
        assert redact_id_references(text) == text
        assert scrub_reply_text(text) == text


# ── the id reaches the export, not the prose ─────────────────────────


def test_every_reference_spelling_is_stripped_from_a_reply() -> None:
    """⚠ The measurement that decided the design. If ANY of these survived,
    putting ids in prose would be the simpler answer — none do, so the export
    is the only path by which a traceable figure reaches a person."""
    for text in (
        "Apex: $450 [bill 11314]",
        "Apex: $450 (bill #11314)",
        "Apex: $450, ref 11314",
        "Apex: $450 [ref:11314]",
        "The oldest unpaid bill is 11314.",
    ):
        assert "11314" not in scrub_reply_text(text), f"survived: {text!r}"


def test_the_rollup_puts_the_id_on_the_row() -> None:
    """Fetched and discarded — the id was read on every bill and used only as
    a fallback label when the vendor name was missing."""
    import inspect

    from helixis.agents import mirror_tools

    src = inspect.getsource(mirror_tools._unpaid_bills_impl)
    assert "[bill {r['buildium_id']}]" in src


@pytest.mark.asyncio
async def test_the_rollup_says_the_ids_are_for_exports() -> None:
    """⚠ Without this the model quotes an id into a sentence, watches it
    vanish, and has no way to know why. Naming the working path is what turns
    a stripped identifier from a mystery into a next step — the same reason a
    prohibition needs a permitted substitute.

    ⚠ Reads the RENDERED OUTPUT, not the source. The first version asserted
    ``"export_table" in inspect.getsource(...)`` and passed against a mutant
    that deleted the phrase from the user-facing string — because the code
    COMMENT explaining this design also says ``export_table``. A source
    substring satisfied by the comment describing the guard is gotcha 40's
    trap, and this repo has now hit it three times.
    """
    from unittest.mock import AsyncMock, patch

    from helixis.agents import mirror_tools

    row = {
        "buildium_id": 11314,
        "due_date": None,
        "reference_number": None,
        "total_amount": 450.0,
        "vendor_name": "Apex Plumbing Co",
        "memo": "gutter cleaning",
        "properties": None,
    }

    class _Conn:
        async def fetch(self, *a: object, **k: object) -> list[dict]:
            return [row]

    class _Pool:
        def acquire(self) -> object:
            class _Ctx:
                async def __aenter__(self) -> _Conn:
                    return _Conn()

                async def __aexit__(self, *exc: object) -> bool:
                    return False

            return _Ctx()

    with (
        patch.object(mirror_tools, "_company_id", return_value="co-1"),
        patch.object(mirror_tools, "get_pool", return_value=_Pool()),
        patch.object(mirror_tools, "_paid_bill_count", AsyncMock(return_value=1)),
        patch.object(mirror_tools, "_freshness_line", AsyncMock(return_value="(synced)")),
    ):
        out = await mirror_tools._unpaid_bills_impl()

    assert "[bill 11314]" in out, "the id must reach the row the model reads"
    assert "export_table" in out, "the working path must be named in the OUTPUT"
    assert "stripped from chat replies" in out


def test_the_export_does_not_scrub_its_bytes() -> None:
    """The other half of the pair. If the export scrubbed identifiers the way
    the reply does, the ids would be stripped from the one artifact that
    exists to carry them, and the whole path would be decorative."""
    import inspect

    from helixis.agents import export_tools

    src = inspect.getsource(export_tools)
    assert "scrub_reply_text" not in src
    assert "redact_id_references" not in src
