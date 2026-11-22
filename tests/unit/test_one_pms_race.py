"""The one-PMS invariant is what makes sharing `buildium_id` safe, and it raced.

WHY THIS MATTERS MORE THAN IT LOOKS
    ADR 0010 Decision (a) puts BOTH systems' record ids in the same
    ``buildium_id`` column under ``UNIQUE (company_id, buildium_id)``. Migration
    0100 states the consequence itself: if a company ever held both PMSes, a
    colliding id "would SILENTLY OVERWRITE one with the other — no error, no
    log, one row where there were two". So the invariant is not hygiene, it is
    the only thing standing between the shared-identity design and silent data
    loss.

    `assert_one_pms_per_company` enforces it by reading the OTHER credentials
    table. That is a cross-table check-then-act, and under READ COMMITTED — what
    this product runs — two concurrent connects each read a snapshot that cannot
    see the other's uncommitted row, both pass, and both commit.

MEASURED against real PostgreSQL 16.13 with the product's own asyncpg 0.31.0,
with a control, before and after:

    AS SHIPPED   control (sequential) REFUSED  ·  race 2/2 landed, both PMSes
                 two properties at one id -> mirror holds 1 row
    WITH 0103    control (sequential) REFUSED  ·  race 1/2 landed, one PMS

⚠ WHAT THESE TESTS CAN AND CANNOT DO. There is no database in CI, so they read
the migration CORPUS — they prove the fix is WRITTEN and correctly ordered, not
that it is APPLIED (gotcha 8) and not that it serialises (that needs two live
connections). `migration_check.py` is blind to this one either way: it matches
`create table`/`add column` against the live catalog and 0103 adds neither, so
the schema sweep reports green whether or not it ever ran (gotchas 53/59).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "supabase" / "migrations"
FUNCTION = "assert_one_pms_per_company"


def _strip_sql_comments(sql: str) -> str:
    """⚠ Load-bearing, and the reason is this file's own subject matter.

    0103 QUOTES ``for update`` and ``pg_advisory_xact_lock`` in its header in
    order to explain why one was chosen over the other. A whole-file scan
    therefore fires on the correct migration — the trap this repo has recorded
    in a regex, in SQL and in an AST walk. Lines are BLANKED rather than
    removed so reported offsets still line up with the file.
    """
    out = []
    for line in sql.splitlines():
        stripped = line.lstrip()
        out.append("" if stripped.startswith("--") else line)
    return "\n".join(out)


def _live_definition() -> tuple[str, Path]:
    """The definition that actually wins: the LAST one in the corpus.

    ⚠ An applied migration cannot be edited, so a fix always arrives in a LATER
    file. A scan that read only the migration which CREATED the function would
    read 0100's racy body and fail against a correctly-fixed repo.
    """
    found: list[tuple[Path, str]] = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        body = _strip_sql_comments(path.read_text(encoding="utf-8"))
        for match in re.finditer(
            rf"create\s+or\s+replace\s+function\s+public\.{FUNCTION}\s*\(",
            body,
            re.IGNORECASE,
        ):
            end = body.find("$$;", match.start())
            assert end != -1, f"{path.name}: function body is not $$-quoted as expected"
            found.append((path, body[match.start() : end + 3]))
    assert found, f"no definition of {FUNCTION} found in the migration corpus"
    path, body = found[-1]
    return body, path


def test_the_function_takes_a_lock_before_it_checks_the_other_table():
    """The whole fix. Presence alone is not it — ORDER is.

    ⚠ A lock taken AFTER the existence check serialises nothing that matters:
    both transactions have already read their snapshots by then and both will
    still pass. The two spellings read identically in a grep, which is why this
    asserts the offset rather than the substring.
    """
    body, path = _live_definition()
    lock = body.lower().find("pg_advisory_xact_lock")
    check = body.lower().find("if exists (")
    assert lock != -1, f"{path.name}: {FUNCTION} takes no advisory lock — the race is open"
    assert check != -1, f"{path.name}: the existence check is gone; re-point this test"
    assert lock < check, (
        f"{path.name}: the lock is taken AFTER the existence check, so it "
        f"serialises nothing — both connects still read their snapshots first"
    )


def test_the_lock_is_transaction_scoped_not_session_scoped():
    """⚠ A session-scoped advisory lock outlives the transaction.

    A pooled connection handed back while still holding one would block every
    later connect for that company until the process restarted — trading a rare
    race for a permanent stall, in the direction that looks like a fix.
    """
    body, path = _live_definition()
    assert "pg_advisory_xact_lock" in body.lower(), path.name
    for banned in ("pg_advisory_lock(", "pg_advisory_lock ("):
        assert banned not in body.lower(), (
            f"{path.name}: session-scoped advisory lock — must be the _xact_ form"
        )


def test_the_lock_is_not_a_row_lock_on_companies():
    """Measured, not preference: FOR UPDATE on `companies` blocks the sync.

    Every mirror table has a foreign key to `companies`, so inserting any mirror
    row takes FOR KEY SHARE on that row, which FOR UPDATE conflicts with. Probed
    on PostgreSQL 16.13:

        FOR UPDATE on companies blocks an unrelated sync insert: True
        advisory xact lock blocks an unrelated sync insert:      False

    So the obvious spelling stalls a customer's running sync for the length of a
    connect, and a long sync stalls a connect.
    """
    body, path = _live_definition()
    assert "for update" not in body.lower(), (
        f"{path.name}: a row lock on companies blocks every concurrent mirror "
        f"insert for that company — use the advisory lock"
    )


def test_the_guard_is_not_fooled_by_the_migrations_own_explanation():
    """⚠ This file's subject IS the trap, so it is asserted rather than assumed.

    0103's header quotes ``for update`` to explain why it was rejected. If the
    comment stripper regressed, `test_the_lock_is_not_a_row_lock_on_companies`
    would fail against the correct repo — a guard that cannot tell a defect from
    its own explanation is the one that gets deleted.
    """
    path = MIGRATIONS / "0103_one_pms_race.sql"
    raw = path.read_text(encoding="utf-8")
    assert "for update" in raw.lower(), (
        "0103 no longer explains why the row lock was rejected — if that "
        "reasoning moved, re-point this test at wherever it now lives"
    )
    assert "for update" not in _strip_sql_comments(raw).lower()


def test_both_triggers_still_bind_the_function():
    """A perfect function bound to one table leaves the second connect open —
    0100's own note says guarding one side leaves the ordering nobody tests."""
    corpus = "\n".join(
        _strip_sql_comments(p.read_text(encoding="utf-8")) for p in sorted(MIGRATIONS.glob("*.sql"))
    )
    for table in ("company_rentvine_credentials", "company_buildium_credentials"):
        pattern = rf"create\s+trigger\s+\w+\s+before\s+insert[^;]*?on\s+public\.{table}"
        assert re.search(pattern, corpus, re.IGNORECASE | re.DOTALL), (
            f"no BEFORE INSERT trigger on {table} binds {FUNCTION}"
        )


