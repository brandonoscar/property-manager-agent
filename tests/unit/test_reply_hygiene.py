"""The chat reply must not leak Buildium record numbers — or eat real figures.

#892 and #893 fixed identifier hygiene for Inbox cards. Nobody checked the
chat surface, so it had none. The strings below are VERBATIM from a live chat
answer on 2026-08-25; they are pinned as literals rather than paraphrased,
because a paraphrase is a guess about the defect and these are the defect.

Two failure directions, and the second is worse:

  * a leaked identifier — ugly, exposes internals
  * a SCRUBBED FIGURE — silently corrupts a number the reader acts on

So the negative cases carry as much weight as the positive ones, and
``$11,314.00`` is in there deliberately: same digits as work order 11314.
"""

from __future__ import annotations

import re

import pytest

from helixis.agents import reply_hygiene
from helixis.agents.reply_hygiene import StreamScrubber, scrub_reply_text

# ── verbatim from the live reply ─────────────────────────────────────

LEAKED = [
    'the 8 I could pull individually all show Status = "New" '
    "(IDs 11314, 11320, 11325, 11330, 11337, 11342, 11350, 11356)",
    "the remaining 2 (11362, 11369) still appear on the current open-work-order list",
    "Buildium Agent — pulled live status on all 10 ceiling-stain work orders",
]

#: Every identifier that appeared in that reply. None may survive.
LEAKED_IDS = [
    "11314",
    "11320",
    "11325",
    "11330",
    "11337",
    "11342",
    "11350",
    "11356",
    "11362",
    "11369",
]


@pytest.mark.parametrize("text", LEAKED)
def test_no_record_number_survives(text: str) -> None:
    out = scrub_reply_text(text)
    for bad in LEAKED_IDS:
        assert bad not in out, f"{bad!r} survived in {out!r}"


def test_the_prose_still_reads() -> None:
    """Redaction must leave a sentence, not a wreck of orphaned punctuation."""
    out = scrub_reply_text(LEAKED[1])
    assert out == "the remaining 2 still appear on the current open-work-order list"


# ── the direction that matters more ──────────────────────────────────

MUST_SURVIVE = [
    "balance is $4,500 against rent of $1,800",
    "lease ends 2026-08-31",
    "10 work orders, 8 checked",
    "call (555) 010-2288",
    "invoice total $1,637.70",
    "the lines sum to $750.50, not $1,290.00",
    "88 days",
    "unit 4B",
    "SOP-01 v3.1 supersedes v2.0",
    "(2026, 2027)",
    "$11,314.00 owed",  # same digits as WO 11314 — the $ is what saves it
    "third call in 14 months",
    "roughly 900 sq ft",
    "TCPA 08:00-21:00",
]


@pytest.mark.parametrize("text", MUST_SURVIVE)
def test_real_figures_are_untouched(text: str) -> None:
    """A scrubbed figure is a worse bug than a leaked id: it silently changes
    a number the reader will act on, and nothing downstream can tell."""
    assert scrub_reply_text(text) == text


def test_newlines_and_structure_survive() -> None:
    """Paragraph and list structure is load-bearing for how the reply renders."""
    text = "Findings:\n\n- item one (11314)\n- item two\n\nBottom line: two open.\n"
    out = scrub_reply_text(text)
    assert "11314" not in out
    assert out.count("\n") == text.count("\n")
    assert out.startswith("Findings:\n\n- item one")
    assert out.endswith("Bottom line: two open.\n")


# ── the streaming half ───────────────────────────────────────────────


def test_a_split_identifier_is_caught() -> None:
    """⚠ The bug the whole class exists for.

    The reply streams token by token, so an identifier arrives in pieces.
    Verified below: scrubbing each delta independently passes 11314 straight
    through AND mangles the surrounding text — worse than not scrubbing.
    """
    deltas = ["I checked ", "work order 11", "314 and ", "tickets (113", "20, 11325) for you"]

    naive = "".join(scrub_reply_text(d) for d in deltas)
    assert "11314" in naive, "guard is broken if the naive path no longer leaks"

    sc = StreamScrubber()
    streamed = "".join(sc.push(d) for d in deltas) + sc.flush()
    assert "11314" not in streamed
    assert "11320" not in streamed
    assert "11325" not in streamed
    assert streamed == "I checked work order and tickets for you"


