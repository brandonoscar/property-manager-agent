"""The Data Analyst sees every column a Rent Manager pass fills, and none of
the ones it never fills.

Both sides (rule 15): the columns come from the REAL mirror writer's INSERTs
over the synthetic fixture, and the verdict from ``sql_guard.hidden_columns``,
the one set the analyst's describer and executor both read. A hidden column
the pass fills is a fact the analyst cannot use; a visible ledger column the
pass never fills is a column that is NULL on every row it can see.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from helixis.analysis import sql_guard
from helixis.analysis.sql_guard import hidden_columns, visible_columns
from helixis.pms.mirror import write_mirror
from helixis.rentmanager import sync
from helixis.rentmanager.resources import RESOURCES
from helixis.rentmanager.sync import Read, build_mirror_rows
from tests.unit._rentmanager_fakes import COMPANY, TODAY, FakePool, portfolio

REPO = Path(__file__).resolve().parents[2]
_INSERT = re.compile(r"^INSERT INTO (helixis_\w+) \(([^)]*)\)")


async def _filled() -> dict[str, set[str]]:
    """``{table: columns}`` the writer's INSERTs name for one full pass."""
    data = portfolio()
    reads = {name: Read(rows=data[name], complete=True) for name in RESOURCES}
    pool = FakePool()
    await write_mirror(pool, COMPANY, "rentmanager", build_mirror_rows(reads, today=TODAY))
    out: dict[str, set[str]] = {}
    for sql, _args in pool.log:
        found = _INSERT.match(sql)
        if found:
            out.setdefault(found.group(1), set()).update(
                c.strip() for c in found.group(2).split(",")
            )
    return out


def _ledger_tables() -> set[str]:
    """The tables migration 0104 gives ``ledger_id``/``ledger_type``."""
    sql = (REPO / "supabase/migrations/0104_rentvine_ledger_columns.sql").read_text()
    tables = set(re.findall(r"alter table public\.(helixis_\w+)\s+add column", sql))
    assert tables, "the migration scan found nothing; re-point it, never delete it"
    return tables


async def test_every_column_the_pass_fills_is_visible_but_the_discriminator() -> None:
    filled = await _filled()
    assert set(filled) == set(sync.SYNC_STATE_STAGE), "every filled table was written"
    for table, columns in filled.items():
        ordered = sorted(columns)
        assert visible_columns(ordered, "rentmanager") == tuple(c for c in ordered if c != "pms"), (
            table
        )
    assert "pms" in hidden_columns("rentmanager")


async def test_the_ledger_columns_it_never_fills_stay_hidden() -> None:
    filled = await _filled()
    ledger_tables = _ledger_tables()
    assert not ledger_tables & set(filled), "a Rent Manager pass now fills a ledger table"
    assert not ledger_tables & set(sync.SYNC_STATE_STAGE)
    assert all({"ledger_id", "ledger_type"}.isdisjoint(cols) for cols in filled.values())
    assert {"ledger_id", "ledger_type"} <= hidden_columns("rentmanager")


def test_rent_manager_has_its_own_row_not_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback is for a company whose PMS could not be read, which Rent
    Manager is not. With the fallback changed, the answer must not move."""
    monkeypatch.setattr(sql_guard, "_DEFAULT_PMS", "rentvine")
    assert hidden_columns("rentmanager") == {"pms", "ledger_id", "ledger_type"}
    assert hidden_columns("Rentmanager ") == hidden_columns("rentmanager")
