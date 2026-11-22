"""The one-PMS trigger covers every system in the registry (migrations 0107
and 0109; the file keeps its 0107 name).

WHY
    Every PMS writes its record ids into the same ``buildium_id`` column under
    ``UNIQUE (company_id, buildium_id)`` (ADR 0010 Decision (a)), so a company
    holding two systems' keys has colliding records silently overwrite each
    other. 0103's trigger checked Buildium against Rentvine only, so a company
    could have held Rent Manager keys and Buildium keys at once.

What ``test_one_pms_race.py`` already pins stays there, unchanged: the lock
comes first and is transaction-scoped, and the two original branches each
start with a bare ``if exists (`` on the other one's table. This file pins the
rest: every registry table is bound, every branch checks all the others, the
name it reports is that table's registry name, and a binding to a table the
function does not know is refused.

Like its sibling, this reads the migration CORPUS: it proves the fix is
written, not that it is applied (gotcha 8). Applying is recorded in
docs/migrations/applied.md.
"""

from __future__ import annotations

import re

import pytest

from helixis.pms.registry import PMS
from tests.unit.test_one_pms_race import MIGRATIONS, _live_definition, _strip_sql_comments

FUNCTION = "assert_one_pms_per_company"
_TABLES = {found.credentials_table: found.name for found in PMS.values()}


def _branches(body: str) -> dict[str, str]:
    """Each ``tg_table_name`` branch's text, keyed by the table that fires it.

    A branch runs from its ``tg_table_name = '<table>' then`` to the next
    branch or the ``else``. The inner checks are ``if … end if;`` blocks, so
    the split is on the outer keywords only: ``elsif tg_table_name`` and the
    ``else`` that is followed by the refusal.
    """
    out: dict[str, str] = {}
    marks = list(re.finditer(r"tg_table_name\s*=\s*'(\w+)'\s+then", body))
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else body.find("\n    else\n", mark.end())
        assert end != -1, "the last branch has no `else` after it"
        out[mark.group(1)] = body[mark.end() : end]
    return out


#: Each migration that created credentials tables and then replaced the
#: function, with the tables it created. Re-pointed 2026-10-06 (rule 19): 0109
#: added DoorLoop and now defines the function last; 0107's own ordering and
#: RLS are still checked, because an applied file is still the record.
_CREATED_BY = {
    "0107_": ("company_rentmanager_credentials", "company_propertyware_credentials"),
    "0109_": ("company_doorloop_credentials",),
}


def _migration(prefix: str) -> str:
    (path,) = MIGRATIONS.glob(f"{prefix}*.sql")
    return _strip_sql_comments(path.read_text(encoding="utf-8"))


def test_the_live_definition_is_the_five_way_one() -> None:
    """The LAST definition in the corpus is the one that wins on apply."""
    _, path = _live_definition()
    assert path.name.startswith("0109_"), (
        f"{path.name} now defines {FUNCTION} last — re-point this file at it, and "
        f"check it still covers every system in pms.registry"
    )


def test_every_registry_table_has_a_branch() -> None:
    body, path = _live_definition()
    assert set(_branches(body)) == set(_TABLES), (
        f"{path.name}: the branches and pms.registry disagree about which credentials tables exist"
    )


def test_each_branch_checks_every_other_system_under_a_bare_if_exists() -> None:
    """``if false and exists (`` would leave the check present and dead, which
    is the mutant 0103's sibling test was written against. Each other table
    must be read, and each read must be the whole condition."""
    body, path = _live_definition()
    for fired_by, branch in _branches(body).items():
        checks = re.findall(
            r"(\S[^\n]*)\n\s*select 1 from public\.(\w+)\s*\n\s*where company_id = new\.company_id",
            branch,
        )
        read = {table for _, table in checks}
        assert read == set(_TABLES) - {fired_by}, (
            f"{path.name}: the {fired_by} branch reads {sorted(read)}"
        )
        for condition, table in checks:
            assert condition.strip() == "if exists (", (
                f"{path.name}: the {fired_by} branch checks {table} under "
                f"{condition.strip()!r}; anything but a bare `if exists (` can "
                f"short-circuit it"
            )