def test_streamed_output_equals_whole_text_output() -> None:
    """Streaming a reply must produce exactly what scrubbing it whole would."""
    full = (
        'I checked Buildium for the 10 work orders. The 8 I pulled show Status = "New" '
        "(IDs 11314, 11320, 11325, 11330, 11337, 11342, 11350, 11356), and the remaining "
        "2 (11362, 11369) appear on the open list. Balance $4,500, lease ends 2026-08-31."
    )
    for size in (1, 3, 7, 64, 1000):
        sc = StreamScrubber()
        streamed = "".join(sc.push(full[i : i + size]) for i in range(0, len(full), size))
        streamed += sc.flush()
        assert streamed == scrub_reply_text(full), f"chunk size {size}"


def test_multiline_stream_matches_whole_text_at_every_chunk_size() -> None:
    """⚠ The test that caught TWO shipped-looking bugs, both invisible on a
    single-line input: the prefix-index desync, and the whitespace split that
    landed inside an identifier run. It asserts across nine chunk sizes
    because each bug only showed at some of them."""
    multi = (
        "Findings for the ceiling work orders:\n\n"
        "- eight are open (IDs 11314, 11320, 11325, 11330, 11337, 11342, 11350, 11356)\n"
        "- two more (11362, 11369) appear on the list but were not checked\n\n"
        "Balance $4,500, rent $1,800, lease ends 2026-08-31.\n"
    )
    for size in (1, 2, 3, 5, 7, 13, 64, 200, 5000):
        sc = StreamScrubber()
        out = "".join(sc.push(multi[i : i + size]) for i in range(0, len(multi), size))
        out += sc.flush()
        assert out == scrub_reply_text(multi), f"chunk {size}"
        for bad in LEAKED_IDS:
            assert bad not in out, f"{bad} leaked at chunk {size}"
        assert "$4,500" in out and "$1,800" in out and "2026-08-31" in out


def test_a_reply_with_no_newline_at_all_is_still_safe() -> None:
    """A short single-line answer never triggers a line emit, so everything
    rides on flush. Nothing may escape on that path either."""
    sc = StreamScrubber()
    one = "The eight open ones are IDs 11314, 11320 and the balance is $4,500"
    out = "".join(sc.push(one[i : i + 3]) for i in range(0, len(one), 3)) + sc.flush()
    assert "11314" not in out and "11320" not in out
    assert "$4,500" in out


def test_scrub_is_idempotent() -> None:
    text = "work order 11314 and tickets (11320, 11325) — balance $4,500"
    once = scrub_reply_text(text)
    assert scrub_reply_text(once) == once


def test_empty_and_flush_only() -> None:
    sc = StreamScrubber()
    assert sc.push("") == ""
    assert sc.flush() == ""
    assert scrub_reply_text("") == ""


def test_short_stream_emits_only_on_flush() -> None:
    """No newline means no complete line, so it all arrives at flush.
    Correct, not a stall."""
    sc = StreamScrubber()
    assert sc.push("all clear") == ""
    assert sc.flush() == "all clear"


def test_tech_vocabulary_is_deliberately_left_alone() -> None:
    """Documents the boundary. ``Status = "New"`` is equally wrong in a reply,
    but rewriting it means changing meaning, and a regex doing that on a token
    stream mangles sentences. That half is a prompt rule + a golden case."""
    text = 'the 8 I pulled show Status = "New"'
    assert 'Status = "New"' in scrub_reply_text(text)


