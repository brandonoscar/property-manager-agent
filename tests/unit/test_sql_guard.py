"""Unit tests for the analyst SQL guard (analysis/sql_guard.py, ADR 0007).

The guard gates agent-written SQL, so these tests are adversarial: every
rejection case is an attack or smuggling shape, every acceptance case is a
realistic reporting query over the mirror.
"""

from __future__ import annotations

import pytest

from helixis.analysis.sql_guard import ALLOWED_TABLES, MAX_ROWS, guard_sql

# ── acceptance: realistic reporting queries ─────────────────────


def test_simple_aggregate_allowed() -> None:
    r = guard_sql("SELECT status, COUNT(*) FROM helixis_work_orders GROUP BY status")
    assert r.allowed and r.execution_sql is not None
    assert f"LIMIT {MAX_ROWS}" in r.execution_sql


def test_join_across_mirror_tables_allowed() -> None:
    r = guard_sql(
        """
        SELECT p.name, SUM(b.amount) AS spend
        FROM helixis_bills b
        JOIN helixis_properties p ON p.buildium_id = b.property_id
        WHERE b.bill_date >= '2026-01-01'
        GROUP BY p.name
        ORDER BY spend DESC
        """
    )
    assert r.allowed, r.reason


def test_cte_allowed_and_cte_name_not_treated_as_table() -> None:
    r = guard_sql(
        """
        WITH monthly AS (
            SELECT date_trunc('month', bill_date) AS m, SUM(amount) AS total
            FROM helixis_bills GROUP BY 1
        )
        SELECT m, total FROM monthly ORDER BY m
        """
    )
    assert r.allowed, r.reason


def test_public_schema_prefix_rejected() -> None:
    # Schema qualification would resolve to the REAL table and bypass the
    # executor's company-scoped shadow CTE — the one hole in CTE shadowing.
    r = guard_sql("SELECT COUNT(*) FROM public.helixis_tenants")
    assert not r.allowed and "schema-qualified" in r.reason


def test_cte_name_colliding_with_mirror_table_rejected() -> None:
    r = guard_sql("WITH helixis_bills AS (SELECT 1 AS x) SELECT * FROM helixis_bills")
    assert not r.allowed and "shadows a mirror table" in r.reason


def test_recursive_cte_rejected() -> None:
    r = guard_sql("WITH RECURSIVE t AS (SELECT 1) SELECT * FROM helixis_bills")
    assert not r.allowed and "recursive" in r.reason.lower()


def test_referenced_tables_exposed_in_order() -> None:
    r = guard_sql(
        "SELECT * FROM helixis_bills b JOIN helixis_vendors v "
        "ON v.buildium_id = b.vendor_id JOIN helixis_bills b2 ON b2.id = b.id"
    )
    assert r.allowed
    assert r.tables == ("helixis_bills", "helixis_vendors")  # deduped, ordered
    assert r.normalized_sql is not None and "LIMIT" not in r.normalized_sql.upper()


def test_inner_limit_still_capped_by_outer_wrap() -> None:
    r = guard_sql("SELECT * FROM helixis_units LIMIT 100000")
    assert r.allowed
    # The outer wrap caps regardless of the inner LIMIT.
    assert r.execution_sql is not None and r.execution_sql.rstrip().endswith(f"LIMIT {MAX_ROWS}")


def test_trailing_semicolon_tolerated() -> None:
    assert guard_sql("SELECT COUNT(*) FROM helixis_leases;").allowed


def test_keywords_inside_string_literals_do_not_trip() -> None:
    # A memo containing scary words is data, not SQL.
    r = guard_sql("SELECT * FROM helixis_bills WHERE memo = 'please DELETE the UPDATE; DROP fee'")
    assert r.allowed, r.reason


def test_column_names_containing_keywords_do_not_trip() -> None:
    # created_at / updated_at contain create/update as substrings only.
    r = guard_sql("SELECT created_at, updated_at FROM helixis_tasks")
    assert r.allowed, r.reason


# ── rejection: writes and DDL ───────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO helixis_bills (amount) VALUES (1)",
        "UPDATE helixis_tenants SET name = 'x'",
        "DELETE FROM helixis_bills",
        "DROP TABLE helixis_bills",
        "TRUNCATE helixis_bills",
        "CREATE TABLE evil (id int)",
        "ALTER TABLE helixis_bills ADD COLUMN x int",
        "GRANT ALL ON helixis_bills TO PUBLIC",
    ],
)
def test_writes_and_ddl_rejected(sql: str) -> None:
    assert not guard_sql(sql).allowed


def test_select_into_rejected() -> None:
    # SELECT ... INTO creates a table — a write disguised as a read.
    assert not guard_sql("SELECT * INTO evil FROM helixis_bills").allowed


def test_row_locks_rejected() -> None:
    assert not guard_sql("SELECT * FROM helixis_bills FOR UPDATE").allowed


