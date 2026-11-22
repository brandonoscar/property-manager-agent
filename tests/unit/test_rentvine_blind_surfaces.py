"""Rentvine sends no balances, ledger, bills, tasks, bank accounts, staff,
applicants or lease-tenant links. Every surface that reads those tables must
say it doesn't know, never "$0 delinquent", "nothing collected" or "no tenants".

Each test runs a surface twice where it matters: as a Rentvine company (the
answer is unknown and the blind query never runs) and as a Buildium company
(the numbers still come through). The pair is what stops "always unknown"
from passing as a fix.

The last section walks the modules' AST: any function whose SQL names a table
Rentvine doesn't fill must ask ``coverage_for`` first, or be listed with the
reason it doesn't need to.
"""

from __future__ import annotations

import ast
import inspect
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.agents import mirror_tools
from helixis.db import pms_coverage
from helixis.db.pms_coverage import FED_BY, PMS_FED_TABLES, RENTVINE, Coverage, coverage_for
from helixis.db.repositories import cockpit
from helixis.db.repositories import operations as ops_repo
from helixis.db.repositories.cockpit import PropertyDetail, PropertySummary
from helixis.reconciliation import analyst
from helixis.rentvine.sync import SYNC_STATE_STAGE

CO = "co-rv"

#: Tables Buildium fills and Rentvine doesn't. Derived, so a new Rentvine
#: entity shrinks it on the same commit.
RENTVINE_BLIND = PMS_FED_TABLES - FED_BY[RENTVINE]


def _on(monkeypatch: pytest.MonkeyPatch, pms: str) -> None:
    async def _pms(_pool: Any, _company_id: str) -> str:
        return pms

    monkeypatch.setattr(pms_coverage, "company_pms_strict", _pms)


# ── a pool that records every statement ─────────────────────────────


class _Conn:
    """Answers each statement from a table of (substring, rows), and records it."""

    def __init__(self, answers: list[tuple[str, Any]]) -> None:
        self.answers = answers
        self.sql: list[str] = []

    def _answer(self, sql: str) -> Any:
        self.sql.append(sql)
        for needle, rows in self.answers:
            if needle in sql:
                return rows
        return []

    async def fetch(self, sql: str, *_a: Any) -> Any:
        return self._answer(sql)

    async def fetchrow(self, sql: str, *_a: Any) -> Any:
        rows = self._answer(sql)
        return rows[0] if isinstance(rows, list) and rows else (rows or None)

    async def fetchval(self, sql: str, *_a: Any) -> Any:
        return self._answer(sql)

    async def execute(self, sql: str, *_a: Any) -> str:
        self.sql.append(sql)
        return "OK"

    def ran(self, table: str) -> bool:
        return any(table in s for s in self.sql)


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _Conn:
        return self.conn

    async def __aexit__(self, *_a: object) -> bool:
        return False


def _pool(conn: _Conn) -> MagicMock:
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _Acquire(conn))
    return pool


_SUMMARY_ROW = {
    "buildium_id": 7,
    "name": "Garden Row",
    "address_line1": "1 Garden Way",
    "city": "Boston",
    "state": "MA",
    "unit_count": 4,
    "occupied_count": 3,
    # 2026-10-02: the summary query also returns this (occupancy unknown).
    "occupancy_unknown_count": 0,
    "open_work_orders": 1,
    "expiring_leases_90d": 0,
    "delinquent_leases": 0,
    "delinquent_total": 0,
}


def _cockpit_conn() -> _Conn:
    return _Conn(
        [
            ("AS unit_count", [_SUMMARY_ROW]),
            ("SUM(rent)", [{"scheduled": 4000, "active": 1, "priced": 1}]),
            ("AS collected", [{"collected": 1200}]),
            ("u.is_occupied", [{"buildium_id": 40, "unit_number": "1A", "property_id": 7}]),
            ("lease_from_date DESC", [{"buildium_id": 31}]),
            ("balance_0_30", [{"total_balance": 850}]),
            ("FROM helixis_lease_balances b", [{"lease_id": 31, "total_balance": 850}]),
            ("FROM helixis_tenants\n", [{"buildium_id": 9, "first_name": "Ana"}]),
        ]
    )


# ── Coverage itself ─────────────────────────────────────────────────


def test_coverage_reads_the_table_set() -> None:
    rv = Coverage(RENTVINE)
    assert rv.sees("helixis_leases")
    # Re-pointed 2026-10-01: balances sync from the lease export. Re-pointed
    # 2026-10-02: so do payments (lease transactions, from getLease); tasks
    # are what Rentvine still does not feed.
    assert rv.sees("helixis_lease_balances")
    assert rv.sees("helixis_lease_transactions")
    assert not rv.sees("helixis_tasks")
    # Occupella-written tables exist for every PMS.
    assert rv.sees("helixis_risk_flags")
    assert Coverage("buildium").sees("helixis_lease_balances")


