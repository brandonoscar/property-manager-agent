"""The spine writer: a connector's rows into the shared mirror.

Stale is never empty, prune only from a complete read, a value not read is
left as it was, and every statement names the company and the system. The
fake pool records each statement so these read the SQL that would run.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any

import pytest

from helixis.pms import mirror
from helixis.pms.mirror import (
    KEYED_COLUMNS,
    PRUNE_TOUCH_MARGIN_S,
    SyncResult,
    TableRead,
    stamp_freshness,
    write_mirror,
)
from tests.unit._pg_bind_contract import reject_unbindable

COMPANY = "00000000-0000-4000-8000-0000000000aa"
PMS = "rentmanager"


class _Conn:
    def __init__(self, log: list[tuple[str, tuple[Any, ...]]], fail_on: str | None):
        self.log = log
        self.fail_on = fail_on

    async def execute(self, sql: str, *args: Any) -> str:
        reject_unbindable(sql, args)
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("boom")
        self.log.append((" ".join(sql.split()), args))
        return "DELETE 2" if sql.lstrip().upper().startswith("DELETE") else "INSERT 0 1"

    @contextlib.asynccontextmanager
    async def transaction(self) -> Any:
        yield


class _Pool:
    def __init__(self, fail_on: str | None = None):
        self.log: list[tuple[str, tuple[Any, ...]]] = []
        self.fail_on = fail_on

    @contextlib.asynccontextmanager
    async def acquire(self) -> Any:
        yield _Conn(self.log, self.fail_on)

    def sql(self, starts: str) -> list[tuple[str, tuple[Any, ...]]]:
        return [(s, a) for s, a in self.log if s.upper().startswith(starts)]


def _props(*ids: int) -> list[dict[str, Any]]:
    return [{"buildium_id": i, "name": f"P{i}", "raw": {"id": i}} for i in ids]


# ── what may be written ───────────────────────────────────────────────


@pytest.mark.parametrize("pms", ["buildium", "appfolio", ""])
async def test_only_a_mirror_only_system_may_write(pms: str) -> None:
    with pytest.raises(ValueError):
        await write_mirror(_Pool(), COMPANY, pms, {})


async def test_an_unknown_table_is_refused() -> None:
    with pytest.raises(ValueError):
        await write_mirror(_Pool(), COMPANY, PMS, {"helixis_activity_log": TableRead()})


async def test_a_mirror_write_needs_a_company() -> None:
    with pytest.raises(ValueError):
        await write_mirror(_Pool(), "", PMS, {})


def _insert_columns(function: Any, table: str) -> tuple[str, ...]:
    """The columns of the ``INSERT INTO <table>`` a function's source holds,
    read from its string constants (AST, never comments), less the four every
    mirror row carries."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if f"INSERT INTO {table}" in node.value:
                listed = node.value.split("(", 1)[1].split(")", 1)[0]
                columns = tuple(c.strip() for c in listed.split(","))
                return tuple(c for c in columns if c not in _EVERY_ROW)
    raise AssertionError(f"no INSERT INTO {table} in {function.__name__}")


_EVERY_ROW = ("buildium_id", "account_id", "company_id", "raw")


def test_the_keyed_columns_are_rentvines_which_are_buildiums() -> None:
    """Both sides read (rule 15): the same column sets Rentvine's sync writes,
    which its own drift guard holds to the Buildium INSERTs.

    Re-pointed 2026-10-07 for ``helixis_tasks``, which DoorLoop writes and
    Rentvine does not: its columns are read from Buildium's own task INSERT
    instead. Every other table is still held to Rentvine's."""
    from helixis.db.repositories import entities
    from helixis.rentvine.sync import TABLE_COLUMNS

    assert set(KEYED_COLUMNS) - set(TABLE_COLUMNS) == {"helixis_tasks"}
    for table, columns in KEYED_COLUMNS.items():
        if table != "helixis_tasks":
            assert columns == TABLE_COLUMNS[table], table
    assert KEYED_COLUMNS["helixis_tasks"] == _insert_columns(entities.upsert_task, "helixis_tasks")
    assert set(mirror.BALANCE_COLUMNS) <= set(TABLE_COLUMNS["helixis_lease_balances"])


# ── keyed upserts ─────────────────────────────────────────────────────


async def test_rows_land_with_the_system_and_a_zero_account() -> None:
    pool = _Pool()
    result = await write_mirror(
        pool, COMPANY, PMS, {"helixis_properties": TableRead(rows=_props(1, 2), complete=True)}
    )
    inserts = pool.sql("INSERT INTO HELIXIS_PROPERTIES")
    assert len(inserts) == 2
    sql, args = inserts[0]
    assert "ON CONFLICT (company_id, buildium_id)" in sql
    assert args[:4] == (1, 0, COMPANY, PMS)
    assert result.rows_by_table["helixis_properties"] == 2


