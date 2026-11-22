"""Two Drive files with the same title, and a footer that says "open the one
you need".

`drive_find_files` already returns self-contained rows — kind, folder, modified
date, file_id, and a superseded marker where the title carries one. That was
gotchas 41/51's fix, applied when the tool was written. What was missing is the
CLOSING INSTRUCTION for the case those rows cannot settle: when two files carry
the *same* title, the list is a set of candidates under a footer telling the
model to open one, and opening one silently is a guess (coverage checklist
#119).

The harm is specific and this product has already done it once. `SOP-01
Maintenance Triage` living in both `/Operations` and `/Archive/2024` is
indistinguishable by name; quoting the archived copy's spend tiers as current
policy is gotcha 42's failure with the opposite sign — there the agent said the
Drive was empty, here it would say the wrong thing confidently.

⚠ **Name-both-and-ask, NOT prefer-the-newest.** Newest is usually right, and
"usually" is exactly what produces a confident wrong quote about a company's own
operating procedure. Where a copy marks itself superseded the ranking already
demotes it and the row already says so; this covers the case where neither does
and only the folder and the date differ.

⚠ **Fires only on an actual collision.** On every other search it is a line the
model learns to skip, and then it is absent exactly when it matters (gotcha 52).
Every assertion below has its negative twin.
"""

from __future__ import annotations

import ast
from pathlib import Path

from helixis.agents.drive_lookup_tools import duplicate_titles, render_results

_MODULE = Path("src/helixis/agents/drive_lookup_tools.py")

_ASK = "name each candidate WITH its folder and date and ask"


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


# ── what counts as a collision ───────────────────────────────────────


def test_the_same_title_in_two_folders_is_a_collision() -> None:
    rows = [
        _row("SOP-01 Maintenance Triage", path="/Operations"),
        _row("SOP-01 Maintenance Triage", path="/Archive/2024", modified="2024-02-11"),
    ]
    assert duplicate_titles(rows) == ["sop-01 maintenance triage"]


def test_case_and_spacing_do_not_hide_a_collision() -> None:
    """A title retyped in a different case is the same document name, and the
    reader cannot tell them apart either."""
    rows = [_row("SOP-01  Maintenance   Triage"), _row("sop-01 maintenance triage")]
    assert duplicate_titles(rows) == ["sop-01 maintenance triage"]


def test_distinct_titles_are_not_a_collision() -> None:
    rows = [_row("SOP-01 Maintenance Triage"), _row("SOP-02 Vendor Dispatch")]
    assert duplicate_titles(rows) == []


def test_punctuation_is_left_alone() -> None:
    """⚠ Deliberate. "Lease (2025)" and "Lease 2025" are plausibly different
    documents, and merging them would put the note on files a reader CAN tell
    apart — the cry-wolf direction (gotcha 52)."""
    assert duplicate_titles([_row("Lease (2025)"), _row("Lease 2025")]) == []


def test_an_untitled_row_is_not_a_collision_with_another() -> None:
    """Two rows the index has no title for are a data gap, not a naming
    ambiguity, and telling the model to "ask which they mean" about two blanks
    helps nobody."""
    rows = [{"file_id": "a", "title": None}, {"file_id": "b", "title": ""}]
    assert duplicate_titles(rows) == []


def test_each_colliding_title_is_reported_once() -> None:
    rows = [_row("Lease Renewal"), _row("Lease Renewal", path="/Old"), _row("Lease Renewal")]
    assert duplicate_titles(rows) == ["lease renewal"]


# ── what the specialist reads ────────────────────────────────────────


def test_the_note_appears_when_titles_collide() -> None:
    out = _render(
        [
            _row("SOP-01 Maintenance Triage", path="/Operations"),
            _row("SOP-01 Maintenance Triage", path="/Archive/2024", modified="2024-02-11"),
        ]
    )
    assert "More than one file here has the SAME title" in out
    assert _ASK in out


def test_the_note_is_absent_on_an_ordinary_search() -> None:
    out = _render([_row("SOP-01 Maintenance Triage"), _row("SOP-02 Vendor Dispatch")])
    assert "SAME title" not in out


def test_the_note_is_absent_on_a_single_hit() -> None:
    assert "SAME title" not in _render([_row("SOP-01 Maintenance Triage")])


def test_the_note_names_the_two_things_that_actually_differ() -> None:
    """A caveat that does not say how to settle it is just doubt (gotcha 46).
    The folder and the modified date are on every row already."""
    out = _render([_row("Lease Renewal"), _row("Lease Renewal", path="/Old")])
    note = out.split("SAME title", 1)[1]
    assert "folder" in note and "modified date" in note


def test_the_note_forbids_the_silent_pick() -> None:
    out = _render([_row("Lease Renewal"), _row("Lease Renewal", path="/Old")])
    assert "Do NOT open one silently" in out


def test_the_note_does_not_tell_the_model_to_prefer_the_newest() -> None:
    """⚠ The wording decision, pinned. "Take the most recent" is right often
    enough to be adopted and wrong often enough to quote an archived procedure
    as current policy."""
    out = _render([_row("Lease Renewal"), _row("Lease Renewal", path="/Old")])
    note = out.split("SAME title", 1)[1].lower()
    for tempting in ("most recent", "newest", "prefer the latest", "use the newer"):
        assert tempting not in note, (
            f"the note tells the model to {tempting!r} — a heuristic that is "
            "usually right, which is what makes a confident wrong quote"
        )


def test_the_rows_still_carry_what_distinguishes_them() -> None:
    """The note is only actionable because the folder and date are on the line.
    A note pointing at facts the list does not show would be worse than none."""
    out = _render(
        [
            _row("SOP-01 Maintenance Triage", path="/Operations", modified="2026-05-01"),
            _row("SOP-01 Maintenance Triage", path="/Archive/2024", modified="2024-02-11"),
        ]
    )
    assert "in /Operations" in out and "in /Archive/2024" in out
    assert "modified 2026-05-01" in out and "modified 2024-02-11" in out


def test_the_existing_contract_is_untouched() -> None:
    """Everything this tool already promised still holds — the change is
    additive or it is a regression."""
    out = _render([_row("SOP-01 Maintenance Triage")])
    assert "209 indexed file(s)" in out
    assert "TITLE matches from the daily index" in out
    assert "file_id:" in out


def test_an_empty_result_is_unchanged() -> None:
    out = render_results("nothing", [], total_indexed=209, limit=8)
    assert "do NOT tell the user the document does not exist" in out
    assert "SAME title" not in out


# ── it is wired into the renderer ────────────────────────────────────


def test_the_renderer_actually_consults_the_detector() -> None:
    """⚠ A behavioural test of `duplicate_titles` cannot see a renderer that
    stopped calling it — the note would simply never appear, which is the
    defect (gotchas 62/66/69)."""
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    fn = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "render_results"
        ),
        None,
    )
    assert fn is not None, "render_results not found"
    called = {
        n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "duplicate_titles" in called, (
        "render_results no longer asks whether any titles collide, so a list of "
        "identically-named files renders under a footer telling the model to "
        "open one"
    )


def test_the_note_is_conditional_not_unconditional() -> None:
    """⚠ Appended every time, this is a line the model sees on every Drive
    search and learns to skip (gotcha 52)."""
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    guard = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.If)
            and any(isinstance(c, ast.Name) and c.id == "_SAME_TITLE_NOTE" for c in ast.walk(n))
        ),
        None,
    )
    assert guard is not None, "the note is appended unconditionally"
    assert "duplicate_titles" in ast.unparse(guard.test)
