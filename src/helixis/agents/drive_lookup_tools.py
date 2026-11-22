"""Find a Drive document in ONE tool call, off the cached index.

Measured 2026-08-26 on ``orchestrator_runs``, 14 days: a turn that consults
``drive_agent`` averages **583-751s**; a turn that does not averages **70-227s**,
every day, all fourteen. Same question, same day: five ``buildium_agent``
consults finished in 175s while two Buildium plus two Drive took 561s. Every
slow run contains ``drive_agent``; no fast run does.

Zero Drive consults exist in the table before 2026-08-25, because until #902
Drive was not readable at all — it was consulted once in the whole window and
returned ``Max turns (8) exceeded`` while the orchestrator rendered "Drive has
no record" over a corpus that plainly had the document (gotcha 42). #902 fixed
that by raising ``SPECIALIST_MAX_TURNS['drive_agent']`` to 16. Correctness
fixed; latency roughly 5x. A wrong-but-instant answer became a
right-but-ten-minute one, and neither is shippable.

**Lowering the ceiling back is not the fix** — gotcha 42 is the standing record
that a flat cap starves Drive, and that raising it flat was the wrong move
twice. The turns are spent on a genuinely multi-step job: search, identify,
rule out a superseded copy, open, read. Sixteen SEQUENTIAL model turns, each
one a model call plus a Drive API round trip.

So this is the move gotchas 25/32/34/37 all end with, most recently
``portfolio_review``: **when the model must make N sequential good decisions to
avoid a loop, make it one tool that makes them.**

The search half needs no Drive API call at all. ``company_knowledge/drive.py``
already enumerates every connected Drive once a day into ``drive_index``
(title / mime / folder / modified) precisely so the agent does not hit the API
per question — 209 files across 2 companies as of 2026-08-26, so the seam its
docstring flagged for dogfood is confirmed working. This tool reads that index.
Pure SQL, no network, no Composio call, no turns spent enumerating.

⚠ **The superseded copy is real and the index already solves it.** The live
corpus holds BOTH ``SOP-01 Maintenance Triage and Vendor Dispatch`` and
``SOP-01 Maintenance Triage v2.0 (SUPERSEDED - do not use)``. That pair is why
the lookup needs a disambiguation step — but the marker is in the TITLE, so
returning candidate titles settles it in one call instead of opening both
files. Reading a superseded SOP and quoting its spend tiers would be worse than
saying nothing.

Deliberately NOT in this module: fetching file CONTENTS. That still goes
through the Composio tools the specialist already holds, whose exact read slug
and argument shape cannot be confirmed from here — and gotchas 15/24/25 are
three separate records of what guessing a Composio/Buildium wire shape costs.
Collapsing search+disambiguate is the part that can be built without a guess,
and it is the part that burns the turns.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from agents import function_tool

from helixis.memory.context import current_memory_context

logger = logging.getLogger(__name__)

#: Titles that announce they are not the live copy. Case-insensitive, matched
#: against the title only — the marker lives there, which is the whole reason
#: one indexed search can disambiguate what would otherwise cost two file opens.
_SUPERSEDED = re.compile(
    r"(?:supersed|deprecat|do\s*not\s*use|obsolete|\bold\b|\barchiv|\bdraft\b|\bv?\d+\.\d+\s*\(old)",
    re.IGNORECASE,
)

#: How many candidates to return. Enough that a near-miss and its live
#: counterpart both appear (the SOP-01 pair), small enough to stay one glance.
_DEFAULT_LIMIT = 8
_MAX_LIMIT = 25

#: Words too common to discriminate between 209 filenames. Dropped from the
#: term list so "the maintenance SOP for vendors" searches on the three words
#: that matter.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "for",
        "of",
        "on",
        "in",
        "to",
        "and",
        "or",
        "my",
        "our",
        "is",
        "are",
        "what",
        "which",
        "who",
        "where",
        "when",
        "how",
        "do",
        "does",
        "find",
        "get",
        "show",
        "read",
        "open",
        "look",
        "up",
        "me",
        "please",
        "file",
        "files",
        "document",
        "documents",
        "doc",
        "docs",
        "drive",
    }
)


def _terms(query: str) -> list[str]:
    """Content words from a natural-language ask, longest first.

    Longest-first matters: a title matching "delinquency" is a far better hit
    than one matching "rent", and scoring rewards the specific term.
    """
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]*", query or "")
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        lowered = w.lower()
        if lowered in _STOPWORDS or len(lowered) < 2 or lowered in seen:
            continue
        seen.add(lowered)
        out.append(lowered)
    return sorted(out, key=len, reverse=True)


def score_title(title: str, terms: list[str]) -> float:
    """How well one indexed title answers the query. 0.0 = no term matched.

    Pure and deterministic so it can be tested without a database. Whole-word
    hits outrank substring hits ("lease" should not be beaten by "release"),
    and a superseded title is pushed BELOW every live match rather than
    dropped — the user may genuinely want the old one, and silently hiding a
    file the corpus contains is the absence-claim failure of gotcha 26.
    """
    if not terms:
        return 0.0
    lowered = title.lower()
    score = 0.0
    for term in terms:
        if re.search(rf"\b{re.escape(term)}\b", lowered):
            score += 2.0 + len(term) / 20.0
        elif term in lowered:
            score += 1.0
    if score and _SUPERSEDED.search(title):
        # ⚠ SCALE, do not subtract. The first cut did ``score -= 100.0``, which
        # drove a matched superseded title NEGATIVE — and callers filter on
        # ``> 0`` to mean "a term matched", so the demotion silently became an
        # EXCLUSION: the exact behaviour three lines of docstring above forbid.
        # Scaling keeps it positive (still found, still returned) while placing
        # it below every live match. Caught by
        # ``test_the_live_sop_beats_the_superseded_one``.
        score *= 0.01
    return score


def _fmt_row(row: dict[str, Any]) -> str:
    title = str(row.get("title") or "(untitled)")
    bits = [f"- {title}"]
    if _SUPERSEDED.search(title):
        bits.append("  ⚠ this title marks itself superseded//old — prefer a live copy above")
    mime = str(row.get("mime_type") or "")
    kind = (
        "Google Doc"
        if "google-apps.document" in mime
        else "Google Sheet"
        if "google-apps.spreadsheet" in mime
        else "Folder"
        if "google-apps.folder" in mime
        else (mime.rsplit("/", 1)[-1] if mime else "file")
    )
    meta = [kind]
    if row.get("path"):
        meta.append(f"in {row['path']}")
    if row.get("modified_at") is not None:
        meta.append(f"modified {str(row['modified_at'])[:10]}")
    bits.append(f"  {' · '.join(meta)}")
    bits.append(f"  file_id: {row.get('file_id')}")
    return "\n".join(bits)


def _normalised_title(row: dict[str, Any]) -> str:
    """Collapse whitespace and case so "SOP-01  Triage" and "sop-01 triage"
    are recognised as the same title. Punctuation is LEFT ALONE: "Lease
    (2025)" and "Lease 2025" are plausibly different documents, and merging
    them would put the note on files that are genuinely distinguishable."""
    return " ".join(str(row.get("title") or "").split()).casefold()


def duplicate_titles(rows: list[dict[str, Any]]) -> list[str]:
    """Normalised titles carried by more than one row, in display order.

    ⚠ The blank check lives in ONE place, and it is this one. Two rows the
    index has no title for are a data gap, not a naming ambiguity — telling the
    model to "ask which they mean" about two untitled files helps nobody, and a
    note that fires on nonsense is one people stop reading (gotcha 52). A
    matching guard in the counting loop below would be redundant, because this
    one short-circuits before the count is ever read (gotcha 58).
    """
    seen: dict[str, int] = {}
    for row in rows:
        seen[_normalised_title(row)] = seen.get(_normalised_title(row), 0) + 1
    out: list[str] = []
    for row in rows:
        key = _normalised_title(row)
        if key and seen[key] > 1 and key not in out:
            out.append(key)
    return out


#: What to do when the list cannot tell two files apart by name.
#:
#: ⚠ The rows were already self-contained — kind, folder, modified date and
#: file_id are all on the line (gotchas 41/51's fix, applied here when the tool
#: was written). What was missing is the CLOSING INSTRUCTION: a list of
#: identically-named files under a footer that says "open the one you need" is
#: an invitation to pick, and picking silently is how a superseded procedure
#: gets quoted as current policy — which Drive has already done once
#: (gotcha 42).
#:
#: ⚠ It says name-both-and-ask rather than prefer-the-newest. Newest is USUALLY
#: right, and "usually" is exactly what produces a confident wrong quote about
#: a company's own operating procedure. Where a copy marks itself superseded
#: the ranking already demotes it and `_fmt_row` already says so; this covers
#: the case where neither does and only the folder and the date differ.
_SAME_TITLE_NOTE = (
    "⚠ More than one file here has the SAME title. A title is not an identity "
    "in Drive — what differs is the folder and the modified date, both shown "
    "above. Do NOT open one silently and treat it as the document: pick the "
    "one the user actually named, or name each candidate WITH its folder and "
    "date and ask which they mean. If you quote from one, say which folder and "
    "date it came from."
)


#: A trailing marker that says "this file is a REVISION of the one beside it".
#:
#: ⚠ Deliberately narrow, and the exclusions are the design. A bare trailing
#: NUMBER is not a version — "Q1 Report" and "Q2 Report" are two reports, and
#: "Lease 2024" and "Lease 2025" are two leases with different terms. Flagging
#: those would put a version warning on ordinary filing, which is how a signal
#: gets skipped (gotcha 52). What IS admitted: an explicit ``v``/``rev``/
#: ``version`` prefix on a number, the word ``final``, and Drive's own
#: duplicate suffix ``(2)`` — capped below 1000 so a year in brackets is not
#: mistaken for a copy number.
_VERSION_TOKEN = re.compile(
    r"[\s\-_(\[]*(?:"
    r"v(?:er(?:sion)?)?\s*\.?\s*\d+(?:\.\d+)*"
    r"|rev(?:ision)?\s*\.?\s*\d+"
    r"|final"
    r"|\(\d{1,3}\)"
    r")[\s\-_)\]]*$",
    re.IGNORECASE,
)


def version_stem(title: str) -> tuple[str, bool]:
    """``(the title without its version marker, whether it had one)``.

    Applied REPEATEDLY, because real filenames stack them — "SOP-01 v2 final"
    carries two, and stripping only the last would leave two stems that do not
    match and the pair would go unnoticed.
    """
    stem = " ".join(str(title or "").split())
    marked = False
    while True:
        stripped = _VERSION_TOKEN.sub("", stem).strip()
        if stripped == stem or not stripped:
            break
        stem, marked = stripped, True
    return stem.casefold(), marked


def version_families(rows: list[dict[str, Any]]) -> list[str]:
    """Stems shared by two or more rows carrying DIFFERENT titles.

    Which is the same thing as "at least one of them is version-MARKED", and
    that equivalence is the whole subtlety here. Stripping nothing off an
    unmarked title leaves exactly what :func:`_normalised_title` produces — the
    two collapse whitespace and case and touch nothing else — so an unmarked
    row's stem IS its normalised title. A family where NOBODY is marked
    therefore holds one distinct title by construction, and the distinctness
    test below has already excluded it.

    ⚠ **An explicit ``and marks.get(stem)`` USED TO SIT HERE and could not
    change any output.** Measured before deleting rather than argued: 2,300
    exhaustive three-row corpora and 30,000 random four-row ones over a
    marker-heavy vocabulary, zero disagreements between the two versions. Dead
    code wearing the costume of a safety check is worse than none, because the
    next reader believes the requirement is enforced there (gotchas 48/58).
    What keeps this note off ordinary filing is `_VERSION_TOKEN` being narrow
    and the distinctness test being strict — nothing else.

    ⚠ **The deletion is only safe while the two normalisers agree**, so that is
    pinned by `test_an_unmarked_title_normalises_identically_in_both_places`.
    Teach `_normalised_title` to strip punctuation and "Lease (2025)" and
    "Lease 2025" become two distinct titles sharing a stem with neither
    marked — a version warning on two genuinely different leases, which is the
    cry-wolf direction (gotcha 52). The test fails first.

    ⚠ Distinct from :func:`duplicate_titles`, which catches the IDENTICAL
    title. This is the case gotcha 42's pair does not cover: two copies whose
    titles differ, where NEITHER marks itself superseded, so the ranking has no
    preference to express and the rows look like two ordinary documents.
    """
    # ⚠ ONE condition does all three exclusions, and there used to be two more
    # guards beside it that could not change any output — a marks lookup and a
    # blank-title skip, each deleted after a mutation survived and a brute-force
    # sweep showed why (gotcha 58; the arithmetic is in the docstring above and
    # in `test_two_untitled_rows_are_not_versions_of_each_other`). Blanks
    # collapse to the same empty title so they never reach two, unmarked rows
    # keep their own titles as stems so they never reach two, and identical
    # titles are one set entry — which is `duplicate_titles`' case, not this
    # one. Do not re-add a guard here without a corpus that distinguishes it.
    stems: dict[str, set[str]] = {}
    for row in rows:
        stems.setdefault(version_stem(str(row.get("title") or ""))[0], set()).add(
            _normalised_title(row)
        )
    out: list[str] = []
    for row in rows:
        stem, _ = version_stem(str(row.get("title") or ""))
        if len(stems.get(stem, ())) > 1 and stem not in out:
            out.append(stem)
    return out


#: What to do when two files look like revisions of one document.
#:
#: ⚠ It does NOT say take the highest number. A "v2" in a folder nobody uses
#: loses to a "v1" somebody is actually maintaining, and the index cannot see
#: which is which — the same reasoning as the same-title note, arriving where
#: the numbers make guessing feel justified.
_VERSION_CONFLICT_NOTE = (
    "⚠ Two or more files here look like VERSIONS of the same document, and "
    "NEITHER marks itself superseded — so nothing in this list says which one "
    "is in force. A higher version number is not proof: a v2 abandoned in a "
    "drafts folder loses to the v1 somebody actually maintains. Name the "
    "candidates with their folder and date and ask which is current, or say "
    "which one you read and that you could not confirm it is the live copy."
)


#: A daily job builds this index, so one missed run is a day old and still
#: usable. Past this, a list read as current is a claim about a Drive nobody
#: has looked at since — and a document created last week would be absent for a
#: reason that has nothing to do with the query.
_STALE_INDEX_AFTER = timedelta(days=2)


def index_freshness(indexed_at: datetime | None, *, now: datetime | None = None) -> str:
    """One sentence on how old this listing is, always present.

    ⚠ **``None`` is not "fresh".** An unreadable stamp means nobody checked,
    which is a different claim from "checked, it is current" — collapsing the
    two is the failure gotchas 55/57/70 all record. It says so instead.

    ⚠ **Stated on a HIT as well as on an empty result.** The tempting shape is
    to warn only when nothing matched, and it is the wrong one: a stale index
    that DOES return rows presents a three-week-old title list as the state of
    the Drive, and the model quotes it without a word. That asymmetry — warn
    when you find nothing, flatter when you find something — is the same defect
    the mirror rollups carried (checklist #41).
    """
    if indexed_at is None:
        return (
            "⚠ The age of this index could not be read, so treat it as possibly "
            "out of date — say so if the answer turns on how current it is."
        )
    current = now or datetime.now(UTC)
    stamp = indexed_at if indexed_at.tzinfo else indexed_at.replace(tzinfo=UTC)
    age = current - stamp
    when = f"Index last refreshed {stamp.date().isoformat()}"
    if age <= _STALE_INDEX_AFTER:
        return f"{when}."
    # No floor: this branch is only reached past _STALE_INDEX_AFTER (2 days),
    # so age.days is already >= 2. A max(1, ...) here would be a distinction
    # that cannot exist for any input (gotcha 58) — the threshold is what
    # keeps this from ever printing "0 days ago".
    days = age.days
    return (
        f"⚠ {when} — {days} days ago. A file added or renamed since then is NOT "
        "in this list, so its absence here says nothing about the user's Drive. "
        "Say the index is stale rather than reporting a document as missing."
    )


def render_results(
    query: str,
    rows: list[dict[str, Any]],
    *,
    total_indexed: int,
    limit: int,
    indexed_at: datetime | None = None,
    now: datetime | None = None,
) -> str:
    """The text the specialist reads. Separated from the query so the wording
    contract is testable without a database.

    ⚠ Gotcha 26's rule, applied here: a filtered result that finds nothing must
    state the scope it searched, and a truncated list must forbid concluding
    absence. Reporting "no such document" over a partial read is this product's
    worst failure mode and Drive has already done it once (gotcha 42).
    """
    header = (
        f'Drive index search for "{query}" — {total_indexed} indexed file(s) in this '
        "company's connected Drive. " + index_freshness(indexed_at, now=now)
    )
    if not rows:
        return (
            f"{header}\n\nNo indexed title matched. This searched FILE TITLES ONLY, "
            "across the whole index — not file contents, and not any Drive the user "
            "has not connected. A document whose title uses different words would "
            "not appear here, so do NOT tell the user the document does not exist. "
            "Say what you searched and offer to try different terms."
        )
    shown = "\n".join(_fmt_row(r) for r in rows)
    footer = (
        "\nThese are TITLE matches from the daily index, not file contents — open "
        "the one you need with your Drive/Docs/Sheets tools using its file_id, and "
        "quote only what you actually read."
    )
    # ⚠ Only when the list actually contains a collision. On every other search
    # this is a line the model learns to skip, and then it is absent exactly
    # when it matters (gotcha 52).
    if duplicate_titles(rows):
        footer += f"\n{_SAME_TITLE_NOTE}"
    # ⚠ A SEPARATE condition, not an elif. A list can hold both an identical
    # pair and a versioned pair, and they need different instructions — the
    # same-title note points at folder and date, this one says a higher number
    # is not authority. Collapsing them would silently drop one.
    if version_families(rows):
        footer += f"\n{_VERSION_CONFLICT_NOTE}"
    if len(rows) >= limit:
        footer += (
            f"\n⚠ Showing the top {limit} only. More titles may match, so this list "
            "is NOT evidence that nothing else exists."
        )
    return f"{header}\n\n{shown}\n{footer}"


async def _search_index(
    company_id: str, terms: list[str], limit: int
) -> tuple[list[dict], int, datetime | None]:
    """Candidate rows, the company's total indexed count, and when the index
    was last refreshed.

    Ranking happens in Python (``score_title``) rather than SQL so it is
    testable and so the superseded demotion is one rule in one place. The index
    is a few hundred rows per company — small enough that fetching and scoring
    in process is cheaper than the round trips a smarter query would need.

    ⚠ ``indexed_at`` is selected here and NOT used for ranking. It exists on
    every row (0063, ``not null default now()``) and was never read — gotchas
    41/50/51's *fetched and thrown away*, arriving on the one fact that decides
    whether this list can be trusted at all. It costs one column on a query
    that already fetches the whole table.
    """
    from helixis.db.engine import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        records = await conn.fetch(
            "SELECT file_id, title, mime_type, path, modified_at, indexed_at "
            "FROM drive_index WHERE company_id = $1::uuid",
            company_id,
        )
    rows = [dict(r) for r in records]
    stamps = [r["indexed_at"] for r in rows if r.get("indexed_at") is not None]
    scored = [(score_title(str(r.get("title") or ""), terms), r) for r in rows]
    hits = [r for s, r in sorted(scored, key=lambda p: -p[0]) if s > 0]
    # ⚠ The OLDEST stamp, not the newest. The index is refreshed per
    # (company, composio_user_id) — Drive is a personal connection — so one
    # user's broken OAuth leaves their share of the corpus weeks behind while
    # a colleague's refreshes nightly. max() would report that company as
    # current and flatter exactly the case the warning exists for; min()
    # answers the question actually being asked, which is "how old is the
    # stalest thing in this list".
    return hits[:limit], len(rows), (min(stamps) if stamps else None)


async def _drive_find_files_impl(query: str, limit: int = _DEFAULT_LIMIT) -> str:
    ctx = current_memory_context()
    company_id = ctx.company_id if ctx else ""
    if not company_id:
        return (
            "Drive index unavailable: no company context on this run. This is OUR "
            "limitation, not a statement about the user's Drive — do not report the "
            "Drive as empty."
        )

    limit = max(1, min(int(limit or _DEFAULT_LIMIT), _MAX_LIMIT))
    terms = _terms(query)
    if not terms:
        return (
            'Give me something to search on — "SOP", "lease renewal", a filename. '
            "The query had no content words."
        )

    try:
        rows, total, indexed_at = await _search_index(company_id, terms, limit)
    except Exception as exc:  # noqa: BLE001 — a lookup must degrade, never raise
        logger.warning("drive index search failed: %s", exc, exc_info=True)
        return (
            "The Drive index could not be read just now (an error on OUR side, not "
            "the user's Drive). Do NOT report the Drive as empty or the document as "
            "missing — say the index was unavailable and offer to search again."
        )

    if total == 0:
        return (
            "This company has no indexed Drive files. That means either no Google "
            "Drive is connected, or the daily index has not run yet — it does NOT "
            "mean the user's Drive is empty. Say which, and point them at "
            "Settings → Connectors → Google Drive if nothing is connected."
        )
    return render_results(query, rows, total_indexed=total, limit=limit, indexed_at=indexed_at)


@function_tool
async def drive_find_files(query: str, limit: int = _DEFAULT_LIMIT) -> str:
    """Find documents in the company's Google Drive by title, in ONE call.

    Use this FIRST for any "find/where is/do we have a document about X"
    question. It searches a cached index of every file in the connected Drive,
    so it costs no Drive API call and returns immediately — do NOT enumerate
    folders or run several searches to narrow down; one call with the words
    that matter is the whole search.

    Returns ranked title matches with each file's kind, folder, modified date
    and ``file_id``. Titles that mark themselves superseded or old are listed
    LAST and flagged, so pick the live copy without opening both.

    Then open only the file you actually need, using its ``file_id`` with your
    Drive/Docs/Sheets tools.

    This searches TITLES, not file contents. An empty result means no title
    matched — never that the document does not exist.

    Args:
        query: What to look for — "maintenance SOP", "Smith lease 2026".
        limit: Max results (default 8, max 25).
    """
    return await _drive_find_files_impl(query, limit)


__all__ = [
    "drive_find_files",
    "duplicate_titles",
    "index_freshness",
    "version_families",
    "version_stem",
    "render_results",
    "score_title",
]