async def test_a_column_the_row_does_not_carry_is_left_as_it_was() -> None:
    pool = _Pool()
    row = {"buildium_id": 5, "status": "Active", "rent": None}
    await write_mirror(pool, COMPANY, PMS, {"helixis_leases": TableRead(rows=[row])})
    sql, _args = pool.sql("INSERT INTO HELIXIS_LEASES")[0]
    assert "rent = EXCLUDED.rent" in sql, "an explicit unknown must be written as NULL"
    assert "lease_to_date" not in sql, "a column not read this pass was overwritten"


async def test_rows_without_an_id_are_skipped_and_counted() -> None:
    pool = _Pool()
    rows = [*_props(1), {"name": "no id"}, {"buildium_id": "abc"}]
    result = await write_mirror(pool, COMPANY, PMS, {"helixis_properties": TableRead(rows=rows)})
    assert result.skipped_no_id == {"helixis_properties": 2}
    assert len(pool.sql("INSERT")) == 1


# ── pruning ───────────────────────────────────────────────────────────


async def test_a_complete_read_prunes_what_the_vendor_no_longer_has() -> None:
    pool = _Pool()
    read = TableRead(rows=_props(1, 2), complete=True, read_seconds=12.5)
    result = await write_mirror(pool, COMPANY, PMS, {"helixis_properties": read})
    deletes = pool.sql("DELETE")
    prop_delete = next(d for d in deletes if "FROM helixis_properties" in d[0])
    sql, args = prop_delete
    assert "pms = $2" in sql and "updated_at < now()" in sql
    assert args == (COMPANY, PMS, [1, 2], 12.5 + PRUNE_TOUCH_MARGIN_S)
    # The units of a property that is gone go with it.
    assert any("FROM helixis_units" in d[0] and "property_id" in d[0] for d in deletes)
    assert result.pruned["helixis_properties"] == 2
    assert "helixis_properties" in result.confirmed_complete


async def test_an_incomplete_read_is_written_stale_and_never_pruned() -> None:
    pool = _Pool()
    result = await write_mirror(
        pool, COMPANY, PMS, {"helixis_properties": TableRead(rows=_props(1), complete=False)}
    )
    assert len(pool.sql("INSERT")) == 1, "rows that arrived are real and must be written"
    assert pool.sql("DELETE") == []
    assert "helixis_properties" in result.failed
    assert "helixis_properties" not in result.confirmed_complete


async def test_a_skipped_row_blocks_the_prune() -> None:
    pool = _Pool()
    rows = [*_props(1), {"name": "no id"}]
    await write_mirror(
        pool, COMPANY, PMS, {"helixis_properties": TableRead(rows=rows, complete=True)}
    )
    assert pool.sql("DELETE") == [], "a record without an id exists and would be pruned"


async def test_an_id_seen_twice_blocks_the_prune() -> None:
    pool = _Pool()
    await write_mirror(
        pool, COMPANY, PMS, {"helixis_properties": TableRead(rows=_props(1, 1), complete=True)}
    )
    assert pool.sql("DELETE") == [], "a page boundary moved; an unseen row would be pruned"


async def test_a_failed_read_writes_nothing_and_is_stale() -> None:
    pool = _Pool()
    result = await write_mirror(
        pool, COMPANY, PMS, {"helixis_tenants": TableRead(rows=_props(1), error="403")}
    )
    assert pool.log == []
    assert result.failed == {"helixis_tenants": "403"}


async def test_a_write_that_fails_marks_only_its_own_table_stale() -> None:
    pool = _Pool(fail_on="INSERT INTO helixis_tenants")
    result = await write_mirror(
        pool,
        COMPANY,
        PMS,
        {
            "helixis_tenants": TableRead(rows=_props(1), complete=True),
            "helixis_vendors": TableRead(rows=_props(1), complete=True),
        },
    )
    assert "helixis_tenants" in result.failed
    assert "helixis_vendors" not in result.failed


# ── the balance snapshot ──────────────────────────────────────────────


def _balance(lease: Any) -> dict[str, Any]:
    return {
        "lease_id": lease,
        "property_id": 1,
        "unit_id": 2,
        "total_balance": 100,
        "balance_0_30": 100,
        "balance_31_60": 0,
        "balance_61_90": 0,
        "balance_over_90": 0,
    }