def test_a_fed_table_that_has_not_landed_is_not_seen() -> None:
    """Lease history fills a few hundred leases a pass: until its stage is
    stamped, a lease with no Payment row may just be unread."""
    landed = frozenset(SYNC_STATE_STAGE.values()) - {"transactions"}
    rv = Coverage(RENTVINE, landed)
    assert not rv.sees("helixis_lease_transactions")
    assert rv.sees("helixis_lease_balances")
    # Nothing stamped at all (a failed freshness read answers {}): every fed
    # table is unknown, Occupella-written ones are not.
    assert not Coverage(RENTVINE, frozenset()).sees("helixis_leases")
    assert Coverage(RENTVINE, frozenset()).sees("helixis_risk_flags")
    # Buildium never waits on a stamp.
    assert Coverage("buildium", frozenset()).sees("helixis_lease_transactions")


def _unlanded(monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    landed = {s: "2026-10-02T00:00:00Z" for s in SYNC_STATE_STAGE.values() if s != stage}
    monkeypatch.setattr(pms_coverage, "get_freshness", AsyncMock(return_value=landed))


def test_a_failed_lookup_sees_no_pms_table() -> None:
    unknown = Coverage(None)
    assert not unknown.sees("helixis_leases")
    assert unknown.sees("helixis_risk_flags")
    assert unknown.name is None


def test_the_sentence_names_the_system_and_an_action() -> None:
    line = Coverage(RENTVINE).not_sent("rent balances", "each tenant's ledger")
    assert "Rentvine" in line and "each tenant's ledger" in line
    assert "$0" not in line
    assert "property management system" in Coverage(None).not_sent("rent balances", "x")


@pytest.mark.asyncio
async def test_coverage_for_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(_pool: Any, _company_id: str) -> str:
        raise RuntimeError("db down")

    monkeypatch.setattr(pms_coverage, "company_pms_strict", _boom)
    assert (await coverage_for(MagicMock(), CO)).pms is None


# ── the property cockpit ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cockpit_list_counts_balances_for_rentvine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-pointed 2026-10-01: Rentvine balances sync, so the cockpit counts
    them exactly as it does Buildium's (same rows-only-for-owing snapshot)."""
    _on(monkeypatch, RENTVINE)
    (s,) = await cockpit.list_property_summaries(_pool(_cockpit_conn()), CO)
    assert s.delinquent_leases == 0 and s.delinquent_total == 0.0
    assert s.open_work_orders == 1


@pytest.mark.asyncio
async def test_cockpit_list_still_counts_for_buildium(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, "buildium")
    (s,) = await cockpit.list_property_summaries(_pool(_cockpit_conn()), CO)
    assert s.delinquent_leases == 0 and s.delinquent_total == 0.0


@pytest.mark.asyncio
async def test_property_detail_for_rentvine(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, RENTVINE)
    conn = _cockpit_conn()
    detail = await cockpit.get_property_detail(_pool(conn), CO, 7)
    assert detail is not None
    # Re-pointed 2026-10-01: Rentvine now syncs lease-tenant links and
    # balances (both from the lease export), so this surface reads them.
    # Re-pointed 2026-10-02: payments too, once their stage has landed.
    assert detail.rent_collected_mtd == 1200.0
    assert detail.tenants == []
    assert conn.ran("helixis_lease_tenants")
    assert detail.delinquency_list == [{"lease_id": 31, "total_balance": 850}]
    assert detail.rent_scheduled == 4000.0
    assert detail.pms_name == "Rentvine"
    assert conn.ran("helixis_lease_transactions")


@pytest.mark.asyncio
async def test_property_detail_before_rentvine_payments_land(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Collected this month is unknown, never a short sum of the leases read so far."""
    _on(monkeypatch, RENTVINE)
    _unlanded(monkeypatch, "transactions")
    conn = _cockpit_conn()
    detail = await cockpit.get_property_detail(_pool(conn), CO, 7)
    assert detail is not None
    assert detail.rent_collected_mtd is None
    assert not conn.ran("helixis_lease_transactions")
    assert detail.delinquency_list == [{"lease_id": 31, "total_balance": 850}]


@pytest.mark.asyncio
async def test_property_detail_for_buildium(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, "buildium")
    detail = await cockpit.get_property_detail(_pool(_cockpit_conn()), CO, 7)
    assert detail is not None
    assert detail.rent_collected_mtd == 1200.0
    assert detail.delinquency_list == [{"lease_id": 31, "total_balance": 850}]
    assert detail.tenants == []


@pytest.mark.asyncio
async def test_unit_detail_for_rentvine(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, RENTVINE)
    conn = _cockpit_conn()
    detail = await cockpit.get_unit_detail(_pool(conn), CO, 40)
    assert detail is not None
    # Re-pointed 2026-10-01: Rentvine now syncs lease-tenant links and
    # balances (from the lease export), so this surface reads them; tasks
    # stay blind.
    assert detail.balances_sent is True and detail.balance is not None
    assert detail.tenants == [] and detail.open_tasks is None
    assert conn.ran("helixis_lease_tenants") and conn.ran("helixis_lease_balances")
    assert not conn.ran("helixis_tasks")


@pytest.mark.asyncio
async def test_unit_detail_for_buildium(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, "buildium")
    detail = await cockpit.get_unit_detail(_pool(_cockpit_conn()), CO, 40)
    assert detail is not None
    assert detail.balances_sent is True and detail.balance == {"total_balance": 850}
    assert detail.open_tasks == []


@pytest.mark.asyncio
async def test_tenant_detail_for_rentvine(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, RENTVINE)
    conn = _cockpit_conn()
    detail = await cockpit.get_tenant_detail(_pool(conn), CO, 9)
    assert detail is not None
    # Re-pointed 2026-10-01: Rentvine now syncs lease-tenant links (from the
    # lease export), so this surface reads them. Re-pointed 2026-10-02: and
    # the ledger, once its stage has landed (the unlanded case is below).
    assert isinstance(detail.leases, list)
    assert conn.ran("helixis_lease_tenants")
    assert all(lease.get("total_balance") is None for lease in detail.leases)
    assert detail.recent_transactions == []
    assert conn.ran("helixis_lease_transactions")


@pytest.mark.asyncio
async def test_tenant_detail_before_rentvine_payments_land(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on(monkeypatch, RENTVINE)
    _unlanded(monkeypatch, "transactions")
    conn = _cockpit_conn()
    detail = await cockpit.get_tenant_detail(_pool(conn), CO, 9)
    assert detail is not None
    # Unknown, never an empty ledger.
    assert detail.recent_transactions is None
    assert not conn.ran("helixis_lease_transactions")


@pytest.mark.asyncio
async def test_tenant_detail_for_buildium(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, "buildium")
    detail = await cockpit.get_tenant_detail(_pool(_cockpit_conn()), CO, 9)
    assert detail is not None
    assert isinstance(detail.leases, list) and detail.recent_transactions == []


# ── the Operations page ─────────────────────────────────────────────


_VENDOR_ROW = {
    "buildium_id": 5,
    "name": "Volt & Vine",
    "category_name": None,
    "is_active": True,
    "phone": None,
    "email": None,
    "open_work_orders": 0,
    "total_work_orders": 0,
    "jobs_90d": 0,
    "avg_days_to_complete": None,
    "spend_12m": 0,
    "bill_count_12m": 0,
    "last_bill_date": None,
}


@pytest.mark.asyncio
async def test_operations_for_rentvine(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, RENTVINE)
    conn = _Conn([("FROM helixis_vendors v", [_VENDOR_ROW])])
    pool = _pool(conn)
    # Re-pointed 2026-10-01: Rentvine now syncs lease-tenant links (from the
    # lease export), so this surface reads them; balances and payments stay blind.
    assert await ops_repo.list_tenant_ops(pool, CO) == []
    assert await ops_repo.list_team_ops(pool, CO) is None
    (v,) = await ops_repo.list_vendor_ops(pool, CO)
    # Re-pointed 2026-10-01: Rentvine bills sync (paid and unpaid, the whole
    # book), so vendor spend is read from the row, not declined.
    assert v.spend_12m == 0 and v.bill_count_12m == 0
    # Vendor attribution reads Buildium's raw key; for Rentvine it is unknown.
    assert v.total_work_orders is None and v.open_work_orders is None
    assert conn.ran("helixis_lease_tenants") and not conn.ran("buildium_staff")


@pytest.mark.asyncio
async def test_operations_for_buildium(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, "buildium")
    pool = _pool(_Conn([("FROM helixis_vendors v", [_VENDOR_ROW])]))
    assert await ops_repo.list_tenant_ops(pool, CO) == []
    assert await ops_repo.list_team_ops(pool, CO) == []
    (v,) = await ops_repo.list_vendor_ops(pool, CO)
    assert v.spend_12m == 0.0 and v.total_work_orders == 0


_KEYED = "COUNT(*) FILTER (WHERE raw ?"


@pytest.mark.asyncio
async def test_rentvine_work_orders_are_attributed_when_they_carry_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on(monkeypatch, RENTVINE)
    row = {**_VENDOR_ROW, "open_work_orders": 1, "total_work_orders": 3, "jobs_90d": 0}
    conn = _Conn([(_KEYED, [{"work_orders": 3, "keyed": 3}]), ("FROM helixis_vendors v", [row])])
    (v,) = await ops_repo.list_vendor_ops(_pool(conn), CO)
    assert (v.total_work_orders, v.open_work_orders) == (3, 1)
    # Jobs-in-90-days comes from Buildium's status history: unknown, not 0.
    assert v.jobs_90d is None and v.avg_days_to_complete is None
    # The vendor rollup itself reads Rentvine's key (the key-presence probe
    # names it too, so only the rollup's own SQL proves anything).
    (vendor_sql,) = [sql for sql in conn.sql if "FROM helixis_vendors v" in sql]
    assert "wo.raw->>'vendorContactID'" in vendor_sql


@pytest.mark.asyncio
async def test_rentvine_rows_without_the_key_stay_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """The key comes from the spec, not a live row. If Rentvine's rows don't
    carry it, counting would show every vendor as never used."""
    _on(monkeypatch, RENTVINE)
    conn = _Conn(
        [(_KEYED, [{"work_orders": 4, "keyed": 0}]), ("FROM helixis_vendors v", [_VENDOR_ROW])]
    )
    (v,) = await ops_repo.list_vendor_ops(_pool(conn), CO)
    assert v.total_work_orders is None and v.open_work_orders is None


@pytest.mark.asyncio
async def test_no_rentvine_work_orders_means_zero_not_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on(monkeypatch, RENTVINE)
    conn = _Conn(
        [(_KEYED, [{"work_orders": 0, "keyed": 0}]), ("FROM helixis_vendors v", [_VENDOR_ROW])]
    )
    (v,) = await ops_repo.list_vendor_ops(_pool(conn), CO)
    assert v.total_work_orders == 0


def test_the_rentvine_vendor_key_is_the_one_the_spec_shows() -> None:
    """Pins the key to T1's own example, so a rename here has to face it."""
    import json
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    spec = json.loads((root / "docs/rentvine/vendor/docs.rentvine.com/openapi.json").read_text())
    examples = spec["paths"]["/maintenance/work-orders"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["examples"]
    assigned = examples["withVendorAssigned"]["value"][0]
    assert assigned["workOrder"][ops_repo._RENTVINE_VENDOR_KEY] == str(
        assigned["contact"]["contactID"]
    )
    assert ops_repo._RENTVINE_VENDOR_KEY in examples["noVendorAssigned"]["value"][0]["workOrder"]


# ── the chat's mirror tools ─────────────────────────────────────────


@contextmanager
def _tools(conn: _Conn) -> Iterator[None]:
    """The tools against ``conn``, for a company whose balances stage has
    landed (a test about the never-landed case patches the stamp to None)."""
    ctx = MagicMock()
    ctx.company_id = CO
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=ctx),
        patch.object(mirror_tools, "get_pool", return_value=_pool(conn)),
        patch.object(mirror_tools, "_freshness_line", AsyncMock(return_value="(as of now)")),
        patch.object(mirror_tools.sync_state, "get_stage_sync", AsyncMock(return_value=_stamped())),
    ):
        yield


def _stamped(row_count: int = 0) -> Any:
    from datetime import UTC, datetime

    from helixis.db.repositories.mirror_sync_state import StageSync

    return StageSync(synced_at=datetime.now(UTC), row_count=row_count)


@pytest.mark.asyncio
@pytest.mark.parametrize("pms", [RENTVINE, "buildium"])
async def test_delinquency_report_clears_only_after_the_balances_stage_landed(
    monkeypatch: pytest.MonkeyPatch, pms: str
) -> None:
    """Re-pointed 2026-10-01: Rentvine balances sync now (lease export), so a
    Rentvine company is held to the same rule as a Buildium one. "Every lease
    is current" needs the balances stage's stamp; without it, an empty table
    is "not synced yet", never "current"."""
    _on(monkeypatch, pms)
    summaries = AsyncMock(return_value=[PropertySummary(7, "G", None, None, None)])
    for stamp, expected in ((_stamped(), "every lease is current"), (None, "haven't synced")):
        with (
            _tools(_Conn([])),
            patch.object(cockpit, "list_property_summaries", summaries),
            patch.object(mirror_tools.sync_state, "get_stage_sync", AsyncMock(return_value=stamp)),
        ):
            out = await mirror_tools._delinquency_report_impl()
        assert expected in out, (pms, stamp, out)
        assert "doesn't sync" not in out
    assert "Buildium" not in out or pms == "buildium"


def _blind_summary() -> PropertySummary:
    return PropertySummary(7, "Garden Row", None, None, None, 4, 3, 1, 0, None, None)


@pytest.mark.asyncio
async def test_portfolio_overview_has_no_zero_outstanding(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, RENTVINE)
    with (
        _tools(_Conn([])),
        patch.object(
            cockpit, "list_property_summaries", AsyncMock(return_value=[_blind_summary()])
        ),
    ):
        out = await mirror_tools._portfolio_overview_impl()
    assert "outstanding" not in out and "$0" not in out
    assert "Occupella doesn't sync rent balances from Rentvine" in out


@pytest.mark.asyncio
async def test_expiring_leases_clears_a_rentvine_tenant_only_after_balances_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-pointed 2026-10-01: Rentvine balances and tenant links both sync, so
    "no amount owed" means "owes nothing", but only once the balances stage
    has landed; before that the footer must not clear anyone."""
    _on(monkeypatch, RENTVINE)
    row = {
        "buildium_id": 31,
        "lease_to_date": None,
        "rent": 1500,
        "security_deposit": None,
        "property_name": "Garden Row",
        "unit_number": "1A",
        "open_work_orders": 0,
        "open_tasks": 0,
        "total_balance": None,
        "tenant_names": None,
    }
    with _tools(_Conn([("FROM helixis_leases l", [row])])):
        out = await mirror_tools._expiring_leases_impl(90)
    assert "i.e. it owes nothing" in out and "Buildium" not in out
    with (
        _tools(_Conn([("FROM helixis_leases l", [row])])),
        patch.object(mirror_tools.sync_state, "get_stage_sync", AsyncMock(return_value=None)),
    ):
        out = await mirror_tools._expiring_leases_impl(90)
    assert "owes nothing" not in out
    assert "NOT the same as nothing overdue" in out
    assert "rent balances" in out and "tenant names" not in out


@pytest.mark.asyncio
async def test_property_snapshot_prints_no_zero_money(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, RENTVINE)
    detail = PropertyDetail(
        summary=_blind_summary(),
        rent_scheduled=4000.0,
        rent_collected_mtd=None,
        tenants=None,
        delinquency_list=None,
        pms_name="Rentvine",
    )
    with (
        _tools(_Conn([])),
        patch.object(
            cockpit, "list_property_summaries", AsyncMock(return_value=[_blind_summary()])
        ),
        patch.object(cockpit, "get_property_detail", AsyncMock(return_value=detail)),
        patch.object(cockpit, "list_units_for_property", AsyncMock(return_value=[])),
        patch.object(mirror_tools, "_owner_of_property", AsyncMock(return_value="")),
    ):
        out = await mirror_tools._property_snapshot_impl("Garden")
    assert "$0" not in out
    assert "collected this month" not in out and "Delinquent:" not in out
    assert "rent balances, payments and who lives in which unit" in out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("impl", "table", "what"),
    [
        # spending_report left this list 2026-10-02: Rentvine bill LINES and GL
        # accounts sync now (ADR 0010 Decision (b)); see the positive test below.
        (lambda: mirror_tools._open_tasks_impl(), "helixis_tasks", "tasks"),
        (lambda: mirror_tools._bank_balances_impl(), "helixis_bank_accounts", "bank accounts"),
    ],
)
async def test_whole_tool_declines(
    monkeypatch: pytest.MonkeyPatch, impl: Any, table: str, what: str
) -> None:
    _on(monkeypatch, RENTVINE)
    conn = _Conn([])
    with _tools(conn):
        out = await impl()
    assert f"Occupella doesn't sync {what} from Rentvine" in out
    assert not conn.ran(table)


@pytest.mark.asyncio
async def test_spending_report_runs_for_rentvine_and_states_unattributed_spend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0010 Decision (b): spend on a portfolio or management ledger is in
    the category totals but on no property, and the report SAYS how much
    rather than letting the property rollup silently come up short."""
    _on(monkeypatch, RENTVINE)
    conn = _Conn(
        [
            ("ledger_type IN (1, 2)", 1250.0),
            ("GROUP BY p.name", [{"property_name": "Garden Row", "total": 480.0, "bill_count": 1}]),
            (
                "GROUP BY 1, 2",
                [{"gl_name": "Repairs", "gl_type": "Expense", "total": 1730.0, "bill_count": 2}],
            ),
        ]
    )
    with _tools(conn):
        out = await mirror_tools._spending_report_impl(90, "")
    assert "Occupella doesn't sync" not in out
    assert "Total: $1,730." in out
    assert "Garden Row: $480" in out
    assert "Not counted by property: $1,250" in out
    assert "Buildium" not in out


@pytest.mark.asyncio
async def test_spending_report_scoped_to_a_property_does_not_claim_unattributed_spend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A property filter already drops ledger 1/2 lines from every total, so
    the "It IS in the category totals" line would be false there."""
    _on(monkeypatch, RENTVINE)
    conn = _Conn(
        [
            ("ledger_type IN (1, 2)", 1250.0),
            ("GROUP BY p.name", [{"property_name": "Garden Row", "total": 480.0, "bill_count": 1}]),
            (
                "GROUP BY 1, 2",
                [{"gl_name": "Repairs", "gl_type": "Expense", "total": 480.0, "bill_count": 1}],
            ),
        ]
    )
    with _tools(conn):
        out = await mirror_tools._spending_report_impl(90, "Garden")
    assert "Not counted by property" not in out
    assert not any("ledger_type IN (1, 2)" in q for q in conn.sql)


@pytest.mark.asyncio
async def test_unpaid_bills_runs_for_rentvine_and_names_no_other_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rentvine bills sync since 2026-10-01 (``sync._sync_bills``), so the tool
    answers rather than declines, and its output (which the model reads on a
    Rentvine run) says nothing about Buildium."""
    _on(monkeypatch, RENTVINE)
    bill = {
        "buildium_id": 990000701,
        "due_date": None,
        "reference_number": "INV-1",
        "total_amount": 480.0,
        "vendor_name": "Synthetic Plumbing Co",
        "memo": "Kitchen sink repair",
        "properties": None,
    }
    conn = _Conn([("is_paid IS NOT TRUE", [bill]), ("is_paid IS TRUE", [{"paid": 3}])])
    with _tools(conn):
        out = await mirror_tools._unpaid_bills_impl()
    assert "Synthetic Plumbing Co: $480 — Kitchen sink repair" in out
    assert "Total unpaid: $480 across 1 bills" in out
    assert "doesn't sync" not in out
    assert "buildium" not in out.lower()
    # The bill's own description is read when it has no line rows.
    sql = next(q for q in conn.sql if "is_paid IS NOT TRUE" in q)
    assert "b.raw->>'description'" in sql


@pytest.mark.asyncio
async def test_vendor_performance_prints_no_zero_billing(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, RENTVINE)
    row = ops_repo.VendorOpsRow(
        buildium_id=5,
        name="Volt & Vine",
        category_name=None,
        is_active=True,
        phone=None,
        email=None,
        open_work_orders=None,
        total_work_orders=None,
        jobs_90d=None,
        spend_12m=None,
        bill_count_12m=None,
    )
    with (
        _tools(_Conn([])),
        patch.object(ops_repo, "list_vendor_ops", AsyncMock(return_value=[row])),
    ):
        out = await mirror_tools._vendor_performance_impl()
    assert "$0" not in out and "0 work orders" not in out and "billed" not in out
    assert "Occupella doesn't sync bills and which vendor did each work order" in out


@pytest.mark.asyncio
async def test_contact_lookup_does_not_say_no_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, RENTVINE)
    tenant = {
        "first_name": "Ana",
        "last_name": "Diaz",
        "email": None,
        "phone": None,
        "properties": None,
        "lease_status": None,
        "ended_on": None,
        "link_status": None,
    }
    with (
        _tools(_Conn([("FROM helixis_tenants t", [tenant])])),
        patch.object(mirror_tools.entity_aliases, "lookup_alias", AsyncMock(return_value=None)),
    ):
        out = await mirror_tools._contact_lookup_impl("Ana")
    # Re-pointed 2026-10-01: Rentvine links are synced, ACTIVE ones only, and a
    # complete export prunes the rest — so a tenant with no link has no current
    # lease on file, the same reading a Buildium tenant gets.
    assert "no lease on file" in out
    assert "current or former unknown" not in out


