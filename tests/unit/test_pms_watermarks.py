"""Incremental watermarks: read behind, never backwards, company-scoped."""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from helixis.pms.watermarks import LAG, TABLE, Watermark, dedupe, since

REPO = Path(__file__).resolve().parents[2]
COMPANY = "00000000-0000-4000-8000-0000000000aa"


class _Pool:
    def __init__(self, value: Any = None):
        self.value = value
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.calls.append((sql, args))
        return self.value

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        return "INSERT 0 1"


async def test_no_watermark_before_the_first_clean_pass() -> None:
    assert await Watermark(_Pool(None), COMPANY, "Tenants").read() is None


async def test_the_stored_watermark_is_read_back() -> None:
    when = datetime(2026, 10, 5, 1, 0, tzinfo=UTC)
    assert await Watermark(_Pool(when), COMPANY, "Tenants").read() == when


async def test_advancing_never_moves_backwards_and_is_company_scoped() -> None:
    pool = _Pool()
    await Watermark(pool, COMPANY, "Tenants").advance(datetime(2026, 10, 5, tzinfo=UTC))
    sql, args = pool.calls[0]
    assert "GREATEST(" in sql and "ON CONFLICT (company_id, resource)" in sql
    assert args[0] == COMPANY and args[1] == "Tenants"


async def test_a_watermark_without_a_time_zone_is_refused() -> None:
    with pytest.raises(ValueError):
        await Watermark(_Pool(), COMPANY, "Tenants").advance(datetime(2026, 10, 5))


def test_a_watermark_needs_a_company_and_a_resource() -> None:
    with pytest.raises(ValueError):
        Watermark(_Pool(), "", "Tenants")
    with pytest.raises(ValueError):
        Watermark(_Pool(), COMPANY, "")


def test_each_read_starts_behind_the_watermark() -> None:
    assert since(None) is None
    wm = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    assert since(wm) == wm - LAG
    assert LAG == timedelta(minutes=10)


def test_dedupe_keeps_the_newest_version_of_each_row() -> None:
    rows = [
        {"id": 1, "modified": "2026-10-05T10:00:00Z", "v": "new"},
        {"id": 2, "modified": "2026-10-05T09:00:00Z", "v": "only"},
        {"id": 1, "modified": "2026-10-05T09:00:00Z", "v": "old, listed after"},
        {"id": 1, "modified": "2026-10-05T10:00:00Z", "v": "exact repeat"},
        {"modified": "2026-10-05T09:00:00Z", "v": "no id"},
    ]
    out = dedupe(rows, id_key="id", modified_key="modified")
    assert [r["v"] for r in out] == ["new", "only", "no id"]


def test_every_query_on_the_table_carries_the_company() -> None:
    """CLAUDE.md rule 11, by reading the SQL the module sends."""
    tree = ast.parse((REPO / "src/helixis/pms/watermarks.py").read_text(encoding="utf-8"))
    sql = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and TABLE in node.value
    ]
    sql += [
        "".join(v.value for v in node.values if isinstance(v, ast.Constant))
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
    ]
    queries = [s for s in sql if "SELECT" in s or "INSERT" in s]
    assert queries, "found no SQL to check"
    for query in queries:
        assert "company_id" in query, query


# ── the table (migration 0108) and the writer agree (rule 15) ────────────

MIGRATION = REPO / "supabase/migrations/0108_pms_sync_watermarks.sql"


def _statements() -> str:
    """The migration without its comments, lower-cased, whitespace folded."""
    lines = [ln.split("--", 1)[0] for ln in MIGRATION.read_text(encoding="utf-8").splitlines()]
    return re.sub(r"\s+", " ", " ".join(lines)).lower()


def _upsert_sql() -> str:
    tree = ast.parse((REPO / "src/helixis/pms/watermarks.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = "".join(v.value for v in node.values if isinstance(v, ast.Constant))
            if "ON CONFLICT" in text:
                return re.sub(r"\s+", " ", text)
    raise AssertionError("the writer's upsert is gone; re-point this test")


def test_the_conflict_target_is_the_tables_primary_key() -> None:
    """``ON CONFLICT (company_id, resource)`` needs exactly that key, or every
    advance after the first fails with "no unique constraint" in production."""
    target = re.search(r"ON CONFLICT \(([^)]*)\)", _upsert_sql())
    assert target is not None
    assert f"primary key ({target.group(1).lower()})" in _statements()


def test_the_table_is_closed_to_every_client_and_goes_with_its_company() -> None:
    sql = _statements()
    assert f"create table if not exists public.{TABLE}" in sql
    assert f"alter table public.{TABLE} enable row level security" in sql
    assert "create policy" not in sql, "a policy would open the table to company members"
    assert "references public.companies(id) on delete cascade" in sql


def test_the_migration_is_safe_to_run_again_and_drops_nothing() -> None:
    """Applied by hand through the Supabase MCP, which holds DROP and DELETE
    for confirmation and times out with nothing run (gotcha 202)."""
    sql = _statements()
    starts = [stmt.strip().split(" ", 1)[0] for stmt in sql.split(";") if stmt.strip()]
    assert not {"drop", "delete", "truncate"} & set(starts), starts
    assert starts[0] == "begin" and starts[-1] == "commit"