async def test_an_incomplete_balance_read_leaves_the_old_snapshot() -> None:
    pool = _Pool()
    result = await write_mirror(
        pool,
        COMPANY,
        PMS,
        {"helixis_lease_balances": TableRead(rows=[_balance(1)], complete=False)},
    )
    assert pool.log == [], "a partial snapshot reads every missing lease as current"
    assert "helixis_lease_balances" in result.failed


async def test_a_complete_balance_read_replaces_the_snapshot() -> None:
    pool = _Pool()
    result = await write_mirror(
        pool,
        COMPANY,
        PMS,
        {"helixis_lease_balances": TableRead(rows=[_balance(1), _balance(2)], complete=True)},
    )
    (delete,) = pool.sql("DELETE")
    assert delete[1] == (COMPANY, PMS, [1, 2])
    assert len(pool.sql("INSERT INTO HELIXIS_LEASE_BALANCES")) == 2
    assert result.rows_by_table["helixis_lease_balances"] == 2


async def test_a_balance_without_a_lease_blocks_the_replace() -> None:
    pool = _Pool()
    result = await write_mirror(
        pool,
        COMPANY,
        PMS,
        {"helixis_lease_balances": TableRead(rows=[_balance(None)], complete=True)},
    )
    assert pool.log == [] and "helixis_lease_balances" in result.failed


# ── per-parent replace and links ──────────────────────────────────────


async def test_a_covered_parent_is_replaced_and_an_uncovered_one_kept() -> None:
    pool = _Pool()
    rows = [
        {"buildium_id": 10, "lease_id": 1, "total_amount": 5},
        {"buildium_id": 11, "lease_id": 3, "total_amount": 5},
    ]
    result = await write_mirror(
        pool,
        COMPANY,
        PMS,
        {
            "helixis_lease_transactions": TableRead(
                rows=rows, complete=True, parents=frozenset({1, 2})
            )
        },
    )
    deleted_parents = sorted(a[2] for _s, a in pool.sql("DELETE"))
    assert deleted_parents == [1, 2], "lease 2 was read with no rows: its old rows must go"
    assert len(pool.sql("INSERT")) == 1
    assert result.skipped_no_id["helixis_lease_transactions"] == 1
    assert "helixis_lease_transactions" in result.failed


async def test_bill_lines_are_replaced_per_bill() -> None:
    pool = _Pool()
    rows = [{"bill_id": 7, "line_index": 0, "amount": 1}, {"bill_id": 7, "line_index": 1}]
    result = await write_mirror(
        pool,
        COMPANY,
        PMS,
        {"helixis_bill_lines": TableRead(rows=rows, complete=True, parents=frozenset({7}))},
    )
    assert len(pool.sql("INSERT INTO HELIXIS_BILL_LINES")) == 2
    assert "helixis_bill_lines" in result.confirmed_complete


async def test_links_never_blank_a_move_in_date_and_prune_only_when_complete() -> None:
    pool = _Pool()
    link = {"lease_buildium_id": 1, "tenant_buildium_id": 2, "status": "Active"}
    await write_mirror(pool, COMPANY, PMS, {"helixis_lease_tenants": TableRead(rows=[link])})
    (insert,) = pool.sql("INSERT")
    assert "COALESCE(EXCLUDED.move_in_date" in insert[0]
    assert pool.sql("DELETE") == []

    pool = _Pool()
    await write_mirror(
        pool, COMPANY, PMS, {"helixis_lease_tenants": TableRead(rows=[link], complete=True)}
    )
    (delete,) = pool.sql("DELETE")
    assert delete[1][:4] == (COMPANY, PMS, [1], [2])


# ── incremental passes: a delta, and the id list that prunes it ───────


async def test_a_delta_is_written_and_stamped_but_prunes_nothing() -> None:
    """A delta holds only what changed since the watermark: a row that did
    not change is not in it, so pruning from it would delete the account."""
    pool = _Pool()
    read = TableRead(rows=_props(3), complete=True, delta=True, read_seconds=4.0)
    result = await write_mirror(pool, COMPANY, PMS, {"helixis_properties": read})
    assert len(pool.sql("INSERT")) == 1
    assert pool.sql("DELETE") == []
    assert "helixis_properties" in result.confirmed_complete
    assert "helixis_properties" not in result.failed


async def test_an_incomplete_delta_is_stale_and_prunes_nothing() -> None:
    pool = _Pool()
    read = TableRead(rows=_props(3), complete=False, delta=True, keep_ids=frozenset({1, 3}))
    result = await write_mirror(pool, COMPANY, PMS, {"helixis_properties": read})
    assert len(pool.sql("INSERT")) == 1
    assert pool.sql("DELETE") == []
    assert "helixis_properties" in result.failed


