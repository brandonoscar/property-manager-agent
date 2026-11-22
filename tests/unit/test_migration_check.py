"""Unit tests for the migration-applied checker's pure core (no DB, no network).

Loaded via ``importlib.util`` (mirrors ``test_rls_guard.py``). The live
information_schema check is owner/cron-run; these lock the SQL parsing and the
missing-object diff so a regression is caught offline.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "_helixis_migration_check", repo_root / "scripts" / "migration_check.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_mod = _load_module()
expected_objects = _mod.expected_objects
missing_objects = _mod.missing_objects
parse_migrations = _mod.parse_migrations


# ── parsing: created tables ──────────────────────────────────────────


def test_parses_create_table_schema_and_ine_optional() -> None:
    tables, cols = expected_objects(
        "create table if not exists public.helixis_leases (id uuid);\n"
        "CREATE TABLE Buildium_Events (id uuid);"
    )
    assert tables == {"helixis_leases", "buildium_events"}
    assert cols == set()


def test_parses_multiline_alter_add_column_binds_to_table() -> None:
    sql = (
        "alter table public.helixis_lease_balances\n"
        "    add column if not exists balance_0_30 numeric(12,2),\n"
        "    add column if not exists balance_31_60 numeric(12,2);"
    )
    tables, cols = expected_objects(sql)
    assert tables == set()
    assert cols == {
        ("helixis_lease_balances", "balance_0_30"),
        ("helixis_lease_balances", "balance_31_60"),
    }


def test_alter_enable_rls_has_no_column_objects() -> None:
    # An RLS-enable ALTER creates nothing to verify by object existence.
    tables, cols = expected_objects("alter table public.companies enable row level security;")
    assert tables == set() and cols == set()


def test_dead_migration_contributes_nothing() -> None:
    sql = (
        "-- HISTORICAL MIGRATION — DO NOT RE-RUN\n"
        "create table public.memory_audit (id uuid);\n"
        "alter table public.x add column if not exists y text;"
    )
    assert expected_objects(sql) == (set(), set())


def test_comments_do_not_produce_phantom_objects() -> None:
    sql = "-- create table public.ghost (id uuid);\n/* alter table x add column z text; */"
    assert expected_objects(sql) == (set(), set())


# ── missing-object diff ──────────────────────────────────────────────


def test_missing_reports_absent_table() -> None:
    plan = parse_migrations({"0002.sql": "create table public.memory_audit (id uuid);"})
    findings = missing_objects(plan, live_tables=set(), live_columns=set())
    assert findings == {"0002.sql": {"tables": ["memory_audit"], "columns": []}}


def test_missing_table_suppresses_its_column_findings() -> None:
    # A whole missing table shouldn't also spam a finding per absent column.
    plan = parse_migrations({"m.sql": "alter table public.gone add column if not exists a text;"})
    findings = missing_objects(plan, live_tables=set(), live_columns=set())
    # 'gone' has no CREATE here, so it's only a column expectation on a table
    # that doesn't exist live → reported as a missing column, not a table.
    assert findings == {"m.sql": {"tables": [], "columns": ["gone.a"]}}


def test_present_objects_produce_no_findings() -> None:
    plan = parse_migrations(
        {
            "m.sql": (
                "create table public.helixis_leases (id uuid);\n"
                "alter table public.helixis_leases add column if not exists geog text;"
            )
        }
    )
    findings = missing_objects(
        plan,
        live_tables={"helixis_leases"},
        live_columns={("helixis_leases", "geog")},
    )
    assert findings == {}


def test_partial_migration_missing_only_the_absent_column() -> None:
    # gotcha #8: "when only part of a multi-part migration is missing".
    plan = parse_migrations(
        {
            "m.sql": (
                "alter table public.t add column if not exists applied text;\n"
                "alter table public.t add column if not exists forgotten text;"
            )
        }
    )
    findings = missing_objects(
        plan,
        live_tables={"t"},
        live_columns={("t", "applied")},  # 'forgotten' never applied
    )
    assert findings == {"m.sql": {"tables": [], "columns": ["t.forgotten"]}}


# ── against the real migration tree ──────────────────────────────────


def test_real_migrations_parse_and_include_known_objects() -> None:
    plan = parse_migrations(_mod._read_migrations())
    assert len(plan) > 40  # 0001 … 005x
    all_tables: set[str] = set()
    for e in plan.values():
        all_tables |= e["tables"]
    # Spot-check tables we know the repo creates.
    assert "companies" in all_tables
    assert "buildium_events" in all_tables
    assert "helixis_activity_log" in all_tables


# --------------------------------------------------------------------------
# A table named at RUNTIME
# --------------------------------------------------------------------------
#
# 0101 adds one column to 21 tables through `execute format('alter table
# public.%I add column ... pms ...', t)`. The parser used to report a missing
# column `public.pms` — the optional `(?:public\.)?` group cannot be followed
# by `%`, so the engine backtracks and captures the SCHEMA as the table. Two
# failures in one: a canary that can never pass, and zero coverage of the 21
# tables it was supposed to be watching.

templated_columns = _mod.templated_columns

_TEMPLATED_0101 = (
    "do $$ declare t text; begin "
    "foreach t in array array['helixis_units', 'helixis_leases'] loop "
    "execute format('alter table public.%I add column if not exists pms text "
    "not null default ''buildium''', t); end loop; end $$;"
)


def test_a_runtime_named_table_is_not_claimed_as_a_table_called_public():
    """The false positive itself. `public` is a schema; no DB has a table
    by that name, so the finding could never clear."""
    tables, cols = expected_objects(_TEMPLATED_0101)
    assert tables == set()
    assert cols == set(), f"a templated ALTER must claim nothing, got {cols}"


def test_the_templated_column_is_reported_rather_than_silently_dropped():
    """⚠ Dropping it quietly is the SAME defect one category over.

    A file that contributes nothing reads exactly like a data-only migration,
    and the sweep prints OK — which is gotcha 53's "a schema check cannot see
    an unapplied backfill" with 21 tables behind it. The gap has to be named.
    """
    assert templated_columns(_TEMPLATED_0101) == {"pms"}


def test_a_templated_statement_that_adds_no_column_is_not_reported():
    """⚠ Precision, or the report cries wolf and gets switched off.

    0049 also templates its table name and adds only CONSTRAINTS — which this
    sweep does not track for ANY migration, templated or not. Listing it
    beside a file with a real gap is what makes a report unreadable
    (gotchas 84/103).
    """
    constraints_only = (
        "do $$ begin execute format('alter table public.%I add constraint %I "
        "unique (company_id, buildium_id)', t, t || '_k'); end $$;"
    )
    assert templated_columns(constraints_only) == set()
    assert expected_objects(constraints_only) == (set(), set())


def test_an_ordinary_alter_is_still_parsed_normally():
    """The other direction (gotchas 38/51).

    A detector that declined every ALTER would pass the tests above perfectly
    while blinding the sweep to all 68 verifiable migrations.
    """
    tables, cols = expected_objects(
        "alter table public.helixis_leases add column if not exists pms text;"
    )
    assert tables == set()
    assert cols == {("helixis_leases", "pms")}


def test_no_migration_in_the_corpus_claims_a_schema_as_a_table():
    """⚠ READS THE CORPUS, because the failure mode is 'silently permits more'
    (gotcha 45) — a hand-written input would pass forever while the next
    templated migration quietly reopened the hole.

    `public`/`only`/`if` are the three words the optional groups in the
    ALTER/CREATE patterns can strand as a capture, so a finding naming any of
    them is this bug rather than a real missing object.
    """
    import glob
    import pathlib

    paths = sorted(glob.glob(str(pathlib.Path("supabase/migrations") / "*.sql")))
    assert len(paths) > 50, "the migration corpus did not load — a green here would be empty"

    strays = {"public", "only", "if"}
    offenders: list[str] = []
    for path in paths:
        tables, cols = expected_objects(pathlib.Path(path).read_text(encoding="utf-8"))
        bad = sorted(tables & strays) + sorted(f"{t}.{c}" for (t, c) in cols if t in strays)
        if bad:
            offenders.append(f"{pathlib.Path(path).name}: {bad}")
    assert not offenders, "a schema/keyword captured as a table name: " + "; ".join(offenders)


def test_the_corpus_still_yields_plenty_of_real_objects():
    """The canary. If the templated-statement skip ever matched too broadly,
    the check above would go green on an empty set and report nothing."""
    import glob
    import pathlib

    total = 0
    for path in sorted(glob.glob(str(pathlib.Path("supabase/migrations") / "*.sql"))):
        tables, cols = expected_objects(pathlib.Path(path).read_text(encoding="utf-8"))
        total += len(tables) + len(cols)
    assert total > 100, f"only {total} objects parsed — the parser has gone blind"