# ── the nightly reconciliation analyst ──────────────────────────────


@pytest.mark.asyncio
async def test_analyst_skips_rentvine_until_its_ledger_lands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-pointed 2026-10-02: Rentvine sends every table the dossier reads now,
    so the analyst runs once they have all landed (test below). Until the
    ledger has, every lease would read as "no payment for three months"."""
    _on(monkeypatch, RENTVINE)
    _unlanded(monkeypatch, "transactions")
    completion = AsyncMock(side_effect=AssertionError("the model must not be asked"))
    # A non-empty dossier, so only the skip can stop the model being asked:
    # an empty one returns 0 on its own, which is what let a mutant through.
    conn = _Conn([("AS days_to_end", [{"lease_id": 31, "balance": 0.0}])])
    written = await analyst.analyze_company(_pool(conn), MagicMock(), CO, completion_fn=completion)
    assert written == 0
    completion.assert_not_called()
    assert not conn.ran("helixis_lease_balances")


@pytest.mark.asyncio
async def test_analyst_runs_for_rentvine_once_its_tables_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on(monkeypatch, RENTVINE)
    reply = MagicMock()
    reply.choices[0].message.content = "[]"
    completion = AsyncMock(return_value=reply)
    conn = _Conn([("AS days_to_end", [{"lease_id": 31, "balance": 0.0}])])
    await analyst.analyze_company(_pool(conn), MagicMock(), CO, completion_fn=completion)
    completion.assert_called_once()
    assert conn.ran("helixis_lease_transactions")


def test_analyst_reads_is_the_sql_it_runs() -> None:
    source = inspect.getsource(analyst)
    named = set(re.findall(r"\bhelixis_[a-z_]+\b", source)) & PMS_FED_TABLES
    assert named == analyst.ANALYST_READS


# ── the guard: a blind read must ask first ──────────────────────────

#: Functions that read a table Rentvine doesn't fill without asking
#: ``coverage_for`` themselves, and why that is safe.
_EXEMPT: dict[str, str] = {}

_MODULES = {"mirror_tools": mirror_tools, "cockpit": cockpit, "operations": ops_repo}


def _strings(node: ast.AST, constants: dict[str, str]) -> str:
    """Every string literal under ``node``, plus module constants it names."""
    out: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.append(sub.value)
        elif isinstance(sub, ast.Name) and sub.id in constants:
            out.append(constants[sub.id])
    return "\n".join(out)


def _calls(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            names.add(fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", ""))
    return names


def _blind_readers() -> Iterator[tuple[str, frozenset[str], bool]]:
    for mod_name, module in _MODULES.items():
        tree = ast.parse(inspect.getsource(module))
        constants: dict[str, str] = {}
        for n in tree.body:
            targets = n.targets if isinstance(n, ast.Assign) else [getattr(n, "target", None)]
            value = getattr(n, "value", None)
            if isinstance(value, ast.Constant | ast.JoinedStr):
                # An f-string constant keeps its literal parts.
                text = _strings(value, {})
                for t in targets:
                    if isinstance(t, ast.Name):
                        constants[t.id] = text
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            sql = _strings(fn, constants)
            # FROM/JOIN only: a docstring naming a table is not a read of it
            # (rule 17 — the comment explaining a rule quotes the defect).
            blind = frozenset(
                t for t in RENTVINE_BLIND if re.search(rf"\b(?:FROM|JOIN)\s+{t}\b", sql)
            )
            if blind:
                yield f"{mod_name}.{fn.name}", blind, "coverage_for" in _calls(fn)


def test_every_blind_read_asks_coverage_first() -> None:
    readers = list(_blind_readers())
    # A walk that finds nothing proves nothing (rule 10). Re-pointed 2026-10-02
    # from a count (>= 10, which went stale as Rentvine learned tables, rule
    # 22) to readers the walk must always find while their tables stay blind:
    # tasks and lease transactions are not Rentvine-fed.
    names = {name for name, _blind, _asks in readers}
    assert {"cockpit.get_unit_detail", "mirror_tools._delinquency_report_impl"} <= names, names
    unasked = [
        (name, sorted(blind)) for name, blind, asks in readers if not asks and name not in _EXEMPT
    ]
    assert not unasked, unasked


def test_every_exemption_still_names_a_blind_reader() -> None:
    names = {name for name, _blind, _asks in _blind_readers()}
    assert set(_EXEMPT) <= names, set(_EXEMPT) - names


# ── the routes carry null to the page, not 0 ────────────────────────


def _app() -> Any:
    from fastapi import FastAPI

    from helixis.api.auth import AuthenticatedUser, get_current_user
    from helixis.api.routes import operations as ops_routes
    from helixis.api.routes import properties as properties_routes

    app = FastAPI()
    app.include_router(properties_routes.router, prefix="/api/v1")
    app.include_router(ops_routes.router, prefix="/api/v1")
    user = AuthenticatedUser(user_id="u", email="u@example.com", company_ids=[CO])
    app.dependency_overrides[get_current_user] = lambda: user
    return app


def test_cockpit_routes_send_null_and_the_system_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from helixis.api.routes import properties as properties_routes

    _on(monkeypatch, RENTVINE)
    monkeypatch.setattr(properties_routes, "get_pool", lambda: MagicMock())
    monkeypatch.setattr(
        cockpit, "list_property_summaries", AsyncMock(return_value=[_blind_summary()])
    )
    detail = PropertyDetail(
        summary=_blind_summary(),
        rent_collected_mtd=None,
        tenants=None,
        delinquency_list=None,
        pms_name="Rentvine",
    )
    monkeypatch.setattr(cockpit, "get_property_detail", AsyncMock(return_value=detail))
    client = TestClient(_app())

    listing = client.get("/api/v1/properties/cockpit").json()
    assert listing["pms_name"] == "Rentvine"
    assert listing["properties"][0]["delinquent_total"] is None

    body = client.get("/api/v1/properties/cockpit/7").json()
    assert body["rent_collected_mtd"] is None
    assert body["tenants"] is None and body["delinquency_list"] is None
    assert body["pms_name"] == "Rentvine"


def test_operations_routes_send_null(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from helixis.api.routes import operations as ops_routes

    _on(monkeypatch, RENTVINE)
    monkeypatch.setattr(ops_routes, "get_pool", lambda: MagicMock())
    monkeypatch.setattr(ops_repo, "list_tenant_ops", AsyncMock(return_value=None))
    monkeypatch.setattr(ops_repo, "list_team_ops", AsyncMock(return_value=None))
    client = TestClient(_app())

    tenants = client.get("/api/v1/operations/tenants").json()
    assert tenants == {"tenants": None, "pms_name": "Rentvine"}
    assert client.get("/api/v1/operations/team").json()["members"] is None


# ── both sides of the boundary (rule 15) ────────────────────────────

#: The shared file both tests read: this one derives it from the response
#: models, and ``frontend/src/services/mirrorNullable.test.ts`` checks each
#: listed field is ``| null`` in the TypeScript interface of the same name.
_NULLABLE_JSON = "frontend/src/services/mirrorNullableFields.json"


def test_the_frontend_is_told_which_fields_can_be_null() -> None:
    import json
    import pathlib
    import typing

    from helixis.api.routes import operations as ops_routes
    from helixis.api.routes import properties as properties_routes

    models = {
        "CockpitSummary": properties_routes.CockpitSummary,
        "CockpitDetail": properties_routes.CockpitDetailResponse,
        "UnitDetail": properties_routes.UnitDetailResponse,
        "TenantDetail": properties_routes.TenantDetailResponse,
        "VendorOps": ops_routes.VendorOpsOut,
        "TenantOps": ops_routes.TenantOpsOut,
        "TeamOps": ops_routes.TeamOpsOut,
    }
    derived = {
        ts_name: sorted(
            name
            for name, f in model.model_fields.items()
            if type(None) in typing.get_args(f.annotation)
        )
        for ts_name, model in models.items()
    }
    root = pathlib.Path(__file__).resolve().parents[2]
    shared = json.loads((root / _NULLABLE_JSON).read_text())
    assert shared == derived, (
        f"Regenerate {_NULLABLE_JSON} from the response models; the frontend "
        "test then checks the TypeScript types accept null for each field."
    )
    # The fields this change made nullable are in it (a vacuous file proves nothing).
    assert "delinquent_total" in shared["CockpitSummary"]
    assert "spend_12m" in shared["VendorOps"]


# ── the empty-mirror message for a Rentvine company ─────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("synced_empty", "expected"),
    [(False, "Nothing has synced from Rentvine"), (True, "zero properties")],
)
async def test_an_empty_rentvine_mirror_is_not_a_missing_buildium_connection(
    monkeypatch: pytest.MonkeyPatch, synced_empty: bool, expected: str
) -> None:
    """Right after the wizard connects Rentvine the mirror is empty, and the
    Buildium check read "no Buildium credential" as "disconnected": the
    customer was told to reconnect a system they have never used."""
    _on(monkeypatch, RENTVINE)
    buildium_check = AsyncMock(return_value=False)
    with (
        patch.object(mirror_tools, "get_pool", return_value=MagicMock()),
        patch.object(mirror_tools, "_buildium_is_connected", buildium_check),
        patch.object(
            mirror_tools, "_empty_after_a_successful_sync", AsyncMock(return_value=synced_empty)
        ),
    ):
        out = await mirror_tools._not_synced(CO)
    assert expected in out
    assert "no Buildium connection" not in out and "Sync data" not in out
    buildium_check.assert_not_called()


@pytest.mark.asyncio
async def test_a_failed_lookup_keeps_the_buildium_wording(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(_pool: Any, _company_id: str) -> str:
        raise RuntimeError("db down")

    monkeypatch.setattr(pms_coverage, "company_pms_strict", _boom)
    with (
        patch.object(mirror_tools, "get_pool", return_value=MagicMock()),
        patch.object(mirror_tools, "_buildium_is_connected", AsyncMock(return_value=None)),
        patch.object(mirror_tools, "_empty_after_a_successful_sync", AsyncMock(return_value=False)),
    ):
        out = await mirror_tools._not_synced(CO)
    assert out == mirror_tools._NOT_SYNCED


# ── which system the page is looking at ─────────────────────────────


def test_freshness_tells_members_which_pms(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Operations page needs "Rentvine or Buildium" to pick its Re-sync and
    its wording, and the credential routes are admin-only."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from helixis.api.auth import AuthenticatedUser, get_current_user
    from helixis.api.routes import mirror as mirror_routes

    _on(monkeypatch, RENTVINE)
    monkeypatch.setattr(mirror_routes, "get_pool", lambda: MagicMock())

    async def _boom(*_a: Any) -> Any:
        raise RuntimeError("freshness table unreadable")

    monkeypatch.setattr(mirror_routes.sync_state, "get_freshness", _boom)
    app = FastAPI()
    app.include_router(mirror_routes.router, prefix="/api/v1")
    member = AuthenticatedUser(user_id="u", email="u@example.com", company_ids=[CO])
    app.dependency_overrides[get_current_user] = lambda: member
    body = TestClient(app).get("/api/v1/mirror/freshness").json()
    # Even when the freshness read fails, the page still learns its system.
    assert body["pms"] == "rentvine" and body["pms_name"] == "Rentvine"
    assert body["has_data"] is False
