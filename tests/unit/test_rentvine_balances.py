"""Rentvine rent balances: the lease export's ``balances`` -> ``helixis_lease_balances``.

The export sends each lease's ``balances`` (and ``unpaidCharges``) BESIDE the
lease record; the sync lifts them off before the lease is written, and
``sync._replace_balances`` replaces this company's snapshot only when it can
stand behind all of it: a complete export and every lease's balance readable.
Rows exist only for leases that owe, as on the Buildium path. All rows are
synthetic (ids 990 000 xxx); the live shape is NOT VERIFIED (runbook "Rent
balances").
"""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from helixis.rentvine import mapper, sync
from helixis.settings import Settings

from ._pg_bind_contract import reject_unbindable, reject_untyped_mirror_bind
from .test_rentvine_sync import EXPORT_BALANCES, _client, export_leases, lease_route

CO = "6a456191-0000-4000-8000-000000000001"
TODAY = date(2026, 10, 1)


def _lease(lease_id: int = 990000601, balances: Any = None, charges: Any = None) -> dict[str, Any]:
    return {
        "leaseID": lease_id,
        "propertyID": 990000101,
        "unitID": 990000201,
        "balances": balances,
        "unpaidCharges": charges,
    }


def _owes(total: Any) -> dict[str, Any]:
    return {**EXPORT_BALANCES, "unpaidTotalAmount": total}


# ── the mapper ────────────────────────────────────────────────────────────────


def test_the_total_is_every_unpaid_charge():
    row = mapper.map_lease_balance(_lease(balances=_owes(1450)), TODAY)
    assert row["total_balance"] == Decimal("1450")
    assert row["lease_id"] == 990000601 and row["pms"] == "rentvine"
    assert row["property_id"] == 990000101 and row["unit_id"] == 990000201


def test_aging_is_filled_when_the_charges_add_up():
    charges = [
        {"amount": 1450, "amountPaid": 0, "datePosted": "2026-09-01"},  # 30 days
        {"amount": 100, "amountPaid": 50, "datePosted": "2026-07-15"},  # 78 days
        {"amount": 25, "amountPaid": 25, "datePosted": "2026-01-01"},  # paid
        {"amount": 200, "amountPaid": 0, "datePosted": "2026-05-01"},  # 153 days
    ]
    row = mapper.map_lease_balance(_lease(balances=_owes(1700), charges=charges), TODAY)
    assert row["balance_0_30"] == Decimal("1450")
    assert row["balance_31_60"] == Decimal("0")
    assert row["balance_61_90"] == Decimal("50")
    assert row["balance_over_90"] == Decimal("200")


def test_an_overpaid_charge_owes_nothing_rather_than_reducing_the_others():
    """amountPaid above amount is not a negative debt against other charges."""
    charges = [
        {"amount": 1450, "amountPaid": 0, "datePosted": "2026-09-20"},
        {"amount": 100, "amountPaid": 150, "datePosted": "2026-09-20"},
    ]
    row = mapper.map_lease_balance(_lease(balances=_owes(1450), charges=charges), TODAY)
    assert row["balance_0_30"] == Decimal("1450")


@pytest.mark.parametrize(
    "charges",
    [
        None,
        {"amount": 1450, "amountPaid": 0, "datePosted": "2026-09-01"},  # one object
        [{"amount": 1000, "amountPaid": 0, "datePosted": "2026-09-01"}],  # does not add up
        [{"amount": "n/a", "amountPaid": 0, "datePosted": "2026-09-01"}],
        [{"amount": 1450, "amountPaid": 0, "datePosted": None}],
        ["junk"],
    ],
)
def test_aging_is_unknown_unless_the_charges_reconcile(charges):
    """Unknown buckets read as "no aging", never as "current" (rule 14)."""
    row = mapper.map_lease_balance(_lease(balances=_owes(1450), charges=charges), TODAY)
    assert row["total_balance"] == Decimal("1450")
    assert all(row[b] is None for b in ("balance_0_30", "balance_31_60", "balance_61_90"))
    assert row["balance_over_90"] is None


@pytest.mark.parametrize(
    "balances",
    [None, "junk", {}, {"unpaidTotalAmount": None}, {"unpaidTotalAmount": "n/a"}],
)
def test_a_balance_that_did_not_come_through_is_unknown(balances):
    assert mapper.lease_balance_known(_lease(balances=balances), TODAY) is mapper.BALANCE_UNKNOWN


def test_a_lease_with_no_id_is_unknown():
    lease = {**_lease(balances=_owes(10)), "leaseID": None}
    assert mapper.lease_balance_known(lease, TODAY) is mapper.BALANCE_UNKNOWN


def test_a_lease_that_owes_nothing_gets_no_row():
    assert mapper.lease_balance_known(_lease(balances=_owes(0)), TODAY) is None


def test_a_lease_that_owes_gets_a_row():
    row = mapper.lease_balance_known(_lease(balances=_owes("12.50")), TODAY)
    assert isinstance(row, dict) and row["total_balance"] == Decimal("12.50")


# ── the stage ─────────────────────────────────────────────────────────────────


def _pool() -> Any:
    conn = MagicMock()

    def _enforce(sql: str, *args: Any) -> str:
        reject_unbindable(sql, args)
        reject_untyped_mirror_bind(sql, args)
        return "DELETE 2" if sql.startswith("DELETE") else "INSERT 0 1"

    conn.execute = AsyncMock(side_effect=_enforce)
    conn.fetch = AsyncMock(return_value=[])
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    pool._conn = conn
    return pool


def _calls(pool: Any, prefix: str) -> list[Any]:
    return [c for c in pool._conn.execute.await_args_list if c.args[0].lstrip().startswith(prefix)]