async def test_a_delta_with_the_full_id_list_prunes_what_is_outside_both() -> None:
    """The reconcile pass: a complete id-only read says what the vendor still
    has. A row the delta just wrote is kept even if the id list missed it."""
    pool = _Pool()
    read = TableRead(
        rows=_props(3, 4),
        complete=True,
        delta=True,
        keep_ids=frozenset({1, 2, 3}),
        read_seconds=9.0,
    )
    result = await write_mirror(pool, COMPANY, PMS, {"helixis_properties": read})
    prop_delete = next(d for d in pool.sql("DELETE") if "FROM helixis_properties" in d[0])
    assert prop_delete[1] == (COMPANY, PMS, [1, 2, 3, 4], 9.0 + PRUNE_TOUCH_MARGIN_S)
    assert any("FROM helixis_units" in d[0] for d in pool.sql("DELETE")), "children go too"
    assert result.pruned["helixis_properties"] == 2


async def test_a_delta_row_without_an_id_blocks_the_prune() -> None:
    pool = _Pool()
    rows = [*_props(3), {"name": "no id"}]
    read = TableRead(rows=rows, complete=True, delta=True, keep_ids=frozenset({1, 3}))
    await write_mirror(pool, COMPANY, PMS, {"helixis_properties": read})
    assert pool.sql("DELETE") == []


@pytest.mark.parametrize(
    "table,read",
    [
        ("helixis_properties", TableRead(rows=_props(1), complete=True, keep_ids=frozenset({1}))),
        (
            "helixis_lease_tenants",
            TableRead(complete=True, delta=True, keep_ids=frozenset({1})),
        ),
        (
            "helixis_bill_lines",
            TableRead(complete=True, delta=True, keep_ids=frozenset({1}), parents=frozenset({1})),
        ),
        ("helixis_lease_balances", TableRead(complete=True, delta=True)),
    ],
    ids=["id list on a full read", "id list on links", "id list per parent", "delta balances"],
)
async def test_a_mode_that_cannot_work_is_refused_before_anything_is_written(
    table: str, read: TableRead
) -> None:
    pool = _Pool()
    rows = {"helixis_properties": TableRead(rows=_props(9), complete=True), table: read}
    with pytest.raises(ValueError):
        await write_mirror(pool, COMPANY, PMS, rows)
    assert pool.log == [], "nothing may be written when one table's request is a bug"


async def test_a_delta_rewrites_only_the_links_of_the_leases_it_read() -> None:
    """Lease 2 was read with no current tenant, so its old link must go; a
    lease the delta did not read keeps its links (no account-wide prune)."""
    pool = _Pool()
    link = {"lease_buildium_id": 1, "tenant_buildium_id": 10, "status": "Current"}
    read = TableRead(rows=[link], complete=True, delta=True, parents=frozenset({1, 2}))
    result = await write_mirror(pool, COMPANY, PMS, {"helixis_lease_tenants": read})
    deletes = pool.sql("DELETE")
    assert sorted((a[2], a[3]) for _s, a in deletes) == [(1, [10]), (2, [])]
    assert all("lease_buildium_id = $3" in sql for sql, _a in deletes)
    (insert,) = pool.sql("INSERT")
    assert insert[1][:3] == (COMPANY, 1, 10)
    assert "COALESCE(EXCLUDED.move_in_date" in insert[0]
    assert "helixis_lease_tenants" in result.confirmed_complete
    assert result.pruned["helixis_lease_tenants"] == 4


async def test_a_delta_link_for_a_lease_it_did_not_read_is_skipped_and_stale() -> None:
    pool = _Pool()
    link = {"lease_buildium_id": 5, "tenant_buildium_id": 10}
    read = TableRead(rows=[link], complete=True, delta=True, parents=frozenset({1}))
    result = await write_mirror(pool, COMPANY, PMS, {"helixis_lease_tenants": read})
    assert pool.sql("INSERT") == []
    assert result.skipped_no_id["helixis_lease_tenants"] == 1
    assert "helixis_lease_tenants" in result.failed


async def test_a_delta_still_replaces_children_per_parent() -> None:
    pool = _Pool()
    rows = [{"bill_id": 7, "line_index": 0, "amount": 1}]
    read = TableRead(rows=rows, complete=True, delta=True, parents=frozenset({7, 8}))
    result = await write_mirror(pool, COMPANY, PMS, {"helixis_bill_lines": read})
    assert sorted(a[2] for _s, a in pool.sql("DELETE")) == [7, 8]
    assert "helixis_bill_lines" in result.confirmed_complete


