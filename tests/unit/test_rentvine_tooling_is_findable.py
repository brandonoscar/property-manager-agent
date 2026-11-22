"""Every Rentvine instrument is reachable from CLAUDE.md, and says what it does.

WHY THIS READS THE CORPUS RATHER THAN CHECKING ONE SCRIPT
    The failure mode is "silently permits more" (gotcha 45): a new operator
    script lands, works perfectly, is tested, and nothing in the runbook names
    it — so the founder cannot find it during the one sandbox window it exists
    for. Nothing fails, nothing logs, and the instrument is worth zero.

    That is gotcha 154's shape (a complete six-suite gate whose only printed
    invocation lived in a gitignored report) and it recurred here the day
    after: ``scripts/rentvine_write_probe.py`` settles TWO of the markers
    ``rentvine_unverified.py --phase8`` prints, is the only thing in the repo
    that can ask those questions, and appeared in CLAUDE.md zero times.

    So the check has to be over the DIRECTORY, not over a list somebody typed.

THE SECOND CLAIM IS WHY THE FIRST IS NOT ENOUGH
    The §2 block that lists these scripts opened "(all read-only, …)". Adding
    the write probe to that list without touching the header would have filed a
    harness that POSTs to a customer's work order under a heading asserting it
    cannot — a worse outcome than leaving it unlisted, because an operator who
    reads the heading has been told the opposite of the truth (gotcha 48: a
    comment cannot fail). So a script that can write must be MARKED as one
    where it is listed.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
CLAUDE_MD = REPO / "CLAUDE.md"
#: Moved out of CLAUDE.md on 2026-09-24. The runbook holds the tooling list and
#: the operator blocks; the nested CLAUDE.md holds the module map. The chain
#: CLAUDE.md -> these two files is pinned below, so "findable" still means
#: reachable from the file every session loads.
RUNBOOK = REPO / "docs" / "runbooks" / "rentvine.md"
MODULE_MAP = REPO / "src" / "helixis" / "rentvine" / "CLAUDE.md"
SCRIPTS = sorted((REPO / "scripts").glob("rentvine_*.py"))


def _doc() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _repo_map() -> str:
    """The Rentvine module map's fence — where the map's CLAIMS live.

    Scoped rather than whole-document for the reason the marker-count test
    records: the gotcha entries quote defects verbatim to explain them.
    """
    text = MODULE_MAP.read_text(encoding="utf-8")
    start = text.index("## Module map")
    open_fence = text.index("```", start)
    close_fence = text.index("\n```", open_fence + 3)
    return text[open_fence:close_fence]


def _tooling_block() -> str:
    """The 'Rentvine operator tooling' list, verbatim."""
    text = _doc()
    start = text.index("#   Rentvine operator tooling")
    end = text.index("\n```", start)
    return text[start:end]


#: A list ENTRY is ``#     <script>.py  — <what it does>``, possibly continued on
#: following, more-indented lines. Anchoring on that shape matters: the header
#: above the list NAMES the write probe (in order to explain why the header no
#: longer claims the list is read-only), so a plain "first mention" search finds
#: the explanation and reads the wrong text as the entry. That is how the first
#: cut of this guard failed against correct code.
_ENTRY = re.compile(r"^\s*#\s+(rentvine_\w+\.py)\s+—", re.MULTILINE)


def _list_entry(name: str) -> str:
    """The block's own entry for ``name``, or "" when it has none."""
    block = _tooling_block()
    starts = [(m.start(), m.group(1)) for m in _ENTRY.finditer(block)]
    for i, (pos, found) in enumerate(starts):
        if found != name:
            continue
        end = starts[i + 1][0] if i + 1 < len(starts) else len(block)
        return block[pos:end]
    return ""