def test_multi_statement_rejected() -> None:
    assert not guard_sql("SELECT 1 FROM helixis_bills; DROP TABLE helixis_bills").allowed


# ── rejection: smuggling vectors ────────────────────────────────


def test_comments_rejected() -> None:
    assert not guard_sql("SELECT * FROM helixis_bills -- hidden").allowed
    assert not guard_sql("SELECT /* x */ * FROM helixis_bills").allowed


def test_dollar_quoting_rejected() -> None:
    assert not guard_sql("SELECT $$drop table x$$ FROM helixis_bills").allowed


def test_unterminated_string_rejected() -> None:
    assert not guard_sql("SELECT * FROM helixis_bills WHERE memo = 'oops").allowed


# ── rejection: scope escapes ────────────────────────────────────


def test_disallowed_table_rejected() -> None:
    r = guard_sql("SELECT * FROM companies")
    assert not r.allowed and "allowlist" in r.reason


def test_credentials_table_rejected_anywhere() -> None:
    # Even smuggled into a subquery the FROM-scan would catch it, but the
    # denylist rejects the identifier anywhere at all.
    r = guard_sql(
        "SELECT * FROM helixis_bills WHERE id IN (SELECT id FROM company_buildium_credentials)"
    )
    assert not r.allowed


def test_chat_and_memory_tables_rejected() -> None:
    assert not guard_sql("SELECT * FROM helixis_chat_messages").allowed
    assert not guard_sql("SELECT * FROM memory_audit").allowed


def test_pg_catalog_and_functions_rejected() -> None:
    assert not guard_sql("SELECT * FROM pg_catalog.pg_tables").allowed
    assert not guard_sql("SELECT pg_sleep(10) FROM helixis_bills").allowed
    assert not guard_sql("SELECT * FROM information_schema.tables").allowed


def test_settings_functions_rejected() -> None:
    assert not guard_sql("SELECT current_setting('x') FROM helixis_bills").allowed


def test_pure_function_query_rejected() -> None:
    # Must actually read the mirror — no table-less probes.
    assert not guard_sql("SELECT version()").allowed


def test_non_select_first_word_rejected() -> None:
    assert not guard_sql("EXPLAIN SELECT * FROM helixis_bills").allowed
    assert not guard_sql("").allowed


# ── the allowlist itself ────────────────────────────────────────


def test_allowlist_is_mirror_only() -> None:
    # Every allowed table is a helixis_* mirror table; the sensitive tables
    # never appear. Widening this set is a reviewed change.
    assert all(t.startswith("helixis_") for t in ALLOWED_TABLES)
    for sensitive in (
        "company_buildium_credentials",
        "helixis_chat_messages",
        "memory_audit",
        "companies",
        "company_users",
        "crm_leads",
    ):
        assert sensitive not in ALLOWED_TABLES


# ── comma joins (implicit cross joins) — the cross-tenant hole ───


def test_comma_join_rejected_cross_tenant_leak() -> None:
    # THE hole: only the first table is captured + company-scoped; the
    # comma-joined second table resolves to the raw public table = every
    # tenant's rows. Must be rejected outright.
    r = guard_sql("SELECT * FROM helixis_bills, helixis_tenants")
    assert not r.allowed
    assert "comma join" in r.reason.lower()


def test_comma_join_with_aliases_rejected() -> None:
    r = guard_sql("SELECT b.amount, t.name FROM helixis_bills b, helixis_tenants t WHERE b.x = t.y")
    assert not r.allowed and "comma join" in r.reason.lower()


def test_comma_join_in_subquery_from_rejected() -> None:
    r = guard_sql("SELECT * FROM (SELECT id FROM helixis_bills) x, helixis_tenants")
    assert not r.allowed and "comma join" in r.reason.lower()


def test_three_way_comma_join_rejected() -> None:
    r = guard_sql("SELECT * FROM helixis_bills, helixis_tenants, helixis_owners")
    assert not r.allowed and "comma join" in r.reason.lower()


# ── comma joins must NOT over-block legitimate commas ────────────


def test_explicit_cross_join_allowed() -> None:
    # CROSS JOIN via the keyword captures + scopes both tables, so it's safe.
    r = guard_sql("SELECT * FROM helixis_bills CROSS JOIN helixis_tenants")
    assert r.allowed, r.reason


def test_select_list_commas_allowed() -> None:
    r = guard_sql("SELECT a, b, c FROM helixis_bills")
    assert r.allowed, r.reason


def test_function_arg_commas_allowed() -> None:
    r = guard_sql("SELECT COALESCE(a, b, 0) FROM helixis_bills")
    assert r.allowed, r.reason


def test_in_list_commas_allowed() -> None:
    r = guard_sql("SELECT * FROM helixis_bills WHERE status IN (1, 2, 3)")
    assert r.allowed, r.reason


def test_group_by_commas_allowed() -> None:
    r = guard_sql("SELECT status, kind, COUNT(*) FROM helixis_work_orders GROUP BY status, kind")
    assert r.allowed, r.reason