async def test_a_pruned_lease_takes_its_links_with_it() -> None:
    """A link outlives its lease otherwise, and every link reader reads a
    link as a tenant who lives there now."""
    pool = _Pool()
    leases = [{"buildium_id": 1, "status": "Active", "raw": {}}]
    read = TableRead(rows=leases, complete=True, delta=True, keep_ids=frozenset({1}))
    await write_mirror(pool, COMPANY, PMS, {"helixis_leases": read})
    link_delete = next(d for d in pool.sql("DELETE") if "FROM helixis_lease_tenants" in d[0])
    assert "NOT (lease_buildium_id = ANY($3::bigint[]))" in link_delete[0]
    assert "updated_at < now()" in link_delete[0]
    assert link_delete[1][:3] == (COMPANY, PMS, [1])


# ── rule 11, and the system on every statement ────────────────────────


async def test_every_statement_names_the_company_and_the_system() -> None:
    pool = _Pool()
    rows = {
        "helixis_properties": TableRead(rows=_props(1), complete=True),
        "helixis_leases": TableRead(rows=[{"buildium_id": 4}], complete=True),
        "helixis_lease_tenants": TableRead(
            rows=[{"lease_buildium_id": 4, "tenant_buildium_id": 9}], complete=True
        ),
        "helixis_lease_transactions": TableRead(
            rows=[{"buildium_id": 1, "lease_id": 4}], complete=True, parents=frozenset({4})
        ),
        "helixis_lease_balances": TableRead(rows=[_balance(4)], complete=True),
        "helixis_bills": TableRead(rows=[{"buildium_id": 7}], complete=True),
        "helixis_bill_lines": TableRead(
            rows=[{"bill_id": 7, "line_index": 0}], complete=True, parents=frozenset({7})
        ),
    }
    await write_mirror(pool, COMPANY, PMS, rows)
    assert len(pool.log) > 10
    for sql, args in pool.log:
        assert "company_id" in sql, sql
        assert COMPANY in args and PMS in args, sql
        if sql.upper().startswith("DELETE"):
            assert re.search(r"\bpms = \$2\b", sql), "a delete could reach another system's rows"


# ── freshness ─────────────────────────────────────────────────────────


async def test_a_stage_is_stamped_only_when_every_table_on_it_is_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stamped: list[tuple[str, int | None]] = []

    async def _record(pool: Any, *, company_id: str, table_name: str, **kw: Any) -> None:
        stamped.append((table_name, kw.get("row_count")))

    monkeypatch.setattr("helixis.db.repositories.mirror_sync_state.record_sync", _record)
    result = SyncResult(company_id=COMPANY, pms=PMS)
    result.rows_by_table = {"helixis_leases": 5, "helixis_lease_tenants": 4, "helixis_units": 3}
    result.failed = {"helixis_lease_tenants": "incomplete"}
    stages = {
        "helixis_leases": "leases",
        "helixis_lease_tenants": "leases",
        "helixis_units": "units",
        "helixis_tenants": "tenants",
    }
    done = await stamp_freshness(_Pool(), result, stages, source="rentmanager_sync")
    assert done == ["units"], "a shared stage was called fresh from its clean half"
    assert stamped == [("units", 3)]


async def test_a_delta_stamps_the_time_and_keeps_the_last_full_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A quiet incremental pass changed 0 properties; the account still has
    them. Stamping 0 would read as "the account has no properties"
    (``mirror_tools._empty_after_a_successful_sync``), so a delta's stage is
    stamped with no count and the stored total stands (``record_sync``'s
    COALESCE)."""
    stamped: list[tuple[str, int | None]] = []

    async def _record(pool: Any, *, company_id: str, table_name: str, **kw: Any) -> None:
        stamped.append((table_name, kw.get("row_count")))

    monkeypatch.setattr("helixis.db.repositories.mirror_sync_state.record_sync", _record)
    result = await write_mirror(
        _Pool(),
        COMPANY,
        PMS,
        {
            "helixis_properties": TableRead(rows=[], complete=True, delta=True),
            # A full read beside it: same pass, a count that is the table's.
            "helixis_vendors": TableRead(rows=_props(1, 2, 3, 4), complete=True),
        },
    )
    assert result.delta == {"helixis_properties"}
    assert result.rows_by_table["helixis_properties"] == 0
    stages = {"helixis_properties": "properties", "helixis_vendors": "vendors"}
    done = await stamp_freshness(_Pool(), result, stages, source="rentmanager_sync")
    assert done == ["properties", "vendors"]
    assert stamped == [("properties", None), ("vendors", 4)]