def test_patterns_compile_under_verbose_without_eating_spaces() -> None:
    """⚠ gotcha 30: ``re.VERBOSE`` silently deletes literal spaces, so a
    pattern that looks right matches nothing. These use ``\\s`` classes; this
    asserts they still match text with real spaces in it."""
    assert reply_hygiene._ID_LABELLED_RUN.search("reference number 11314")
    assert reply_hygiene._ID_LABELLED_RUN.search("IDs 11314, 11320")
    assert re.search(r"\\s", reply_hygiene._ID_LABELLED_RUN.pattern) or True


# ── a postcode is not a record number ───────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        # ⚠ THE ONE THAT SHIPPED BROKEN. A US ZIP is five digits and so is a
        # Buildium record number, so length cannot separate them. This came
        # back as "Honolulu, HI needs attention" — the postcode deleted from
        # an address the reader acts on.
        #
        # It hid because a ZIP at the END of a sentence is followed by ".",
        # already in the trailing exclusion, so every hand-written example
        # happened to be safe and only the mid-sentence case failed.
        "Unit at 415 S Beretania St, Honolulu, HI 96813 needs attention.",
        "Mail it to Honolulu HI 96813, attention Marcus.",
        "Zip code 96813 covers the whole block.",
        "Postal code 96813 is correct on the lease.",
        # A bare run followed by a unit word is a QUANTITY, not an identifier.
        "The balance is 12500 dollars.",
        "That unit is 12500 sq ft.",
    ],
)
def test_real_figures_survive(text: str) -> None:
    """This module's stated principle is precision over recall: scrubbing a
    figure is a worse bug than leaking an id, because it silently corrupts
    something the reader will act on. A rule that fails its own principle
    cannot stay."""
    assert scrub_reply_text(text) == text


@pytest.mark.parametrize(
    ("text", "gone"),
    [
        ("Marcus closed the work orders (IDs 11314, 11320, 11325).", "11314"),
        ("Property 4610 has 10 open work orders.", "4610"),
        ("the remaining 2 (11362, 11369) still appear on the list", "11362"),
        ("they all show 11314 as open", "11314"),
    ],
)
def test_the_postal_guard_did_not_buy_precision_with_recall(text: str, gone: str) -> None:
    """The other half of the same change. A guard that also silenced the real
    leaks would be a regression wearing a fix's clothes."""
    assert gone not in scrub_reply_text(text)


@pytest.mark.parametrize(
    ("text", "gone"),
    [
        # ⚠ Found by the INBOX test, not by any chat case: an identifier that
        # ends a sentence was never scrubbed at all. "Re 11314 and 11320."
        # came back "Re and 11320." — one stripped, one kept, in one line.
        ("The work order is 11320.", "11320"),
        ("Re 11314 and 11320.", "11320"),
        ("Everything hinges on 11325!", "11325"),
        ("Is it 11325?", "11325"),
    ],
)
def test_an_identifier_ending_a_sentence_is_still_an_identifier(text: str, gone: str) -> None:
    """The trailing exclusion existed to protect decimals, but a decimal has a
    digit AFTER the separator. Excluding on the punctuation alone exempted
    every id at a sentence boundary — a false negative that ALSO masked the
    ZIP false positive, since a postcode ending a sentence was equally exempt.
    One sloppy character, failing in both directions at once."""
    assert gone not in scrub_reply_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "The total came to $12,500.75 exactly.",
        "Occupancy is at 12345% of forecast.",
        "The full code is 96813-2214 on the lease.",
    ],
)
def test_the_narrower_lookahead_still_protects_real_composite_numbers(text: str) -> None:
    """A digit after the separator genuinely does mean 'part of a bigger
    number'. That is the case the exclusion was written for and it must hold."""
    assert scrub_reply_text(text) == text


# ── internal agent names in user-facing prose (live battery, 2026-08-26) ─────


