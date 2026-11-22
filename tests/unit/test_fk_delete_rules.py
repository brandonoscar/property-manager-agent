"""Every foreign key into `auth.users` or `public.companies` states what a
delete does to it.

WHY THIS EXISTS
---------------
`memory_audit.user_id` (0023) referenced `auth.users(id)` with no `on delete`
clause. An omitted rule is NO ACTION, which REFUSES the parent delete — so
deleting an auth user who had ever touched memory failed with a foreign key
violation. Measured on helixis-test 2026-09-09: 881 of 902 audit rows carried a
user, across 7 of the project's 15 accounts.

Nothing could have caught it. It is one missing clause in a CREATE TABLE whose
very next column has the clause, it produces no error until somebody deletes a
user, and no product code path deletes one yet — so the first time it fires is
the right-to-erasure request that has to succeed.

WHAT THIS ASSERTS, AND WHY THAT SHAPE
-------------------------------------
Not "everything cascades" — four columns are deliberately SET NULL
(`uploaded_by` / `created_by` / `updated_by` / chat `user_id`) because they are
ATTRIBUTION on a record that outlives the person. The rule is weaker and more
useful: the delete behaviour must be *stated*. A reviewer can argue with a
stated choice; they cannot see an omitted one.

⚠ **Corpus-wide, not per-file** — the same lesson
`test_public_views_do_not_bypass_rls` records. An applied migration cannot be
edited, so a fix always arrives in a LATER file: 0023 declares the bad
constraint and 0095 repairs it. A scan that judged each migration in isolation
would fail forever on a defect that is already fixed, and the natural way to
quiet it is to add an exemption — which is how a guard goes blind.

⚠ **Reads the migration SQL, not the live database.** A live catalogue read
proves what helixis-test happens to hold today; this proves what a fresh
install gets, which is the thing a new deployment actually runs. The live check
was done once, by hand, and is recorded above.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"

#: The two parents whose children this guard covers.
#:
#: ⚠ Deliberately NOT every foreign key in the schema. These are the two whose
#: deletion is a real product event — a person exercising erasure, and a
#: company being removed on disconnect (ToS 18). A FK between two mirror tables
#: has no such event, and widening this to all of them would bury the two that
#: matter under dozens that never fire.
_PARENTS = ("auth.users", "public.companies")

#: `references <parent>(<col>)` plus the rest of that clause, so the delete
#: rule can be looked for in the tail.
#:
#: ⚠ The tail runs to the next `,` or `;` and CROSSES NEWLINES. The first cut
#: stopped at the line end (`[^,\n]*`) and immediately reported
#: `0010_readd_activity_log_company_id_fk.sql` — which is correct SQL whose
#: `on delete cascade` simply sits on the following line. That is gotcha 49's
#: trap: the line-bounded form does not go quietly blind, it cries wolf on
#: every properly-wrapped statement, and the natural way to quiet it is an
#: exemption that later hides a real one.
#:
#: Bounded at 200 characters so a column with no trailing comma cannot run on
#: into the next statement and borrow ITS delete rule.
_REF_RE = re.compile(
    r"references\s+(auth\.users|public\.companies)\s*\([^)]*\)([^,;]{0,200})",
    re.IGNORECASE,
)

#: An explicit rule, in any of the spellings Postgres accepts.
_ON_DELETE_RE = re.compile(
    r"on\s+delete\s+(cascade|set\s+null|set\s+default|restrict|no\s+action)", re.IGNORECASE
)

#: A repair in a later migration: `add constraint … on delete …`, or an
#: `alter column … drop not null` paired with one. Matching the constraint NAME
#: is what links a repair to the declaration it fixes.
_REPAIR_RE = re.compile(
    r"add\s+constraint\s+(\w+)\s+foreign\s+key[^;]*?on\s+delete\s+(cascade|set\s+null|set\s+default|restrict)",
    re.IGNORECASE | re.DOTALL,
)


def _sql_files() -> list[Path]:
    files = sorted(_MIGRATIONS.glob("*.sql"))
    assert files, f"no migrations found under {_MIGRATIONS} — wrong path or empty glob"
    return files


def _strip_comments(sql: str) -> str:
    """Drop `--` lines.

    ⚠ Not cosmetic. This file's own migration (0095) QUOTES the broken
    declaration in its header in order to explain it, so a scan over raw text
    finds `references auth.users(id),` with no rule and reports the fix as the
    defect — gotcha 40/59's trap, arriving in SQL for the third time.
    """
    return "\n".join(line.split("--")[0] for line in sql.splitlines())


def _repairs() -> set[str]:
    """Constraint names that some migration gives an explicit delete rule."""
    found: set[str] = set()
    for path in _sql_files():
        for name, _rule in _REPAIR_RE.findall(_strip_comments(path.read_text(encoding="utf-8"))):
            found.add(name.lower())
    return found


def _declarations() -> list[tuple[Path, int, str, str]]:
    """(file, line, parent, tail) for every reference to a covered parent."""
    out: list[tuple[Path, int, str, str]] = []
    for path in _sql_files():
        body = _strip_comments(path.read_text(encoding="utf-8"))
        for match in _REF_RE.finditer(body):
            line = body[: match.start()].count("\n") + 1
            out.append((path, line, match.group(1).lower(), match.group(2)))
    return out


def test_the_scan_finds_the_references_it_is_meant_to_guard() -> None:
    """A bad glob or a broken regex collects nothing and reports green.

    The floor is deliberately generous — this asserts the scanner is WORKING,
    not how many tables exist, so adding or removing one does not fail here.
    """
    decls = _declarations()
    assert len(decls) >= 30, (
        f"expected the corpus to declare many FKs to {_PARENTS}, found {len(decls)}"
    )
    parents = {p for _f, _l, p, _t in decls}
    assert parents == set(_PARENTS), f"scanner did not see both parents: {parents}"


def test_every_reference_states_what_a_delete_does() -> None:
    """The guard itself.

    A reference is compliant when its own declaration carries `on delete`, OR
    when a later migration names its constraint and gives it one.
    """
    repaired = _repairs()
    silent: list[str] = []
    for path, line, parent, tail in _declarations():
        if _ON_DELETE_RE.search(tail):
            continue
        # The convention Postgres itself uses for an unnamed constraint.
        # `memory_audit.user_id` -> `memory_audit_user_id_fkey`.
        if any(name in repaired for name in _plausible_names(path, line, parent)):
            continue
        silent.append(f"{path.name}:{line} references {parent} with no ON DELETE rule")

    assert not silent, (
        "A foreign key into a parent that really gets deleted must say what "
        "happens to its children. An omitted rule is NO ACTION, which REFUSES "
        "the delete — that is how erasure of a real person fails.\n  " + "\n  ".join(silent)
    )


def _plausible_names(path: Path, line: int, parent: str) -> set[str]:
    """Constraint names a repair migration might use for this reference.

    ⚠ Derived from the surrounding SQL rather than assumed, because a repair
    has to be matched to the declaration it fixes and Postgres's default name
    is `<table>_<column>_fkey`.
    """
    body = _strip_comments(path.read_text(encoding="utf-8"))
    lines = body.splitlines()
    column = ""
    if 0 < line <= len(lines):
        column = lines[line - 1].strip().split()[0] if lines[line - 1].strip() else ""
    table = ""
    for prior in reversed(lines[:line]):
        m = re.search(
            r"create\s+table\s+(?:if\s+not\s+exists\s+)?(?:public\.)?(\w+)", prior, re.IGNORECASE
        )
        if m:
            table = m.group(1)
            break
    names = set()
    if table and column:
        names.add(f"{table}_{column}_fkey".lower())
    return names


@pytest.mark.parametrize("parent", _PARENTS)
def test_a_missing_rule_is_actually_detected(parent: str) -> None:
    """Mutation in test form: the scanner must FLAG a silent declaration.

    Without this, a regex that matched nothing would satisfy the guard above
    vacuously — gotcha 45's rule, that a control whose failure mode is
    'silently permits more' needs a test measuring what it permitted.
    """
    sample = (
        f"create table public.probe (\n    who uuid not null references {parent}(id),\n"
        "    x int\n);"
    )
    body = _strip_comments(sample)
    matches = list(_REF_RE.finditer(body))
    assert matches, f"scanner failed to see a reference to {parent} at all"
    assert not _ON_DELETE_RE.search(matches[0].group(2)), (
        "a rule-less declaration must read as rule-less"
    )


@pytest.mark.parametrize(
    "rule",
    ["on delete cascade", "on delete set null", "ON DELETE CASCADE", "on  delete   set   null"],
)
def test_a_stated_rule_is_accepted_in_every_spelling(rule: str) -> None:
    """The other direction — a guard that flags correct SQL gets switched off."""
    sample = (
        f"create table public.probe (\n    who uuid references auth.users(id) {rule},\n"
        "    x int\n);"
    )
    matches = list(_REF_RE.finditer(_strip_comments(sample)))
    assert matches, "scanner failed to see the reference"
    assert _ON_DELETE_RE.search(matches[0].group(2)), f"{rule!r} should read as a stated rule"


def test_a_rule_on_the_following_line_is_still_a_stated_rule() -> None:
    """The false positive this guard shipped with, kept as a test.

    `0010_readd_activity_log_company_id_fk.sql` writes the reference and its
    rule on separate lines inside a DO block — perfectly ordinary SQL. The
    first version of `_REF_RE` bounded the tail at the newline and reported it
    as a defect. Formatting must not change a verdict (gotcha 40/49).
    """
    sample = (
        "alter table public.probe\n"
        "    add constraint probe_company_id_fkey\n"
        "    foreign key (company_id)\n"
        "    references public.companies(id)\n"
        "    on delete cascade;"
    )
    matches = list(_REF_RE.finditer(_strip_comments(sample)))
    assert matches, "scanner failed to see the wrapped reference"
    assert _ON_DELETE_RE.search(matches[0].group(2)), "a rule on the next line is still a rule"


def test_the_tail_cannot_borrow_a_rule_from_the_next_statement() -> None:
    """The over-capture the 200-character bound exists to prevent.

    A column with no trailing comma runs to the next `;`. If that window were
    unbounded, a silent declaration could read as compliant by picking up an
    unrelated `on delete` from a later statement — a false GREEN, which is the
    direction that matters.
    """
    sample = (
        "create table public.probe (\n"
        "    who uuid references auth.users(id)\n"
        ");\n"
        + "-- filler\n"
        * 40
        + "alter table public.other add constraint other_fkey foreign key (c)\n"
        "    references public.companies(id) on delete cascade;"
    )
    body = _strip_comments(sample)
    first = list(_REF_RE.finditer(body))[0]
    assert first.group(1).lower() == "auth.users"
    assert not _ON_DELETE_RE.search(first.group(2)), (
        "the silent declaration borrowed a rule from a later statement — widen the "
        "bound and this guard starts passing declarations it should flag"
    )


def test_memory_audit_is_the_case_this_guard_was_written_for() -> None:
    """0023 declares it silently; 0095 must be the file that repairs it.

    Pinned by NAME rather than left to the general scan, because this is the
    one instance that was live and the general scan would go quiet if 0095 were
    reverted for an unrelated reason.
    """
    fix = (_MIGRATIONS / "0095_memory_audit_user_delete_rule.sql").read_text(encoding="utf-8")
    body = _strip_comments(fix)
    assert "memory_audit_user_id_fkey" in body, "0095 must name the constraint it repairs"
    assert re.search(r"on\s+delete\s+set\s+null", body, re.IGNORECASE), (
        "0095 must state the new rule"
    )
    assert re.search(r"drop\s+not\s+null", body, re.IGNORECASE), (
        "SET NULL cannot fire against a NOT NULL column — dropping it is part of the fix, "
        "not incidental. Without it the rule reads as applied and the delete stays blocked."
    )
