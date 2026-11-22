"""Two files that look like versions of one document, and neither says so.

The module's own docstring names the pair the index already solves: `SOP-01
Maintenance Triage` beside `SOP-01 Maintenance Triage v2.0 (SUPERSEDED - do not
use)`. The marker is in the title, `score_title` demotes it, and `_fmt_row`
says so on the row. That case is handled.

The case that was not is the one where NEITHER copy announces itself: `SOP-01
Triage v1` beside `SOP-01 Triage v2`. The ranking has no preference to express,
the rows read as two ordinary documents, and the model opens whichever came
first (coverage checklist #122).

⚠ **Not "take the highest number".** A v2 abandoned in a drafts folder loses to
the v1 somebody actually maintains, and nothing in a title index can tell which
is which. Same reasoning as the same-title note, arriving exactly where the
numbers make guessing feel justified.

⚠ **The exclusions are the design, and they are what this file mostly tests.**
A bare trailing number is not a version: "Q1 Report"/"Q2 Report" are two
reports, "Lease 2024"/"Lease 2025" are two leases with different terms. A
version warning on ordinary filing is one people stop reading (gotcha 52), so
only an explicit v/rev/version/final, or Drive's own "(2)" duplicate suffix,
counts — and the copy number is capped below 1000 so a bracketed year is not
mistaken for one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from helixis.agents.drive_lookup_tools import (
    _normalised_title,
    render_results,
    version_families,
    version_stem,
)

_MODULE = Path("src/helixis/agents/drive_lookup_tools.py")


def _row(title: str, *, path: str = "/Operations", modified: str = "2026-05-01") -> dict:
    return {
        "file_id": f"id-{abs(hash((title, path))) % 10000}",
        "title": title,
        "mime_type": "application/vnd.google-apps.document",
        "path": path,
        "modified_at": modified,
    }


def _render(rows: list[dict]) -> str:
    return render_results("sop triage", rows, total_indexed=209, limit=8)


# ── stripping the marker ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("title", "stem"),
    [
        ("SOP-01 Triage v2", "sop-01 triage"),
        ("SOP-01 Triage V2.0", "sop-01 triage"),
        ("SOP-01 Triage (v3)", "sop-01 triage"),
        ("SOP-01 Triage rev 4", "sop-01 triage"),
        ("SOP-01 Triage revision 4", "sop-01 triage"),
        ("SOP-01 Triage version 2", "sop-01 triage"),
        ("SOP-01 Triage - FINAL", "sop-01 triage"),
        ("Budget (2)", "budget"),
        # ⚠ Stacked markers, stripped repeatedly. Stripping only the last would
        # leave "sop-01 triage v2", which does not match its own v1 and the
        # pair goes unnoticed.
        ("SOP-01 Triage v2 final", "sop-01 triage"),
    ],
)
def test_a_version_marker_is_stripped(title: str, stem: str) -> None:
    got, marked = version_stem(title)
    assert got == stem
    assert marked is True


@pytest.mark.parametrize(
    "title",
    [
        # ⚠ Every one of these must keep its trailing token. These are the
        # false positives that would put a version warning on ordinary filing.
        "Q1 Report",
        "Lease 2025",
        "Unit 4B",
        "Report (2024)",
        "Building 12",
        "SOP-01 Triage",
    ],
)
def test_an_ordinary_title_is_left_whole(title: str) -> None:
    stem, marked = version_stem(title)
    assert stem == " ".join(title.split()).casefold()
    assert marked is False


def test_a_title_that_is_only_a_marker_is_not_reduced_to_nothing() -> None:
    """ "final.docx" stripped to "" would collide with every other empty stem
    and flag unrelated files as versions of each other."""
    stem, _ = version_stem("final")
    assert stem == "final"


# ── what counts as a conflict ────────────────────────────────────────


def test_two_numbered_versions_are_a_conflict() -> None:
    assert version_families([_row("SOP-01 Triage v1"), _row("SOP-01 Triage v2")]) == [
        "sop-01 triage"
    ]


def test_a_bare_copy_beside_a_numbered_one_is_a_conflict() -> None:
    assert version_families([_row("SOP-01 Triage"), _row("SOP-01 Triage v2")]) == ["sop-01 triage"]


def test_drives_own_duplicate_suffix_is_a_conflict() -> None:
    assert version_families([_row("Budget"), _row("Budget (2)")]) == ["budget"]


@pytest.mark.parametrize(
    "titles",
    [
        # ⚠ The direction that decides whether this survives real traffic.
        ("Q1 Report", "Q2 Report"),
        ("Lease 2024", "Lease 2025"),
        ("Report (2024)", "Report (2025)"),
        ("SOP-01 Triage", "SOP-02 Dispatch"),
        ("Unit 4B Inspection", "Unit 5A Inspection"),
    ],
)
def test_ordinary_filing_is_not_a_conflict(titles: tuple[str, str]) -> None:
    assert version_families([_row(titles[0]), _row(titles[1])]) == []


def test_a_shared_stem_with_no_marker_anywhere_is_not_a_conflict() -> None:
    """⚠ The marked requirement, pinned — but NOT by the mechanism it looks
    like. Two identical files are `duplicate_titles`' job and get different
    advice, and what excludes them here is the DISTINCTNESS test, not a marks
    lookup: see the twin below for why there is no longer a marks lookup."""
    assert version_families([_row("Budget"), _row("Budget")]) == []


def test_an_unmarked_title_normalises_identically_in_both_places() -> None:
    """⚠ Found by MUTATION, and it deleted code rather than adding a test.

    `version_families` used to carry an explicit `and marks.get(stem)`.
    Removing it left all 35 guards green — so the first question was gotcha
    58's: can the mutation change any output? Measured, not argued: 2,300
    exhaustive three-row corpora and 30,000 random four-row ones, zero
    disagreements. It could not.

    The reason is this invariant. `version_stem` strips nothing from an
    unmarked title, so it reduces to exactly what `_normalised_title`
    produces — which means a family where nobody is marked holds ONE distinct
    title and the distinctness test has already excluded it.

    So this is the guard that makes the deletion safe. Teach `_normalised_title`
    to strip punctuation and "Lease (2025)"/"Lease 2025" become two distinct
    titles sharing a stem with neither marked — a version warning on two
    different leases, the cry-wolf direction (gotcha 52). This fails first.
    """
    for title in ("Q1 Report", "Lease 2025", "SOP-01  Maintenance   Triage", "Report (2024)"):
        stem, marked = version_stem(title)
        assert marked is False, f"{title!r} is version-marked — wrong fixture for this invariant"
        assert stem == _normalised_title({"title": title}), (
            f"{title!r} normalises to {stem!r} here and "
            f"{_normalised_title({'title': title})!r} in the same-title rule. The two have "
            "diverged, so an UNMARKED pair can now share a stem while carrying different "
            "titles — and version_families would put a revision warning on ordinary filing. "
            "Restore the `and marks.get(stem)` requirement in version_families before "
            "changing either normaliser."
        )


def test_two_untitled_rows_are_not_versions_of_each_other() -> None:
    """⚠ Found by MUTATION, and the mutation was the SECOND redundant guard in
    this function. Two rows the index has no title for both reduce to the empty
    stem, so without a check they collide and the note fires on two blanks —
    the cry-wolf direction (gotcha 52), on rows that are a data gap rather than
    a naming ambiguity. `duplicate_titles` makes exactly the same call.

    ⚠ BOTH explicit blank checks were then deleted, because both proved
    undetectable: 680 exhaustive three-row corpora over a blank-heavy
    vocabulary and 30,000 random four-row ones, zero disagreements. A blank
    stem only ever comes from a blank TITLE, so every blank row carries the
    same empty normalised title and the distinctness test has already excluded
    them. That equivalence rests on `version_stem` never reducing a real title
    to nothing, which `test_a_title_that_is_only_a_marker_is_not_reduced_to_
    nothing` pins — so this test asserts the BEHAVIOUR, which is what has to
    hold however the exclusion is implemented.
    """
    assert version_families([{"file_id": "a", "title": None}, {"file_id": "b", "title": ""}]) == []
    assert version_families([{"file_id": "a", "title": ""}, _row("Plan v2")]) == []
    # ...and a real family beside a blank row is still reported.
    assert version_families([{"file_id": "a"}, _row("Plan v1"), _row("Plan v2")]) == ["plan"]


def test_one_marked_file_alone_is_not_a_conflict() -> None:
    """A v2 with no sibling in the list is just a document."""
    assert version_families([_row("SOP-01 Triage v2"), _row("Vendor List")]) == []


def test_a_family_is_reported_once() -> None:
    rows = [_row("Plan v1"), _row("Plan v2"), _row("Plan v3")]
    assert version_families(rows) == ["plan"]


# ── what the specialist reads ────────────────────────────────────────


def test_the_note_appears_on_a_version_pair() -> None:
    out = _render(
        [
            _row("SOP-01 Triage v1", path="/Operations"),
            _row("SOP-01 Triage v2", path="/Drafts", modified="2026-08-20"),
        ]
    )
    assert "look like VERSIONS of the same document" in out
    assert "NEITHER marks itself superseded" in out


def test_the_note_is_absent_on_an_ordinary_search() -> None:
    assert "VERSIONS" not in _render([_row("Q1 Report"), _row("Q2 Report")])


def test_the_note_refuses_the_highest_number_heuristic() -> None:
    """⚠ The wording decision, pinned — and it is the whole reason the note
    exists rather than a sort order."""
    out = _render([_row("Plan v1"), _row("Plan v2")])
    note = out.split("VERSIONS", 1)[1]
    assert "higher version number is not proof" in note
    assert "drafts folder" in note


def test_both_notes_can_appear_together() -> None:
    """⚠ Separate conditions, not an elif. A list can hold an identical pair
    AND a versioned pair, and they need different instructions — the same-title
    note points at folder and date, this one says a number is not authority."""
    out = _render(
        [
            _row("Vendor List", path="/A"),
            _row("Vendor List", path="/B"),
            _row("Plan v1"),
            _row("Plan v2"),
        ]
    )
    assert "SAME title" in out
    assert "VERSIONS" in out


def test_the_superseded_pair_is_still_handled_by_the_existing_rule() -> None:
    """The corpus's real pair marks itself, so the ranking and the row already
    say so — this note deliberately does NOT fire on it, because a second
    warning about a file that already carries one is noise."""
    out = _render(
        [
            _row("SOP-01 Maintenance Triage"),
            _row("SOP-01 Maintenance Triage v2.0 (SUPERSEDED - do not use)"),
        ]
    )
    assert "marks itself superseded" in out
    assert "VERSIONS" not in out


def test_the_existing_contract_is_untouched() -> None:
    out = _render([_row("SOP-01 Triage")])
    assert "209 indexed file(s)" in out
    assert "TITLE matches from the daily index" in out
    assert "VERSIONS" not in out and "SAME title" not in out


# ── it is wired into the renderer ────────────────────────────────────


def test_the_renderer_consults_the_detector() -> None:
    """⚠ A behavioural test of `version_families` cannot see a renderer that
    stopped calling it (gotchas 62/66/69)."""
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    fn = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "render_results"
        ),
        None,
    )
    assert fn is not None
    called = {
        n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "version_families" in called


def test_the_two_notes_are_independent_branches() -> None:
    """Pins the elif-collapse the docstring warns about: each note has its own
    `if`, so a list carrying both conditions prints both."""
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    ifs = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.If)
        and any(
            isinstance(c, ast.Name) and c.id in {"_SAME_TITLE_NOTE", "_VERSION_CONFLICT_NOTE"}
            for c in ast.walk(n)
        )
    ]
    assert len(ifs) == 2, f"expected two independent branches, found {len(ifs)}"
    assert all(not node.orelse for node in ifs), (
        "the two notes share an if/else, so a list carrying both an identical "
        "pair and a versioned pair prints only one of the two instructions"
    )