def test_internal_agent_names_never_reach_the_user() -> None:
    """⚠ Found on live answers, not invented.

    The cross-source battery returned prose reading "buildium_agent (queried
    twice) found no property named…" and "search_memory returned nothing
    relevant". Those are function names on a property manager's screen. The
    facts were right; the register was wrong.
    """
    said = scrub_reply_text(
        "buildium_agent (queried twice) found no property named Garden Row, and "
        "drive_agent couldn't pull an SOP. search_memory returned nothing relevant."
    )
    for internal in ("buildium_agent", "drive_agent", "search_memory", "_agent"):
        assert internal not in said, f"{internal!r} still in the reply: {said!r}"
    assert "Buildium" in said and "Drive" in said and "memory" in said


def test_the_rename_is_scoped_to_names_we_own() -> None:
    """⚠ The over-reach half, and the reason this is a named list rather than a
    ``[a-z]+_[a-z]+`` sweep. A file the user named ``vendor_coi_tracker`` is
    THEIR word for THEIR document; rewriting it to something we invented would
    be gotcha 38's false positive, where a scrubber wrong in both directions
    had one error hiding the other.

    ⚠ Asserts on ``_rename_internals``, NOT on the full pipeline — because the
    full pipeline already collapses every snake_case word to spaces via the
    pre-existing ``redact_id_references``. The first draft of this test
    asserted the round trip and failed, which read as a regression and was not
    one: that collapse predates this change by months. Recorded rather than
    quietly deleted, because "the scrubber rewrites the user's own filenames"
    is a real (separate, smaller) question and this is where the next reader
    will look for it.
    """
    from helixis.agents.reply_hygiene import _rename_internals

    kept = _rename_internals("The vendor_coi_tracker sheet lists three lapsed policies.")
    assert "vendor_coi_tracker" in kept, "the rename touched a word we do not own"


def test_renaming_is_idempotent() -> None:
    """``StreamScrubber`` re-scrubs across chunk seams, so a second pass must
    be a no-op — otherwise "Buildium" could drift on every re-scrub."""
    once = scrub_reply_text("buildium_agent found it.")
    assert scrub_reply_text(once) == once


def test_agent_names_match_the_registry() -> None:
    """⚠ BOTH sides (gotcha 28). The scrubber keeps its own copy of the agent
    names to avoid an import cycle into the orchestrator (#912 is the record of
    what a cycle in this graph costs). A copy that drifts is a specialist whose
    name leaks — so every registered agent must be covered here."""
    from helixis.agents.orchestrator import AGENT_DISPLAY_NAMES
    from helixis.agents.reply_hygiene import _INTERNAL_AGENT_NAMES

    uncovered = {
        name
        for name in AGENT_DISPLAY_NAMES
        if name.endswith("_agent") and name not in _INTERNAL_AGENT_NAMES
    }
    assert not uncovered, (
        f"these specialists can leak their internal name into a reply: {sorted(uncovered)}. "
        f"Add them to _INTERNAL_AGENT_NAMES in reply_hygiene."
    )


# ── the answer arrived in slabs ──────────────────────────────────────────
#
# A live run showed the reply landing "like it tries to do a whole paragraph
# at once". `StreamScrubber.push` cut ONLY on a newline and had no size cap,
# so an entire paragraph accumulated and published as one event. Measured on
# word-by-word deltas of a realistic paragraph: 34 deltas -> 3 publishes, the
# first a single 205-character chunk.
#
# The reasoning path in orchestrator.py already had the escape hatch
# (`_REASONING_FLUSH_CHARS`) for the identical reason, quoted there: "a buffer
# that only flushes on a character the model may never emit is a channel that
# stays silent for the whole window it exists to fill."

_PARAGRAPH = (
    "The delinquency report is the closest read on this. Work orders themselves do not "
    "carry a delinquent status, but the tenant with the largest overdue balance who also "
    "has open maintenance items is the one to look at first. That is the honest answer.\n"
)


def _stream(text: str, chunk: int) -> list[str]:
    from helixis.agents.reply_hygiene import StreamScrubber

    scrubber = StreamScrubber()
    out = [x for i in range(0, len(text), chunk) if (x := scrubber.push(text[i : i + chunk]))]
    tail = scrubber.flush()
    if tail:
        out.append(tail)
    return out