def _replace(rows: list[dict[str, Any]], gaps: list[str] | None = None) -> tuple[Any, Any]:
    pool = _pool()
    result = sync.SyncResult(company_id=CO)
    asyncio.run(sync._replace_balances(pool, CO, rows, list(gaps or []), result))
    return pool, result


def test_a_complete_read_replaces_the_snapshot_with_the_leases_that_owe():
    rows = [_lease(1, _owes(1450)), _lease(2, _owes(0)), _lease(3, _owes(80))]
    pool, result = _replace(rows)
    (delete,) = _calls(pool, "DELETE FROM helixis_lease_balances")
    assert "company_id = $1::uuid" in delete.args[0] and "pms = 'rentvine'" in delete.args[0]
    assert delete.args[1] == CO and sorted(delete.args[2]) == [1, 3]
    inserts = _calls(pool, "INSERT INTO helixis_lease_balances")
    assert [c.args[2] for c in inserts] == [1, 3]
    assert all(c.args[3] == 0 and c.args[4] == "rentvine" for c in inserts)
    assert result.rows_by_table["helixis_lease_balances"] == 2
    assert "helixis_lease_balances" in result.confirmed_complete
    assert result.pruned["helixis_lease_balances"] == 2
    assert "helixis_lease_balances" not in result.failed


def test_everyone_paid_up_clears_the_snapshot():
    """A complete read where nobody owes is a fact: the snapshot empties."""
    pool, result = _replace([_lease(1, _owes(0))])
    (delete,) = _calls(pool, "DELETE FROM helixis_lease_balances")
    assert delete.args[2] == []
    assert _calls(pool, "INSERT") == []
    assert result.rows_by_table["helixis_lease_balances"] == 0


def test_one_unreadable_balance_writes_nothing_and_is_stale():
    """Leaving that tenant out would read as "current"."""
    pool, result = _replace([_lease(1, _owes(1450)), _lease(2, None)])
    assert pool._conn.execute.await_args_list == []
    assert "1 of 2" in result.failed["helixis_lease_balances"]
    assert "helixis_lease_balances" not in result.rows_by_table
    assert "helixis_lease_balances" not in result.pruned


def test_an_incomplete_export_writes_nothing_and_is_stale():
    pool, result = _replace([_lease(1, _owes(1450))], gaps=["the export's end is a guess"])
    assert pool._conn.execute.await_args_list == []
    assert "guess" in result.failed["helixis_lease_balances"]


# ── wired into the lease stage ────────────────────────────────────────────────


def _app() -> Settings:
    app = Settings()
    return app.model_copy(update={"rentvine": app.rentvine.model_copy(update={"enabled": True})})


def _sync(handler: Any) -> tuple[Any, sync.SyncResult]:
    pool = _pool()
    result = asyncio.run(sync.sync_company(pool, CO, settings=_app(), client=_client(handler)))
    return pool, result


def test_balances_ride_on_the_export_and_never_reach_the_lease_row(monkeypatch):
    monkeypatch.setattr(sync, "_today", lambda: TODAY)
    leases = export_leases(2)

    def handler(request: httpx.Request) -> httpx.Response:
        answered = lease_route(request, leases)
        if answered is not None:
            return answered
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    pool, result = _sync(handler)
    assert "helixis_lease_balances" not in result.failed, result.failed
    assert result.rows_by_table["helixis_lease_balances"] == 0  # EXPORT_BALANCES owe nothing
    for call in _calls(pool, "INSERT INTO helixis_leases"):
        raw = call.args[-1]
        assert "unpaidTotalAmount" not in raw and "unpaidCharges" not in raw


def test_an_export_without_balances_keeps_the_leases_and_marks_balances_stale():
    """The lease read is live and proven: missing balances cost the balances,
    never the leases."""
    leases = export_leases(2)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/leases/export"):
            rows = [{"lease": lease} for lease in leases]  # no balances beside
            head = {"pagination-total-items": "2", "pagination-total-pages": "1"}
            return httpx.Response(200, json=rows, headers=head)
        answered = lease_route(request, leases)
        if answered is not None:
            return answered
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    pool, result = _sync(handler)
    assert result.rows_by_table["helixis_leases"] == 2
    assert "helixis_leases" not in result.failed
    assert "2 of 2" in result.failed["helixis_lease_balances"]
    assert _calls(pool, "DELETE FROM helixis_lease_balances") == []


def test_a_failed_export_marks_balances_stale_too():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/leases/export"):
            return httpx.Response(503, json={})
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    _pool_, result = _sync(handler)
    assert "helixis_lease_balances" in result.failed


def test_balances_are_stamped_under_the_buildium_stage_name():
    assert sync.SYNC_STATE_STAGE["helixis_lease_balances"] == "balances"


def test_aging_is_counted_in_the_company_calendar(monkeypatch):
    """Not the server's UTC date (tests/unit/test_company_clock.py's rule)."""
    seen: list[Any] = []
    monkeypatch.setattr(sync, "company_timezone", AsyncMock(return_value="America/Denver"))

    def _today_in(tz: Any, **_: Any) -> date:
        seen.append(tz)
        return TODAY

    monkeypatch.setattr(sync, "today_in", _today_in)
    charges = [{"amount": 300, "amountPaid": 0, "datePosted": "2026-07-01"}]  # 92 days
    pool, _ = _replace([_lease(1, _owes(300), charges)])
    assert seen == ["America/Denver"]
    (insert,) = _calls(pool, "INSERT INTO helixis_lease_balances")
    assert insert.args[11] == Decimal("300")  # balance_over_90
