#!/usr/bin/env python3
"""Migration-applied checker — did the migrations in the repo actually reach prod?

Migrations are applied MANUALLY, forward-only, with no runner (CLAUDE.md §4 /
gotcha #8), so a committed migration file is NOT proof it ran. This already bit
us: ``memory_audit`` (defined in ``0002_phase1_memory_rls.sql``) was never
applied to helixis-test — every memory op logged ``relation "memory_audit" does
not exist`` for weeks, silently dropping the SOC2 audit trail. The audit
(2026-07-12, Q35) flagged this whole class.

There is no migration ledger to query, so this verifies migrations by their
EFFECT: every ``create table public.X`` and ``alter table public.X add column
col`` in ``supabase/migrations/`` should have a matching object in the live DB.
A missing table/column means that migration (or part of it) never applied.

  * STATIC (no DB): parse each migration for the objects it creates. Prints the
    plan; useful as a sanity check and unit-tested pure core.
  * LIVE (needs ``HELIXIS_SUPABASE__DB_URL``): query ``information_schema`` for
    the tables + columns that exist, and fail on any expected object that's
    missing — naming the migration file it came from.

Only object-CREATING migrations are verifiable this way; a migration that just
enables RLS, backfills data, or defines a function has no created object to
check and is reported as "not object-verifiable" (informational, never a
failure). Dead/never-applied migrations (``DO NOT RE-RUN`` marker) are skipped.

Exit 0 = every expected object present; exit 1 = a finding. ``--json`` for
machine-readable output. Run the live check from a machine/CI that can reach
Supabase (this container can't — egress).
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATIONS_GLOB = os.path.join(ROOT, "supabase", "migrations", "*.sql")

# Historical, never-applied migrations kept for the audit trail (e.g. 0002).
# Their CREATE/ADD statements must NOT be treated as expected-live (0002 itself
# is how memory_audit shipped missing). Same marker rls_guard.py honors.
_DEAD_MARKER = "DO NOT RE-RUN"

_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

_CREATE_TABLE_RE = re.compile(
    r"create\s+table\s+(?:if\s+not\s+exists\s+)?(?:public\.)?([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
_ALTER_TABLE_RE = re.compile(
    r"alter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?(?:public\.)?([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
_ADD_COLUMN_RE = re.compile(
    r"add\s+column\s+(?:if\s+not\s+exists\s+)?([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)

#: A table name supplied at RUNTIME by ``format()``, not written in the file.
#:
#: ⚠ WITHOUT THIS THE PARSER REPORTS A TABLE NAMED ``public``, FOREVER. Given
#: ``alter table public.%I add column ... pms ...``, the optional ``(?:public\.)?``
#: group cannot be followed by ``%`` — ``[a-z_]`` does not match it — so the
#: engine backtracks, declines the group, and captures ``public`` as the table.
#: 0101 therefore expected a column ``public.pms``, which no database will ever
#: have. That is the worst of both outcomes: a canary that cannot pass (gotcha
#: 84 — people stop reading it) AND zero real coverage of the 21 tables the
#: migration does touch.
#:
#: Matching this DECLINES the statement rather than guessing the loop's array
#: (gotcha 55: a guard that cannot see must decline, never report emptiness).
#: Reading the array literal is possible and fragile — a ``DO`` block is a
#: general program, and a parser that handles one shape silently mis-reads the
#: next. What the file adds is reported instead, by name, as unverifiable.
_TEMPLATED_TABLE_RE = re.compile(
    r"(?:alter|create)\s+table\s+"
    r"(?:if\s+not\s+exists\s+|if\s+exists\s+|only\s+)*"
    r"(?:public\s*\.\s*)?%[ILs]",
    re.IGNORECASE,
)


def _strip_sql_comments(text: str) -> str:
    return _LINE_COMMENT_RE.sub("", _BLOCK_COMMENT_RE.sub("", text))


def expected_objects(sql: str) -> tuple[set[str], set[tuple[str, str]]]:
    """Parse one migration's SQL → (created tables, (table, added-column) pairs).

    Pure + statement-scoped: split on ``;`` so a multi-line ``ALTER TABLE X ADD
    COLUMN a, ADD COLUMN b`` binds every added column to X. A migration carrying
    the dead marker contributes nothing.
    """
    tables: set[str] = set()
    columns: set[tuple[str, str]] = set()
    if _DEAD_MARKER in sql.upper():
        return tables, columns
    clean = _strip_sql_comments(sql)
    for stmt in clean.split(";"):
        # A runtime-named table contributes NOTHING. Guessing produces a
        # permanent false finding; see _TEMPLATED_TABLE_RE.
        if _TEMPLATED_TABLE_RE.search(stmt):
            continue
        create = _CREATE_TABLE_RE.search(stmt)
        if create:
            tables.add(create.group(1).lower())
            continue
        alter = _ALTER_TABLE_RE.search(stmt)
        if alter:
            table = alter.group(1).lower()
            for col in _ADD_COLUMN_RE.findall(stmt):
                columns.add((table, col.lower()))
    return tables, columns


def templated_columns(sql: str) -> set[str]:
    """Columns a migration adds to tables it names at RUNTIME.

    These are real DDL this sweep structurally cannot verify, and saying so is
    the whole point: silently dropping them makes 0101 indistinguishable from
    a data-only migration, which is gotcha 53's "a schema check cannot see an
    unapplied backfill" arriving one category over — with 21 tables behind it.

    ⚠ Scoped to statements that ADD A COLUMN or CREATE A TABLE, which is what
    this sweep tracks. 0049 also templates its table name and adds only
    CONSTRAINTS — invisible to the sweep whether templated or not — so naming
    it here would put a file with no gap beside one with a real one, and a
    report that cries wolf is one people stop reading (gotchas 84/103).
    """
    out: set[str] = set()
    if _DEAD_MARKER in sql.upper():
        return out
    for stmt in _strip_sql_comments(sql).split(";"):
        if not _TEMPLATED_TABLE_RE.search(stmt):
            continue
        out.update(c.lower() for c in _ADD_COLUMN_RE.findall(stmt))
    return out


def parse_migrations(texts_by_file: dict[str, str]) -> dict[str, dict[str, set]]:
    """Map each migration file → {'tables': set, 'columns': set[(t,c)]}. Files
    that create nothing map to empty sets (reported as not object-verifiable)."""
    out: dict[str, dict[str, set]] = {}
    for name, sql in texts_by_file.items():
        tables, columns = expected_objects(sql)
        out[name] = {"tables": tables, "columns": columns}
    return out


def missing_objects(
    plan: dict[str, dict[str, set]],
    live_tables: set[str],
    live_columns: set[tuple[str, str]],
) -> dict[str, dict[str, list]]:
    """Per migration, the expected objects absent from the live DB. A missing
    TABLE suppresses its column findings (the whole table is gone, not a
    partial). Files with no expected objects, or all present, are omitted."""
    findings: dict[str, dict[str, list]] = {}
    for name, exp in plan.items():
        miss_tables = sorted(t for t in exp["tables"] if t not in live_tables)
        miss_cols = sorted(
            f"{t}.{c}"
            for (t, c) in exp["columns"]
            if t not in miss_tables and (t, c) not in live_columns
        )
        if miss_tables or miss_cols:
            findings[name] = {"tables": miss_tables, "columns": miss_cols}
    return findings


def _read_migrations() -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(glob.glob(MIGRATIONS_GLOB)):
        with open(path, encoding="utf-8") as fh:
            out[os.path.basename(path)] = fh.read()
    return out


async def fetch_live_objects(db_url: str) -> tuple[set[str], set[tuple[str, str]]]:
    """(public tables, (table, column) pairs) that currently exist."""
    import asyncpg

    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        trows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
        crows = await conn.fetch(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public'"
        )
    finally:
        await conn.close()
    tables = {r["table_name"].lower() for r in trows}
    columns = {(r["table_name"].lower(), r["column_name"].lower()) for r in crows}
    return tables, columns


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--live",
        action="store_true",
        help="check expected objects against the live DB (needs HELIXIS_SUPABASE__DB_URL)",
    )
    args = parser.parse_args()

    texts = _read_migrations()
    plan = parse_migrations(texts)
    verifiable = {n for n, e in plan.items() if e["tables"] or e["columns"]}
    templated = {n: sorted(cols) for n, sql in texts.items() if (cols := templated_columns(sql))}

    findings: dict[str, dict[str, list]] = {}
    if args.live:
        db_url = os.environ.get("HELIXIS_SUPABASE__DB_URL", "")
        if not db_url:
            print("HELIXIS_SUPABASE__DB_URL not set — cannot run the live check.")
            return 2
        live_tables, live_columns = asyncio.run(fetch_live_objects(db_url))
        findings = missing_objects(plan, live_tables, live_columns)

    if args.json:
        print(
            json.dumps(
                {
                    "migrations": len(plan),
                    "object_verifiable": sorted(verifiable),
                    "unapplied": findings,
                    "templated_ddl": templated,
                },
                indent=2,
            )
        )
    elif not args.live:
        print(
            f"{len(plan)} migrations parsed; {len(verifiable)} create objects "
            "(tables/columns) that the --live check can verify."
        )
    elif findings:
        print("MIGRATION CHECK FAIL — expected objects missing from the live DB:")
        for name, miss in findings.items():
            bits = []
            if miss["tables"]:
                bits.append(f"tables: {', '.join(miss['tables'])}")
            if miss["columns"]:
                bits.append(f"columns: {', '.join(miss['columns'])}")
            print(f"  [{name}] {'; '.join(bits)}")
        print("\nApply the missing migration piece(s) to helixis-test (§4 CLAUDE.md).")
    else:
        print(
            f"Migration check: OK — all {len(verifiable)} object-creating migrations are applied."
        )

    # ⚠ Printed on PASS as well as FAIL, and outside the --json branch on
    # purpose. This is a COVERAGE gap, not a failure: the columns may well be
    # applied and this sweep cannot say. Reporting it only on failure would
    # make an OK line read as "everything is verified", which is exactly the
    # false confidence a data-only migration already buys (gotchas 53/59).
    if templated and not args.json:
        print("\nNOT VERIFIABLE HERE — these migrations name their tables at runtime:")
        for name, cols in sorted(templated.items()):
            print(f"  [{name}] adds column(s) {', '.join(cols)} to tables chosen in a loop")
        print("  A standing query against the live DB is the only instrument for these;")
        print("  each migration's header carries its own.")

    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