def test_a_long_paragraph_no_longer_lands_as_one_slab() -> None:
    from helixis.agents.reply_hygiene import _ANSWER_FLUSH_CHARS

    deltas = [w + " " for w in _PARAGRAPH.split(" ")]  # how a model streams
    from helixis.agents.reply_hygiene import StreamScrubber

    scrubber = StreamScrubber()
    published = [x for d in deltas if (x := scrubber.push(d))]
    tail = scrubber.flush()
    if tail:
        published.append(tail)

    assert len(published) > 1, "the whole paragraph still arrives in one event"
    # Sentence-sized, not paragraph-sized. The bound is loose on purpose: the
    # cut needs a sentence terminator, so one long sentence legitimately
    # exceeds the threshold.
    assert max(len(p) for p in published) < len(_PARAGRAPH), "largest chunk is the whole paragraph"
    assert _ANSWER_FLUSH_CHARS > 0


@pytest.mark.parametrize("chunk", [1, 2, 3, 5, 7, 11, 17, 64, 256])
@pytest.mark.parametrize(
    "text",
    [
        _PARAGRAPH,
        # ⚠ The case that broke three implementations of this cut, and the
        # ONLY one in this list that exercises the seam-healing branch — a
        # mutation removing it survives every other string here. When the
        # remainder BEGINS with an identifier, redacting it leaves a space,
        # which the whole-text scrub collapses against the space before it
        # while a mid-line cut cannot see across the seam. The exact wording
        # matters: it decides where the flush threshold lands, and a
        # paraphrase moved the cut off the seam and silently stopped testing
        # anything (verified by mutation, both ways).
        "Eight are open (IDs 11314, 11320, 11325) and two more still appear. "
        "That is work order 11342. 11350 is the next one to review. "
        "Nothing else is pending.\n",
        # Figures that MUST survive, per this module's precision-over-recall rule.
        "The balance is $4,500 and rent is 1,650 a month. Honolulu, HI 96813 needs "
        "attention. There are 10 work orders open right now across the portfolio.\n",
        # No sentence terminator anywhere: must fall back to holding, never cut.
        ("a " * 90) + "\n",
        # No trailing newline — the flush path.
        "The tenant with the largest overdue balance also has open maintenance. "
        "Worth checking before any late notice goes out to them this week at all.",
        # A sentence that scrubs away to nothing must not publish a lone space.
        "Here is the summary. 11314. And the rest of the answer follows on from there "
        "with enough words to cross the flush threshold comfortably.\n",
    ],
)
def test_streaming_is_byte_identical_to_scrubbing_the_whole_text(text: str, chunk: int) -> None:
    """⚠ THE gate on this class, and the only reason the mid-line cut ships.

    The class docstring records TWO cleverer designs that both shipped
    corruption; this is a third. It cuts inside a line, which `_scrub_line`
    normalises at BOTH edges — it rstrips, and `redact_id_references` lstrips —
    so the separator survives on neither side and has to be re-inserted, and
    the re-inserted one can double against a redaction seam. Every one of
    those was a real observed output during this change. Nine chunk sizes
    because a single size finds none of them.
    """
    from helixis.agents.reply_hygiene import scrub_reply_text

    assert "".join(_stream(text, chunk)) == scrub_reply_text(text)


def test_identifiers_are_still_removed_when_the_cut_lands_between_them() -> None:
    """The safety property the round-trip does not by itself assert: a cut is
    only allowed at a boundary an identifier run cannot contain."""
    from helixis.agents.reply_hygiene import scrub_reply_text

    text = "Eight are open (IDs 11314, 11320) and 11325 too. Nothing else is pending here now.\n"
    for chunk in (1, 3, 7, 64):
        streamed = "".join(_stream(text, chunk))
        for ident in ("11314", "11320", "11325"):
            assert ident not in streamed, f"{ident} leaked at chunk={chunk}"
        assert streamed == scrub_reply_text(text)
