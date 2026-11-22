"""Unit tests for the RLS guard's pure static core (no FS / no network).

Loaded via ``importlib.util`` (mirrors ``test_doc_drift_check.py``) so the test
works under plain ``pytest tests/`` without ``scripts`` being a package.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "_helixis_rls_guard",
        repo_root / "scripts" / "rls_guard.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_mod = _load_module()
find_unprotected_tables = _mod.find_unprotected_tables


def test_table_with_rls_in_same_text_is_protected() -> None:
    sql = [
        "create table if not exists public.widgets (id uuid);\n"
        "alter table public.widgets enable row level security;"
    ]
    assert find_unprotected_tables(sql) == []


def test_rls_may_land_in_a_later_migration() -> None:
    # create in one file, enable RLS in another — must still count as protected.
    texts = [
        "create table public.gadgets (id uuid);",
        "alter table if exists public.gadgets enable row level security;",
    ]
    assert find_unprotected_tables(texts) == []


def test_created_but_never_protected_is_flagged() -> None:
    # This is the buildium_events class: table created, RLS never enabled.
    texts = ["create table if not exists public.leaky (id uuid, company_id uuid);"]
    assert find_unprotected_tables(texts) == ["leaky"]


def test_allowlist_suppresses_a_known_service_only_table() -> None:
    texts = ["create table public.internal_only (id uuid);"]
    assert find_unprotected_tables(texts, allowlist=frozenset({"internal_only"})) == []


def test_case_insensitive_and_schema_optional() -> None:
    texts = [
        "CREATE TABLE Things (id uuid);\n"  # no schema qualifier, mixed case
        "ALTER TABLE public.things ENABLE ROW LEVEL SECURITY;"
    ]
    assert find_unprotected_tables(texts) == []


def test_live_allowlist_excludes_postgis_reference_table() -> None:
    # spatial_ref_sys (PostGIS, migration 0053) is RLS-off but holds no tenant
    # data — it must be tolerated by the LIVE check so Monday's cron doesn't
    # false-fail, WITHOUT loosening the stricter static allowlist.
    assert "spatial_ref_sys" in _mod.LIVE_ALLOWLIST
    assert "spatial_ref_sys" not in _mod.SERVICE_ONLY_ALLOWLIST
    # And the live set-difference logic would drop it (mirrors find_live_unprotected).
    live_rls_off = {"helixis_leases", "spatial_ref_sys"}
    remaining = sorted(live_rls_off - {t.lower() for t in _mod.LIVE_ALLOWLIST})
    assert remaining == ["helixis_leases"]


def test_dead_migrations_are_ignored() -> None:
    # A historical, never-applied migration must NOT count toward protection —
    # its create AND its enable-RLS are both inert. So a table created in a live
    # migration but only "protected" by the dead one is still flagged.
    texts = [
        "create table public.buildium_events (id uuid);",  # live (0007-like)
        "-- HISTORICAL MIGRATION — DO NOT RE-RUN\n"
        "alter table public.buildium_events enable row level security;",  # dead (0002-like)
    ]
    assert find_unprotected_tables(texts) == ["buildium_events"]


def test_real_migrations_are_all_protected() -> None:
    """The shipped migration set must have RLS on every table it creates.

    This is the regression that 0025 fixes: before it, ``buildium_events`` was
    created (0007) but never RLS-enabled in any live migration.
    """
    repo_root = Path(__file__).resolve().parents[2]
    texts = [
        p.read_text(encoding="utf-8")
        for p in sorted((repo_root / "supabase" / "migrations").glob("*.sql"))
    ]
    assert find_unprotected_tables(texts) == []