def _posts_to_rentvine(path: pathlib.Path) -> bool:
    """Does this script issue a POST?

    ⚠ AST, never a substring. Two of these files QUOTE ``"POST"`` in prose
    explaining that they do not send one — ``rentvine_probe.py``'s "the verb is
    not a variable" and ``rentvine_phase8.py``'s read-only claim — so a text
    scan reports the careful scripts as the dangerous ones, which is gotcha
    40's trap pointed at a safety classification. A comment is never an
    ``ast.Constant``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(isinstance(node, ast.Constant) and node.value == "POST" for node in ast.walk(tree))


def test_the_scan_finds_the_scripts_it_is_meant_to_judge() -> None:
    """A scan that collects nothing reports a clean corpus (gotchas 49/100/101/114).

    A renamed directory or a bad glob would make every assertion below pass
    against the empty set, which is the one way this guard could go quiet
    without anybody noticing.
    """
    assert len(SCRIPTS) >= 8, f"only found {len(SCRIPTS)} rentvine scripts — bad glob?"
    names = {p.name for p in SCRIPTS}
    assert "rentvine_write_probe.py" in names, "the script this guard was written for is missing"
    assert "rentvine_phase8.py" in names

    # And the entry parser: a regex that matched nothing would make the
    # write-marking check below vacuous for every script at once.
    listed = {m.group(1) for m in _ENTRY.finditer(_tooling_block())}
    assert len(listed) >= 6, f"the entry parser found only {sorted(listed)} — bad anchor?"
    assert "rentvine_write_probe.py" in listed

    # And §2's fence: a mis-found boundary returns a fragment, which makes the
    # marker-count check pass against text it never reached.
    repo_map = _repo_map()
    assert "@unverified` markers await Phase 8" in repo_map, "the map scope missed the tree"
    assert "docs/gotchas" not in repo_map, "the map scope leaked into the rules above it"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_every_rentvine_script_is_named_in_the_runbook(script: pathlib.Path) -> None:
    """An instrument nobody can find is worth what an unwritten one is worth.

    These all need the founder's own credentials and a live sandbox, so the
    window in which any of them can run is short and does not repeat cheaply.
    A script discovered afterwards is one that did not get used.
    """
    # Either file counts: together they are what CLAUDE.md used to hold, so
    # this is the old "named anywhere in CLAUDE.md" property, re-pointed.
    findable = _doc() + MODULE_MAP.read_text(encoding="utf-8")
    assert script.name in findable, (
        f"{script.name} is not named in {RUNBOOK.name} or the module map, so an operator "
        f"reading the runbook cannot know it exists. Add it to the Rentvine "
        f"tooling list (and give it a block if it takes an operator's own run)."
    )


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_a_script_that_writes_is_marked_as_one_where_it_is_listed(
    script: pathlib.Path,
) -> None:
    """The list must not describe a write harness as though it only reads.

    MEASURED at the time of writing: exactly one of nine issues a POST. That
    asymmetry is what made "(all read-only, …)" survive as a header — it was
    true of everything anybody had checked.
    """
    if not _posts_to_rentvine(script):
        return
    entry = _list_entry(script.name)
    assert entry, f"{script.name} writes and has no entry in the tooling list"
    assert "WRITES" in entry.upper(), (
        f"{script.name} POSTs to Rentvine and its entry does not say so. An "
        f"operator scanning this list decides from it which harnesses are safe "
        f"to point at a live account."
    )


def test_the_tooling_header_does_not_claim_the_list_is_read_only() -> None:
    """⚠ SCOPED TO THE HEADER LINE, and the reason is this guard's own text.

    The corrected header explains the old "all read-only" claim in order to
    stop somebody restoring it, so a whole-block scan for that phrase fires on
    the sentence that exists to prevent the defect — gotcha 40, which this repo
    has now recorded more than a dozen times. The CLAIM lives on the header
    line and the explanation does not.

    ⚠ What this cannot see, stated rather than implied: a reflow that moves the
    explanation onto the header line would fail this loudly. That direction is
    the acceptable one (gotcha 114) — a false alarm gets read, a quiet
    blindness does not.
    """
    header = _tooling_block().splitlines()[0]
    assert "read-only" not in header.lower(), (
        "the §2 tooling header claims the list is read-only, and "
        "rentvine_write_probe.py POSTs to a real work order"
    )


def test_the_marker_note_cites_no_count() -> None:
    """The number went stale twice in a week, beside its own do-not-cite rule.

    ⚠ TWO scopings, and each was wrong before it was right.

    The pattern matches the CLAIM's shape ("⚠ 16 `@unverified` markers") rather
    than the presence of a digit, because the corrected note names 16 and 15
    while explaining why neither is written as a total — a digit scan flags the
    fix.

    And it reads §2's MAP, not the whole document. The first cut scanned
    everything and went red the moment gotcha 161 was written, because a §5
    entry quotes the defect verbatim in order to explain it. That is gotcha 40
    arriving inside the guard for the very trap it describes — and it is the
    distinction ``rentvine_unverified.py`` already draws when it separates
    real markers from the "13 mention(s) of the marker inside strings — PROSE,
    not work". §5 is prose about defects by design; the CLAIM lives in the map.
    """
    stale_claim = re.compile(r"⚠\s*\d+\s*`@unverified`\s*markers")
    assert not stale_claim.search(_repo_map()), (
        "the repo map states a marker total. It goes stale the next time a "
        "session settles one; `rentvine_unverified.py --phase8` prints it."
    )


# ------------------------------------------------------- the §4 write-probe block


def _write_probe_block() -> str:
    text = _doc()
    start = text.index("### Settle the two Rentvine WRITE questions")
    end = text.find("\n### ", start + 1)
    return text[start:] if end == -1 else text[start:end]


def test_every_invocation_the_write_probe_block_prints_is_one_argparse_accepts() -> None:
    """gotcha 156's rule, applied to the block that modifies a live record.

    ⚠ The flags are read off the PARSER rather than a hand-typed list, because
    a second copy of the names is the drift being guarded against. A renamed
    flag here leaves an operator with a command argparse refuses, mid-sandbox,
    on the one harness whose failure mode is a modified customer record.
    """
    script = REPO / "scripts" / "rentvine_write_probe.py"
    known: set[str] = set()
    for node in ast.walk(ast.parse(script.read_text(encoding="utf-8"))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            known.add(str(node.args[0].value))
    assert known, "found no flags at all — the scan is pointed at the wrong thing"

    block = _write_probe_block()
    invocations = [
        line.strip() for line in block.splitlines() if line.strip().startswith("python scripts/")
    ]
    assert invocations, "the block prints no invocation to check"
    for line in invocations:
        parts = line.split()
        assert (REPO / parts[1]).exists(), f"{line!r} names a script that does not exist"
        for flag in (p for p in parts[2:] if p.startswith("--")):
            assert flag in known, f"{line!r} passes {flag}, which argparse does not accept"


def test_the_block_names_the_gate_and_what_a_failed_restore_costs() -> None:
    """A runbook for a harness that modifies a record owes three facts.

    Without the acknowledgement flag the operator meets an exit 2 they read as
    the harness being broken; without exit 1 naming a MODIFIED record they read
    a real finding as an ordinary failure and move on; without the disposable
    requirement they point it at a record somebody depends on.
    """
    block = _write_probe_block()
    assert "--i-accept-the-write" in block, "the acknowledgement flag must be printed"
    assert "MODIFIED" in block, "exit 1 must name what it leaves behind"
    assert "DISPOSABLE" in block.upper(), "the record requirement must be stated"
    assert "--check" in block, "the free mode has to be reachable from the block"


def test_the_block_names_the_witness_constant_rather_than_its_value() -> None:
    """⚠ Naming the NUMBER would be the marker-count defect again, one block over.

    ``MIN_WITNESS_FIELDS`` is a judgement about how many populated fields make
    a full-replace distinguishable from a partial update; if it is ever raised,
    a runbook quoting today's digit sends an operator to nominate a record the
    harness will refuse.
    """
    block = _write_probe_block()
    assert "MIN_WITNESS_FIELDS" in block
    src = (REPO / "scripts" / "rentvine_write_probe.py").read_text(encoding="utf-8")
    assert "MIN_WITNESS_FIELDS" in src, "the runbook names a constant the script does not define"


def test_claude_md_links_both_rentvine_files() -> None:
    """Findable means reachable from the file every session loads.

    Moving the list out of CLAUDE.md is only safe while CLAUDE.md still points
    at where it went; without this the move re-creates gotcha 161 (an
    instrument named nowhere a session will read).
    """
    root = CLAUDE_MD.read_text(encoding="utf-8")
    assert "docs/runbooks/" in root and "rentvine.md" in root, "CLAUDE.md lost the runbook link"
    assert "src/helixis/rentvine/CLAUDE.md" in root, "CLAUDE.md lost the module-map link"
