"""Unit tests for the company-erasure script's pure core (no DB).

Loaded via ``importlib.util`` (mirrors ``test_rls_guard.py``). The live legs
(discover / cascade-delete / verify against Postgres) are owner-run; these lock
the SQL builders, identifier safety, residual analysis, and the erasure-proof
shape so a regression is caught offline.
"""

from __future__ import annotations

import ast
import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest


def _load_module():
    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "_helixis_purge_company_data", repo_root / "scripts" / "purge_company_data.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_mod = _load_module()


# ── identifier safety ────────────────────────────────────────────────


def test_safe_ident_accepts_plain_table_names() -> None:
    assert _mod._safe_ident("helixis_leases") == "helixis_leases"
    assert _mod._safe_ident("buildium_events") == "buildium_events"


@pytest.mark.parametrize("bad", ["helixis_leases; drop table x", 'a"b', "SELECT", "a-b", "a b", ""])
def test_safe_ident_rejects_anything_not_a_bare_identifier(bad: str) -> None:
    with pytest.raises(ValueError, match="unsafe identifier"):
        _mod._safe_ident(bad)


# ── SQL builders ─────────────────────────────────────────────────────


def test_count_sql_is_parameterized_and_scoped() -> None:
    sql = _mod.count_sql("helixis_work_orders")
    assert sql == "SELECT count(*) FROM public.helixis_work_orders WHERE company_id = $1"


def test_delete_sql_is_parameterized_and_scoped() -> None:
    sql = _mod.delete_sql("crm_leads")
    assert sql == "DELETE FROM public.crm_leads WHERE company_id = $1"


def test_delete_root_targets_companies_by_id() -> None:
    # The cascade root keys on `id`, not company_id.
    assert _mod.delete_root_sql() == "DELETE FROM public.companies WHERE id = $1"


def test_discover_sql_finds_company_id_tables_only() -> None:
    assert "column_name = 'company_id'" in _mod.DISCOVER_SQL
    assert "table_schema = 'public'" in _mod.DISCOVER_SQL


# ── residual analysis (which tables the cascade missed) ──────────────


def test_residual_tables_lists_only_nonzero_sorted() -> None:
    counts = {"helixis_leases": 0, "buildium_events": 3, "crm_leads": 0, "activity": 1}
    assert _mod.residual_tables(counts) == ["activity", "buildium_events"]


def test_residual_tables_empty_when_all_zero() -> None:
    assert _mod.residual_tables({"a": 0, "b": 0}) == []


# ── erasure proof record ─────────────────────────────────────────────


def test_erasure_record_shape_and_totals() -> None:
    rec = _mod.erasure_record(
        "co-123",
        requested_by="admin-uuid",
        counts_before={"helixis_leases": 5, "buildium_events": 2, "crm_leads": 0},
        memory_erased=4,
        verified=True,
        at=datetime(2026, 7, 12, 15, 0, 0, tzinfo=UTC),
    )
    assert rec["event"] == "company_data_erased"
    assert rec["company_id"] == "co-123"
    assert rec["requested_by"] == "admin-uuid"
    assert rec["erased_at"] == "2026-07-12T15:00:00+00:00"
    assert rec["relational_rows_erased"] == 7  # 5 + 2 + 0
    # Only non-zero tables are itemized in the proof.
    assert rec["per_table_rows_erased"] == {"buildium_events": 2, "helixis_leases": 5}
    assert rec["memory_rows_erased"] == 4
    assert rec["verified_zero_remaining"] is True


def test_erasure_record_defaults_operator_when_no_requester() -> None:
    rec = _mod.erasure_record(
        "co-1",
        requested_by="",
        counts_before={},
        memory_erased=0,
        verified=False,
        at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert rec["requested_by"] == "operator"
    assert rec["relational_rows_erased"] == 0
    assert rec["verified_zero_remaining"] is False


# ── the storage leg, and what the certificate may claim ─────────────────────


def test_the_record_carries_the_storage_leg_separately() -> None:
    """⚠ `verified_zero_remaining` has always meant the RELATIONAL leg and
    keeps meaning exactly that. Widening it to include Storage would silently
    change what every record archived before today asserted, so the storage
    verdict is its own field — an audit fact a reader has to decode is one they
    decode wrongly (gotcha 94)."""
    rec = _mod.erasure_record(
        "c1",
        requested_by="op",
        counts_before={"helixis_leases": 3},
        memory_erased=2,
        verified=True,
        at=datetime(2026, 9, 11, tzinfo=UTC),
        storage_objects_erased=7,
        storage_verified=True,
    )
    assert rec["verified_zero_remaining"] is True
    assert rec["storage_objects_erased"] == 7
    assert rec["storage_verified"] is True


def test_a_record_that_was_not_told_about_storage_under_claims() -> None:
    """The default is FALSE, not True. A caller that forgets the argument must
    produce a record that under-claims rather than one certifying a sweep
    nobody ran — the same direction as every other decline-to-assert here
    (gotchas 55/57/89)."""
    rec = _mod.erasure_record(
        "c1",
        requested_by="op",
        counts_before={},
        memory_erased=0,
        verified=True,
        at=datetime(2026, 9, 11, tzinfo=UTC),
    )
    assert rec["storage_verified"] is False
    assert rec["storage_objects_erased"] == 0


def test_an_unverified_storage_leg_blocks_the_certificate() -> None:
    """Until this existed the script printed "✓ Verified" with three buckets of
    the customer's PDFs untouched, and the only place that said so was its own
    docstring. The relational leg passing is no longer sufficient.

    Source-level because `run()` needs a live DSN: what is being pinned is that
    the success path is GATED, and a behavioural test would need the database
    this container cannot reach.
    """
    src = Path(_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "run")

    # The sweep must run…
    assert any(isinstance(n, ast.Name) and n.id == "_purge_storage" for n in ast.walk(fn)), (
        "the erasure script stopped sweeping Storage"
    )

    # …and its verdict must be able to return non-zero.
    guarded = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        test_src = ast.unparse(node.test)
        if "storage" in test_src and "complete" in test_src:
            if any(isinstance(s, ast.Return) for s in ast.walk(node)):
                guarded = True
    assert guarded, (
        "nothing in run() branches on storage.complete — the script can print "
        "a verified erasure with the files still in the buckets"
    )
