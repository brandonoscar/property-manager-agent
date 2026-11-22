"""The `@unverified` work list must count work, not prose.

Phases 4-6 build against a spec nobody has watched answer, and the plan's whole
bargain is that the unproven parts stay visible as literal ``@unverified``
comments until Phase 8 discharges them against a live account.

⚠ The plan makes Phase 8's gate ``grep -c '@unverified'`` returning 0, and that
gate CANNOT REACH ZERO today. Two independent reasons, both measured here
rather than argued: the grep counts prose that merely explains the convention,
and ``grep -r`` also counts ``__pycache__/*.pyc``. A gate nobody can satisfy
gets satisfied the wrong way — by deleting the explanation, or by deleting real
markers to make the number go down.

So the first test below is the important one: it pins the REAL current state of
the real package, and it is the case the grep gets wrong.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "src" / "helixis" / "rentvine"


def _module():
    path = REPO / "scripts" / "rentvine_unverified.py"
    spec = importlib.util.spec_from_file_location("_rv_unverified", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_rv_unverified"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tool():
    return _module()


# --------------------------------------------------------------------------
# the real package — this is the defect the plan's grep has
# --------------------------------------------------------------------------


def test_prose_about_the_marker_is_not_counted_as_work(tool):
    """Several modules mention the marker while EXPLAINING the convention.

    They are STRING tokens — module and class docstrings, and an error message
    telling a caller to tag their own call. None is an unproven assumption, and
    all would survive every real marker being settled.

    ⚠ **Re-pointed, not deleted.** This asserted ``all(... endswith
    "transport.py"))`` and a count of 2, which was true when transport.py was
    the only module in the package and became false the moment Phase 4 landed
    `client.py`/`mapper.py`/`sync.py` — each of which names the marker in its
    own docstring to say what the convention is. The FILENAME was a mechanism;
    the property is that a marker inside a string is classified as prose and
    reaches neither the work list nor the malformed list.
    """
    result = tool.scan(PACKAGE)
    assert result.prose, "the scan found no prose — check the root, this is the known case"
    # ⚠ Never the line numbers, and now never the filenames either. A line-keyed
    # assertion goes stale on any unrelated edit, which is the drift this repo
    # has re-pointed six times (gotchas 63/68/76/112/120/123).
    work_lines = {(m.path, m.line) for m in result.markers}
    bad_lines = {(m.path, m.line) for m in result.malformed}
    for m in result.prose:
        assert (m.path, m.line) not in work_lines, f"prose counted as work: {m.path}:{m.line}"
        assert (m.path, m.line) not in bad_lines, f"prose judged malformed: {m.path}:{m.line}"


def test_every_marker_in_the_real_package_is_well_formed(tool):
    """The durable property — NOT that the count is zero.

    ⚠ Asserting an empty work list would red-gate Phase 4 the moment it writes
    its first legitimate marker, which is exactly what the plan asks it to do.
    What must hold at every point is that each marker names its assumption and
    how Phase 8 settles it; the count is a work list, not a defect count.
    """
    malformed = tool.scan(PACKAGE).malformed
    assert malformed == [], "\n".join(f"{m.path}:{m.line} {m.problems}" for m in malformed)


def test_the_writes_module_keeps_its_assumptions_on_the_work_list():
    """``writes.py`` says its assumptions live where this scan looks. Four were
    first written inside docstrings, which the scan reads as PROSE, so none of
    them reached the Phase 8 list (gotcha 192). Inside a string the marker may
    only appear quoted (````@unverified````), as prose about the convention.

    Walks the tokens rather than the text (rule 17): a ``#`` comment is where a
    real marker belongs, and only STRING tokens are checked.
    """
    import io
    import tokenize

    path = PACKAGE / "writes.py"
    source = path.read_text(encoding="utf-8")
    strings = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type == tokenize.STRING
    ]
    for text in strings:
        bare = text.replace("``@unverified``", "")
        assert "@unverified" not in bare, f"an assumption inside a string is prose: {text[:80]!r}"
    assert len(strings) > 10, "the token walk found almost no strings; it is pointed at nothing"


def test_bytecode_is_never_scanned(tool):
    """``grep -rc`` matches ``__pycache__/*.pyc``; the glob here must not.

    Measured: the package tree really does contain ``.pyc`` files, so this is
    a live condition rather than a hypothetical — and ``grep -rc`` on it
    reports ``transport.cpython-311.pyc:2`` today.
    """
    files = tool._python_files(PACKAGE)
    assert files, "no source files found — the root is wrong"
    assert any((PACKAGE / "__pycache__").glob("*.pyc")), "no bytecode present; this proves nothing"
    assert all(p.suffix == ".py" for p in files)
    assert all("__pycache__" not in p.parts for p in files)


# --------------------------------------------------------------------------
# what counts as a marker
# --------------------------------------------------------------------------


GOOD = "# @unverified pageSize defaults differ per operation; Phase 8 settles it with one call\n"


def test_a_well_formed_comment_marker_is_work(tool):
    result = tool.scan_text(GOOD, "x.py")
    assert len(result.markers) == 1
    assert result.markers[0].problems == []
    assert result.prose == []


def test_a_bare_marker_is_malformed(tool):
    """A tag with no reason says something is unproven without saying what."""
    result = tool.scan_text("# @unverified\n", "x.py")
    assert len(result.markers) == 1
    assert any("no reason" in p for p in result.markers[0].problems)


def test_a_marker_that_does_not_say_how_it_is_settled_is_malformed(tool):
    """Phase 8 has a live account and an afternoon; it needs to know the call."""
    result = tool.scan_text("# @unverified the rate limit is not documented anywhere\n", "x.py")
    assert len(result.markers) == 1
    assert any("how it gets settled" in p for p in result.markers[0].problems)
    assert not any("no reason" in p for p in result.markers[0].problems)


def test_a_reason_continuing_onto_the_next_comment_line_is_read(tool):
    """The reason usually wraps; reading only the first line would reject it."""
    source = "# @unverified the envelope shape is a T2 claim and the\n# sandbox settles it\n"
    result = tool.scan_text(source, "x.py")
    assert result.markers[0].problems == []


def test_a_comment_block_broken_by_code_does_not_absorb_a_later_line(tool):
    """A cue on an unrelated comment further down must not satisfy the marker."""
    source = "# @unverified the rate limit is not documented anywhere\nx = 1\n# probe this later\n"
    result = tool.scan_text(source, "x.py")
    assert any("how it gets settled" in p for p in result.markers[0].problems)


def test_a_marker_inside_a_string_is_prose_not_work(tool):
    source = 'MSG = "catch this and mark the call @unverified"\n'
    result = tool.scan_text(source, "x.py")
    assert result.markers == []
    assert len(result.prose) == 1


def test_a_docstring_mentioning_the_marker_is_prose(tool):
    source = '"""Callers tag these @unverified until Phase 8 settles them."""\n'
    result = tool.scan_text(source, "x.py")
    assert result.markers == []
    assert len(result.prose) == 1


# --------------------------------------------------------------------------
# declining honestly, and the gates
# --------------------------------------------------------------------------


def test_a_file_that_does_not_tokenize_is_reported_not_treated_as_empty(tool):
    """An unparseable file is not a file with no markers (gotchas 55/57).

    ⚠ The fixture is an unterminated TRIPLE quote, measured rather than
    assumed: an unterminated single-quoted string and a bad indent both
    tokenize without raising, so either would have tested nothing while
    looking like it tested this.
    """
    result = tool.scan_text('x = """abc\n', "broken.py")
    assert result.unreadable, "a tokenize failure must be reported"
    assert result.markers == []


def test_the_format_gate_fails_on_a_malformed_marker(tool, tmp_path):
    (tmp_path / "m.py").write_text("# @unverified\n", encoding="utf-8")
    assert tool.main(["--root", str(tmp_path), "--check"]) == 1


def test_the_format_gate_passes_on_a_well_formed_one(tool, tmp_path):
    (tmp_path / "m.py").write_text(GOOD, encoding="utf-8")
    assert tool.main(["--root", str(tmp_path), "--check"]) == 0


def test_the_phase8_gate_fails_while_any_assumption_remains(tool, tmp_path):
    """Even a perfectly-formed marker means Phase 8 is not done."""
    (tmp_path / "m.py").write_text(GOOD, encoding="utf-8")
    assert tool.main(["--root", str(tmp_path), "--phase8"]) == 1


def test_the_phase8_gate_passes_only_when_the_work_list_is_empty(tool, tmp_path):
    (tmp_path / "m.py").write_text('"""prose mentioning @unverified only."""\n', encoding="utf-8")
    assert tool.main(["--root", str(tmp_path), "--phase8"]) == 0


def test_an_unreadable_file_fails_every_gate_rather_than_passing_it(tool, tmp_path):
    """A scan that could not read a file must not report a clean work list."""
    (tmp_path / "broken.py").write_text('x = """abc\n', encoding="utf-8")
    assert tool.main(["--root", str(tmp_path), "--phase8"]) == 2
    assert tool.main(["--root", str(tmp_path), "--check"]) == 2


def test_a_missing_root_is_empty_rather_than_an_exception(tool, tmp_path):
    result = tool.scan(tmp_path / "nope")
    assert result.markers == [] and result.prose == []