def test_each_branch_checks_the_other_systems_table_and_is_not_short_circuited():
    """0100's own note: guarding one side leaves the ordering nobody tests.

    ⚠ Found by mutation, not review. ``if false and exists (…)`` on the Buildium
    branch left every other test in this file green while a company could
    connect Buildium after Rentvine — half the invariant gone, silently. The
    lock tests cannot see it because the lock is still taken; the fail-closed
    test cannot see it because the raise is still there. What separates a live
    check from a dead one is that each branch queries the OTHER system's table
    under a plain ``if exists``, so that is what is asserted here.
    """
    body, path = _live_definition()
    branches = {
        "company_rentvine_credentials": "public.company_buildium_credentials",
        "company_buildium_credentials": "public.company_rentvine_credentials",
    }
    for fired_by, must_query in branches.items():
        marker = f"tg_table_name = '{fired_by}' then"
        start = body.find(marker)
        assert start != -1, f"{path.name}: no branch for a trigger fired by {fired_by}"
        rest = body[start + len(marker) :]
        # The branch ends at the next elsif/end-if, whichever comes first.
        stops = [i for i in (rest.find("elsif"), rest.find("end if")) if i != -1]
        branch = rest[: min(stops)] if stops else rest
        assert must_query in branch, (
            f"{path.name}: the {fired_by} branch does not read {must_query} — "
            f"it is checking the wrong table, or not checking at all"
        )
        condition = branch[: branch.find("select")]
        assert re.search(r"if\s+exists\s*\(\s*$", condition.strip() + "\n", re.MULTILINE) or (
            re.sub(r"\s+", " ", condition).strip() == "if exists ("
        ), (
            f"{path.name}: the {fired_by} branch's condition is "
            f"{condition.strip()!r} — anything other than a bare `if exists (` "
            f"can short-circuit the check while leaving it looking present"
        )


def test_the_replacement_keeps_the_refusal_rather_than_softening_it():
    """Fail-closed is the other half. A fix that serialised the connects and
    then let the second one through would pass every test above."""
    body, _ = _live_definition()
    assert "raise exception" in body.lower()
    assert "unique_violation" in body.lower()
    assert "one PMS per company" in body