def test_each_check_reports_the_registry_name_of_the_table_it_read() -> None:
    """The connect routes read this name back (``company_pms.connected_to``)
    and put it in front of a customer, so a check that found Rent Manager keys
    must not say Propertyware."""
    body, path = _live_definition()
    for fired_by, branch in _branches(body).items():
        pairs = re.findall(
            r"select 1 from public\.(\w+)\s*\n\s*where company_id = new\.company_id\s*\n"
            r"\s*\) then\s*\n\s*other_pms := '([^']+)';",
            branch,
        )
        assert len(pairs) == len(_TABLES) - 1, f"{path.name}: {fired_by} branch shape changed"
        for table, name in pairs:
            assert name == _TABLES[table], (
                f"{path.name}: finding {table} reports {name!r}, pms.registry calls it "
                f"{_TABLES[table]!r}"
            )


def test_a_binding_to_an_unknown_table_is_refused() -> None:
    """With no ``else``, a trigger bound to a table no branch names would let
    every insert through unchecked."""
    body, path = _live_definition()
    last_branch = list(re.finditer(r"tg_table_name\s*=\s*'\w+'\s+then", body))[-1]
    tail = body[last_branch.end() :]
    # Comment lines inside the `else` are blanked by the stripper.
    assert re.search(r"\n\s*else\s*\n\s*raise exception", tail), (
        f"{path.name}: no `else raise` after the last branch"
    )


def test_every_registry_table_binds_the_function() -> None:
    """A perfect function bound to all but one table leaves that one open."""
    corpus = "\n".join(
        _strip_sql_comments(p.read_text(encoding="utf-8")) for p in sorted(MIGRATIONS.glob("*.sql"))
    )
    for table in _TABLES:
        pattern = (
            rf"create\s+(?:or\s+replace\s+)?trigger\s+\w+\s+before\s+insert\s+or\s+update\s+of\s+"
            rf"company_id\s+"
            rf"on\s+public\.{table}\s+for\s+each\s+row\s+execute\s+function\s+"
            rf"public\.{FUNCTION}\(\)"
        )
        assert re.search(pattern, corpus, re.IGNORECASE), (
            f"no BEFORE INSERT OR UPDATE OF company_id trigger on {table} binds {FUNCTION}"
        )


def test_every_registry_table_is_created_by_one_of_them_or_before() -> None:
    """A registry system whose table no migration creates would make the live
    function name a table that does not exist."""
    corpus = "\n".join(
        _strip_sql_comments(p.read_text(encoding="utf-8")) for p in sorted(MIGRATIONS.glob("*.sql"))
    )
    for table in _TABLES:
        assert re.search(rf"create\s+table\s+(?:if\s+not\s+exists\s+)?public\.{table}\b", corpus), (
            f"no migration creates {table}"
        )


@pytest.mark.parametrize(("prefix", "tables"), sorted(_CREATED_BY.items()))
def test_the_new_tables_are_created_before_the_function_names_them(
    prefix: str, tables: tuple[str, ...]
) -> None:
    """plpgsql resolves a table when the statement runs. A function naming a
    table that does not exist yet fails every connect to every system, so each
    of these migrations creates its tables before it replaces the function."""
    text = _migration(prefix)
    function_at = text.find(f"create or replace function public.{FUNCTION}")
    assert function_at != -1, f"{prefix}: no longer replaces {FUNCTION}"
    for table in tables:
        created_at = text.find(f"create table if not exists public.{table}")
        assert -1 < created_at < function_at, f"{prefix}: {table} is not created first"


@pytest.mark.parametrize(("prefix", "tables"), sorted(_CREATED_BY.items()))
def test_the_new_credentials_tables_keep_rls_on(prefix: str, tables: tuple[str, ...]) -> None:
    """RLS on with no policy, like 0106: only the backend's privileged pool
    reads or writes a credentials row."""
    text = _migration(prefix)
    for table in tables:
        assert f"alter table public.{table} enable row level security" in text
        assert not re.search(rf"create\s+policy[^;]*on\s+public\.{table}\b", text), (
            f"{prefix}: {table} has a policy; members do not need to read credentials"
        )
