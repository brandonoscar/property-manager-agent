"""The Composio read/write gate, checked against 603 REAL slugs.

``tool_gate`` decides which Composio tools run freely as READS and which are
confirm-gated as WRITES. Its own TODO recorded that the patterns came from
Composio's *published* slugs and were never validated against a live
``tools.get`` — with GOOGLEDOCS and GOOGLESHEETS added without a dump at all.

``scripts/composio_tool_dump.py`` closed that: 603 slugs across six toolkits,
committed as a fixture so these assertions run offline forever.

⚠ WHAT THIS FILE USED TO ASSERT, AND WHY IT WAS WRONG
------------------------------------------------------
The first version failed on 16 slugs, claiming a leak. It asserted that no
slug both ``_has_write_verb`` AND ``_matches_read_pattern`` — pattern algebra
over two helper predicates. But that conjunction is *precisely the case the
real classifier handles correctly*::

    def is_write_tool(slug):
        if _has_write_verb(slug):   return True    # <- write verb wins, always
        if _matches_read_pattern(slug): return False
        return True                                # <- unknown fails closed

All 16 classified ``is_write_tool=True``. Nothing was ungated; the test was
measuring a proxy that had no bearing on the decision it claimed to guard.
Four were genuine writes (``GOOGLECALENDAR_CALENDAR_LIST_DELETE`` — ``_LIST``
matches a read segment while the real verb trails), correctly gated by the
ordering above. Twelve were substring collisions (``SET`` inside ``SETTINGS``,
``ADD`` inside ``ADDRESSES``, ``SHARE`` inside ``SHARED``).

Retiring a proxy once you hold the real measurement is legitimate; weakening a
live assertion to go green is not, and each retirement must name its
replacement (gotcha 33). **Replacement: ``test_no_destructive_slug_is_ungated``
below**, which runs the ACTUAL production classifier over the ACTUAL slug list
and asks the only question that matters — can any real operation that sends
mail, deletes a file or moves an event run without a confirmation card?

That is strictly stronger. The old test could pass while a genuinely
destructive slug slipped through on a pattern nobody thought of; this one
cannot.

⚠ CORRECTION (2026-08-26): "OVER-GATING IS SAFE" WAS WRONG
-----------------------------------------------------------
This file said over-gating a read is "annoying, safe" and made it a REPORT.
The TODO it replaced said the same. Both were wrong, and that sentence is why
a broken feature read as a strict posture for months.

A gate BLOCKS its tool until someone answers, and the stall watchdog PAUSES
for the whole wait by design (``confirm_wait["pending"]``). So an over-gated
read is not a card the user shrugs off — it is 300 seconds of dead air with
nothing that will end it. Measured on ``helixis_pending_confirmations``,
**20 of the 44 confirmations ever raised** were
``GOOGLEDRIVE_EXPORT_GOOGLE_WORKSPACE_FILE`` and ``GOOGLEDRIVE_PARSE_FILE`` —
the two calls that read a Google Doc's contents — every one expiring
unanswered at ~300s. One session raised four serially, 13:42→14:06. That is
why Occupella had never read its own SOP corpus (gotcha 42 blamed the turn cap
alone) and very likely the ~590s gotcha 39 logged as unexplained.

The real asymmetry is not read-vs-write, it is **fails-open vs fails-slow**.
Under-gating a write stays the louder failure and stays the primary assertion.
But the read side is no longer only a report: the document-read path is an
assertion too (``test_reading_a_document_is_never_gated``), because a
confirmation card on "open the file" is not caution, it is the feature not
working.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from helixis.connectors.tool_gate import is_write_tool

FIXTURE = pathlib.Path("tests/fixtures/composio_tool_slugs.json")

pytestmark = pytest.mark.skipif(
    not FIXTURE.exists(),
    reason=(
        "no live Composio slug dump — the read/write gate is UNVALIDATED. "
        "Run scripts/composio_tool_dump.py with the Composio key and commit "
        "tests/fixtures/composio_tool_slugs.json."
    ),
)

#: Slug SEGMENTS that mean the operation changes something. Matched as whole
#: ``_``-delimited segments, never substrings — substring matching is what made
#: ``SET`` fire inside ``SETTINGS`` and produced the phantom leaks above.
#:
#: Deliberately broad: a verb wrongly listed here can only make the test
#: demand MORE gating, which is the safe direction. One wrongly omitted is the
#: dangerous direction, so err toward inclusion.
#:
#: ⚠ Verbs only — NOT modifiers. ``BATCH`` was listed here in the first cut and
#: reported two false leaks: ``GOOGLESHEETS_GET_BATCH_VALUES`` and
#: ``..._VALUES_BATCH_GET_BY_DATA_FILTER`` are reads. ``BATCH_UPDATE`` is
#: destructive because of UPDATE, and ``BATCH_GET`` is not destructive at all,
#: so the modifier carries no signal of its own. Checked before removing it:
#: no slug's only destructive segment was ``BATCH``, so nothing lost coverage.
DESTRUCTIVE_SEGMENTS: frozenset[str] = frozenset(
    {
        "ADD",
        "APPEND",
        "ARCHIVE",
        "CLEAR",
        "COPY",
        "CREATE",
        "DELETE",
        "DUPLICATE",
        "EMPTY",
        "FORWARD",
        "IMPORT",
        "INSERT",
        "MODIFY",
        "MOVE",
        "PATCH",
        "PUT",
        "REMOVE",
        "RENAME",
        "REPLY",
        "RESTORE",
        "SEND",
        "SET",
        "SHARE",
        "STOP",
        "TRASH",
        "UNTRASH",
        "UPDATE",
        "UPLOAD",
        "WRITE",
    }
)


def _slugs() -> dict[str, list[str]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {
        toolkit: [e["slug"] for e in entries]
        for toolkit, entries in (data.get("toolkits") or {}).items()
        if entries  # None = that toolkit failed to dump; absent, not empty
    }


def _all_slugs() -> list[str]:
    return [s for slugs in _slugs().values() for s in slugs]


def _is_destructive(slug: str) -> bool:
    return bool(set(slug.upper().split("_")) & DESTRUCTIVE_SEGMENTS)


# ── the invariant that matters ──────────────────────────────────────────────


def test_no_destructive_slug_is_ungated() -> None:
    """⚠ THE security property, over the real classifier and the real list.

    UNDER-gating a write is a Google action firing with no card at all: mail
    sent, a file deleted, a calendar event moved, with nobody asked. It is the
    failure you cannot walk back, which is why it is the primary assertion —
    but see the module docstring: over-gating is not the harmless opposite it
    was described as, and the read side now has assertions of its own.
    """
    leaks = sorted(s for s in _all_slugs() if _is_destructive(s) and not is_write_tool(s))
    assert not leaks, (
        f"these slugs name a destructive operation and are NOT confirm-gated, so they "
        f"would fire without a card: {leaks}. Add the verb to `_WRITE_VERB_TOKENS` in "
        f"connectors/tool_gate.py, or tighten `_READ_VERB_SEGMENTS`. Fail closed: when "
        f"in doubt, gate it."
    )


def test_an_unknown_slug_fails_closed() -> None:
    """The rule that makes tomorrow's Composio tools safe without a redeploy.
    A slug matching nothing must be treated as a write — coverage of a moving
    third-party catalogue can never be assumed complete."""
    assert is_write_tool("SOMETOOLKIT_A_VERB_NOBODY_ANTICIPATED") is True
    assert is_write_tool("") is True


def test_a_write_verb_beats_a_read_pattern() -> None:
    """The ordering the phantom leaks depended on, pinned so a refactor cannot
    reverse it. ``GOOGLECALENDAR_CALENDAR_LIST_DELETE`` contains ``LIST`` — if
    read-matching ran first, a real delete would go ungated."""
    for slug in (
        "GOOGLECALENDAR_CALENDAR_LIST_DELETE",
        "GOOGLECALENDAR_CALENDAR_LIST_INSERT",
        "GOOGLECALENDAR_CALENDAR_LIST_PATCH",
        "GOOGLECALENDAR_CALENDAR_LIST_UPDATE",
    ):
        assert is_write_tool(slug) is True, f"{slug} is a write wearing a read's prefix"


# ── coverage of the dump itself ─────────────────────────────────────────────


def test_the_dump_covers_every_toolkit_a_specialist_can_be_given() -> None:
    """A toolkit missing from the dump is a toolkit whose gate nothing checks.
    A PARTIAL dump validating a gate is worse than no dump — it reads as
    coverage while leaving a hole. (OUTLOOK really did truncate at the SDK's
    200-item page once, losing 105 slugs.)"""
    from scripts.composio_tool_dump import TOOLKITS

    missing = set(TOOLKITS) - set(_slugs())
    assert not missing, (
        f"these toolkits are not in the dump, so their slugs are unchecked: {sorted(missing)}. "
        "Re-run scripts/composio_tool_dump.py — it reports which toolkits failed."
    )


def test_the_fixture_is_a_plausible_full_catalogue() -> None:
    """Guards the truncation class generally rather than one toolkit. A dump
    that silently halved would still pass every assertion above — each
    surviving slug would classify fine — while the ones it dropped went
    unchecked."""
    per_toolkit = {k: len(v) for k, v in _slugs().items()}
    assert sum(per_toolkit.values()) > 500, f"fixture looks truncated: {per_toolkit}"
    for toolkit, count in per_toolkit.items():
        assert count > 10, f"{toolkit} has only {count} slugs — did its page truncate?"


# ── over-gating: the specific reads that must never cost 300s ───────────────

#: Reads whose gating is not "noise" but a broken feature, each with what
#: breaks. An entry is a claim, checked against this dump, that the slug
#: returns data and changes nothing — so it must never raise a card.
#:
#: ⚠ This list is deliberately NOT "every non-destructive slug". A blanket
#: assertion would fail on genuinely ambiguous slugs (``GOOGLEDRIVE_WATCH_FILE``
#: creates a push subscription) and the way to make it green would be to
#: classify those as reads — turning a slow failure into an open one. Name the
#: paths the product actually depends on instead.
LOAD_BEARING_READS: dict[str, str] = {
    # Opening a document. 20 of the 44 live confirmations were these two.
    "GOOGLEDRIVE_EXPORT_GOOGLE_WORKSPACE_FILE": "the Drive agent cannot read any Google Doc",
    "GOOGLEDRIVE_PARSE_FILE": "the Drive agent cannot extract text from a file",
    "GOOGLEDOCS_EXPORT_DOCUMENT_AS_PDF": "Docs content is unreachable",
    # Finding the document in the first place.
    "GOOGLEDRIVE_FIND_FILE": "Drive search interrupts the user",
    "GOOGLEDRIVE_LIST_SHARED_DRIVES": "listing shared drives interrupts the user",
    # Reading a spreadsheet — the rent-roll path.
    "GOOGLESHEETS_BATCH_GET": "reading a rent roll interrupts the user",
    "GOOGLESHEETS_VALUES_GET": "reading a sheet range interrupts the user",
    # Calendar + mail lookups behind ordinary questions.
    "GOOGLECALENDAR_EVENTS_GET": "reading an event interrupts the user",
    "GOOGLECALENDAR_EVENTS_LIST": "checking the calendar interrupts the user",
    "GMAIL_FETCH_EMAILS": "reading mail interrupts the user",
    "GMAIL_GET_VACATION_SETTINGS": "a settings LOOKUP reads as a settings WRITE",
}


def test_reading_a_document_is_never_gated() -> None:
    """⚠ Promoted from a report to an assertion, and this is the reason.

    A gate blocks its tool and pauses the stall watchdog, so an over-gated read
    costs the user 300 seconds of silence — not a card they can ignore. These
    are the specific lookups the product is built on; a card on any of them is
    the feature not working, and it is invisible in every other measure because
    the watchdog is paused for exactly that window.
    """
    broken = {s: why for s, why in LOAD_BEARING_READS.items() if is_write_tool(s)}
    assert not broken, (
        "these lookups raise a confirmation card, and an unanswered card costs the "
        f"run ~300 seconds each: {broken}. Add the read verb to `_READ_VERB_SEGMENTS` "
        "or the slug to `_EXPLICIT_READ_SLUGS` in connectors/tool_gate.py. Do NOT "
        "resolve this by widening a write pattern."
    )


def test_the_load_bearing_reads_are_real_slugs() -> None:
    """⚠ A read that no longer exists is an assertion that cannot fail — the
    same class as gotcha 35's case that never ran. If Composio renames one, this
    must say so rather than quietly guarding nothing."""
    known = set(_all_slugs())
    missing = sorted(set(LOAD_BEARING_READS) - known)
    assert not missing, (
        f"LOAD_BEARING_READS names slugs absent from the 603-slug dump: {missing}. "
        "Either Composio renamed them (update the list AND whatever calls them) or "
        "the dump is stale."
    )


def test_over_gating_has_not_run_away() -> None:
    """The wholesale-rot canary, kept as a threshold.

    ``test_reading_a_document_is_never_gated`` covers the paths we know matter;
    this covers the ones we have not thought of yet, by catching the read
    patterns falling off Composio's naming altogether. That shows up as a
    cliff, not a drift — so the bar stays generous rather than becoming a
    quality bar someone is tempted to make green by loosening a write pattern.
    """
    slugs = _all_slugs()
    gated = [s for s in slugs if is_write_tool(s)]
    over = sorted(s for s in gated if not _is_destructive(s))
    print(
        f"\n[tool_gate] {len(slugs)} slugs | {len(gated)} gated "
        f"({100 * len(gated) // len(slugs)}%) | {len(over)} gated without a "
        f"destructive segment — each one costs ~300s if a model reaches for it "
        f"and nobody answers: {over[:12]}"
    )
    assert len(gated) / len(slugs) < 0.95, (
        f"{len(gated)}/{len(slugs)} slugs are gated — nearly everything now needs a "
        "confirmation card, which means the read patterns have stopped matching "
        "Composio's naming and every lookup interrupts the user."
    )


# ── the guard's own probe ───────────────────────────────────────────────────


def test_the_destructive_detector_can_actually_fire() -> None:
    """⚠ Assert the probe RUNS (gotcha 33). If ``_is_destructive`` matched
    nothing, ``test_no_destructive_slug_is_ungated`` would pass vacuously —
    green because it checked zero slugs, which is indistinguishable from green
    because everything is gated."""
    assert _is_destructive("GMAIL_SEND_EMAIL")
    assert _is_destructive("GOOGLEDRIVE_DELETE_FOLDER")
    assert not _is_destructive("GMAIL_LIST_MESSAGES")
    # ⚠ Segment matching, not substring: these are the collisions that produced
    # the phantom leaks the old assertion reported.
    assert not _is_destructive("GMAIL_GET_VACATION_SETTINGS"), "SET must not match SETTINGS"
    assert not _is_destructive("GOOGLEDRIVE_LIST_SHARED_DRIVES"), "SHARE must not match SHARED"

    destructive = [s for s in _all_slugs() if _is_destructive(s)]
    assert len(destructive) > 100, (
        f"only {len(destructive)} of 603 slugs read as destructive — the detector has "
        "stopped matching, so the leak assertion above is checking almost nothing"
    )
