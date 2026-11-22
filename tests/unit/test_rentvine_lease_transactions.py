"""Rentvine rent payment history: ``getLease?includes=transactions`` ->
``helixis_lease_transactions``.

The money comes from the lease-scoped ``getLease`` read the rent pass already
makes; the transactions search only picks which leases to re-read
(``sync.TRANSACTIONS_SEARCH_PATH``). Rows are signed the Buildium way, by
their effect on the balance (``buildium/money.PAYMENTS_ARE_NEGATIVE``), because
every reader of this table negates Payment rows on that understanding. All
rows here are synthetic; the live item shape and sign are NOT VERIFIED
(Phase 8 reports both).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.db.repositories.mirror_sync_state import StageSync
from helixis.rentvine import mapper, sync
from helixis.rentvine.client import RentvineAPIError
from helixis.rentvine.transport import Failure

from ._pg_bind_contract import reject_unbindable, reject_untyped_mirror_bind

CO = "6a456191-0000-4000-8000-000000000001"
LEASE = 990000601


def _txn(txn_id: int, kind: int, amount: Any, **over: Any) -> dict[str, Any]:
    return {
        "transactionID": txn_id,
        "transactionTypeID": kind,
        "amount": amount,
        "isVoided": 0,
        "datePosted": "2026-10-01",
        **over,
    }


def _one(item: dict[str, Any]) -> dict[str, Any]:
    rows = mapper.lease_transactions([item], LEASE)
    assert rows is not None and len(rows) == 1
    return rows[0]


# ── the mapper ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "label", "signed"),
    [
        (1, "Charge", Decimal("800.00")),
        (2, "Payment", Decimal("-800.00")),
        (3, "Credit", Decimal("-800.00")),
        # A payment RETURN is money back out: a positive Payment row, which
        # payments_received_sql subtracts from "collected".
        (4, "Payment", Decimal("800.00")),
    ],
)
def test_lease_money_is_signed_by_its_effect_on_the_balance(kind, label, signed):
    row = _one(_txn(11, kind, "800.00"))
    assert row["transaction_type"] == label
    assert row["total_amount"] == signed


@pytest.mark.parametrize("sent", ["800.00", "-800.00"])
def test_the_type_decides_the_sign_whichever_convention_rentvine_sends(sent):
    """Unsigned magnitudes or balance-signed amounts land the same way."""
    assert _one(_txn(11, 2, sent))["total_amount"] == Decimal("-800.00")
    assert _one(_txn(12, 1, sent))["total_amount"] == Decimal("800.00")


@pytest.mark.parametrize("kind", [19, 20, 21])
def test_a_lease_type_with_no_stated_direction_keeps_no_amount(kind):
    """A guessed sign is a wrong number; NULL is an honest unknown (rule 14)."""
    row = _one(_txn(11, kind, "300.00"))
    assert row["total_amount"] is None
    assert row["transaction_type"] == mapper.LEASE_TRANSACTION_UNSIGNED[kind]


def test_money_that_is_not_lease_money_is_left_out():
    """A bill charge (7) on a lease's list is not rent; neither is an unknown type."""
    rows = mapper.lease_transactions([_txn(11, 7, "50.00"), _txn(12, 99, "1.00")], LEASE)
    assert rows == []


def test_voided_and_idless_rows_are_left_out():
    rows = mapper.lease_transactions(
        [_txn(11, 2, "1.00", isVoided=1), _txn(None, 2, "1.00"), _txn(13, 2, "5.00")], LEASE
    )
    assert rows is not None
    assert [r["buildium_id"] for r in rows] == [13]


def test_an_unreadable_amount_is_unknown_not_zero():
    assert _one(_txn(11, 2, "n/a"))["total_amount"] is None


def test_a_wrapped_item_is_read_like_a_bare_one():
    """The spec types each item only as ``object``; the same response wraps
    ``chargesForPayment`` items as ``{transaction}``."""
    bare = _one(_txn(11, 2, "800.00"))
    wrapped = _one({"transaction": _txn(11, 2, "800.00")})
    assert {k: v for k, v in wrapped.items() if k != "raw"} == {
        k: v for k, v in bare.items() if k != "raw"
    }


def test_the_date_is_midnight_utc_on_the_posted_day_and_the_lease_is_the_callers():
    row = _one(_txn(11, 2, "800.00", datePosted="2026-10-01"))
    assert row["transaction_date"] == datetime(2026, 10, 1, tzinfo=UTC)
    assert row["lease_id"] == LEASE and row["buildium_id"] == 11
    assert _one(_txn(11, 2, "800.00", datePosted=None))["transaction_date"] is None


@pytest.mark.parametrize("sent", [None, "x", {"transactions": []}])
def test_a_missing_list_is_unknown_not_an_empty_history(sent):
    assert mapper.lease_transactions(sent, LEASE) is None
    assert mapper.lease_transactions([], LEASE) == []


# ── which leases a pass reads ─────────────────────────────────────────────────


def _export(lease_id: int, *, unit: int | None = None, prop: int | None = None) -> dict[str, Any]:
    return {
        "leaseID": lease_id,
        "unitID": unit,
        "propertyID": prop,
        "dateTimeModified": "m1",
        "primaryLeaseStatusID": 2,
    }


def test_a_lease_whose_history_was_never_read_goes_first():
    """Rent read before history was synced: its rent mark is current, but the
    history is missing, so it is read ahead of every re-read."""
    leases = [_export(1), _export(2), _export(3)]
    marks = {1: "m1", 2: "m1", 3: "m1"}
    got = sync._pick_rent_reads(leases, marks, date(2026, 10, 2), history_read={1, 3})
    assert got[0] == 2


def test_a_lease_with_new_money_is_read_before_the_daily_re_read():
    """Re-pointed 2026-10-02: the rotation became a daily re-read."""
    today = date(2026, 10, 2)
    ids = range(1, 8)
    leases = [_export(i) for i in ids]
    marks = {i: "m1" for i in ids}
    every = set(ids)
    # Lease 7 is due (read yesterday); every other lease was read today.
    read_on = {i: "2026-10-02" for i in ids} | {7: "2026-10-01"}
    assert 3 not in sync._pick_rent_reads(leases, marks, today, history_read=every, read_on=read_on)
    got = sync._pick_rent_reads(
        leases, marks, today, history_read=every, touched={3}, read_on=read_on
    )
    assert got.index(3) < got.index(7)


# ── the change search ─────────────────────────────────────────────────────────


class _Search:
    def __init__(self, rows: list[dict[str, Any]] | Exception) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    async def iter_all(self, template: str, **kw: Any) -> Any:
        self.calls.append({"template": template, **kw})
        if isinstance(self.rows, Exception):
            raise self.rows
        return type("C", (), {"rows": self.rows, "stop_was_documented": True})()


def _changed(rows: Any, *, stamped: bool = True) -> tuple[set[int], str | None, _Search]:
    search = _Search(rows)
    leases = [_export(1, unit=77, prop=10), _export(2, unit=78, prop=10), _export(3, prop=20)]
    stamp = StageSync(synced_at=datetime(2026, 10, 2, 6, tzinfo=UTC), row_count=3)
    with patch.object(sync, "get_stage_sync", AsyncMock(return_value=stamp if stamped else None)):
        touched, gap = asyncio.run(
            sync._leases_with_new_money(MagicMock(), search, CO, leases)  # type: ignore[arg-type]
        )
    return touched, gap, search


def _moved(kind: int = 2, *, unit: int | None = None, prop: int | None = None) -> dict[str, Any]:
    return {
        "transactionID": 1,
        "transactionTypeID": kind,
        "dateTimeModified": "2026-10-02 07:00:00",
        "unit": None if unit is None else {"unitID": unit},
        "property": None if prop is None else {"propertyID": prop},
    }


def test_before_the_first_stamp_nothing_is_searched():
    touched, gap, search = _changed([], stamped=False)
    assert (touched, gap, search.calls) == (set(), None, [])


def test_the_search_starts_a_day_before_the_last_stamp_and_asks_for_lease_money():
    _touched, _gap, search = _changed([])
    (call,) = search.calls
    assert call["template"] == sync.TRANSACTIONS_SEARCH_PATH
    assert call["query"]["dateTimeModifiedMin"] == "2026-10-01 00:00:00"
    assert set(call["query"]["transactionTypeIDs[]"]) == {1, 2, 3, 4, 19, 20, 21}
    assert call["max_pages"] == sync.TXN_CHANGE_MAX_PAGES


def test_money_on_a_unit_marks_that_units_leases_and_no_others():
    touched, gap, _ = _changed([_moved(unit=77, prop=10)])
    assert (touched, gap) == ({1}, None)


def test_money_with_no_unit_falls_back_to_the_property():
    touched, gap, _ = _changed([_moved(prop=20)])
    assert (touched, gap) == ({3}, None)


def test_money_that_names_nothing_we_hold_makes_the_list_unknown():
    touched, gap, _ = _changed([_moved(prop=999)])
    assert gap is not None and "1 changed" in gap


def test_non_lease_money_is_ignored_even_if_the_type_filter_was():
    touched, gap, _ = _changed([_moved(kind=7, prop=999)])
    assert (touched, gap) == (set(), None)


def test_a_row_older_than_the_window_means_the_filter_was_ignored():
    old = {**_moved(unit=77), "dateTimeModified": "2026-09-01 00:00:00"}
    touched, gap, _ = _changed([old])
    assert touched == set() and gap is not None and "not honoured" in gap


def test_a_failed_search_makes_the_list_unknown():
    touched, gap, _ = _changed(RentvineAPIError(429, Failure.UNAVAILABLE, True, "slow down"))
    assert touched == set() and gap is not None and "failed" in gap


# ── the write ─────────────────────────────────────────────────────────────────


def _pool() -> Any:
    conn = MagicMock()

    def _enforce(sql: str, *args: Any) -> str:
        reject_unbindable(sql, args)
        reject_untyped_mirror_bind(sql, args)
        return "INSERT 0 1"

    conn.execute = AsyncMock(side_effect=_enforce)
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


def test_each_read_lease_has_its_history_replaced_and_an_unknown_one_is_left_alone():
    pool = _pool()
    rows = mapper.lease_transactions([_txn(11, 2, "800.00"), _txn(12, 1, "800.00")], LEASE)
    written = asyncio.run(sync._write_lease_transactions(pool, CO, {LEASE: rows, LEASE + 1: None}))
    assert written == 2
    calls = pool._conn.execute.await_args_list
    deletes = [c for c in calls if c.args[0].startswith("DELETE")]
    # Only the lease that came back with a list, scoped to company and PMS.
    (delete,) = deletes
    assert "company_id = $1::uuid" in delete.args[0] and "pms = 'rentvine'" in delete.args[0]
    assert delete.args[1:] == (CO, LEASE)
    inserts = [c for c in calls if "INSERT INTO helixis_lease_transactions" in c.args[0]]
    assert len(inserts) == 2
    assert all("ON CONFLICT (company_id, buildium_id)" in c.args[0] for c in inserts)
    first = inserts[0].args
    assert first[1:6] == (11, 0, CO, LEASE, "rentvine")
    assert first[7] == "Payment" and first[8] == Decimal("-800.00")


def test_the_table_is_fed_and_stamped_under_buildiums_stage_name():
    from helixis.db.pms_coverage import FED_BY, RENTVINE

    assert "helixis_lease_transactions" in FED_BY[RENTVINE]
    assert sync.SYNC_STATE_STAGE["helixis_lease_transactions"] == "transactions"
    assert "transactions" in sync.REFERENCE_STAGES


# ── the lease pass ────────────────────────────────────────────────────────────


def _lease_pass(handler: Any, pool: Any) -> sync.SyncResult:
    from .test_rentvine_complete_leases import _app
    from .test_rentvine_sync import _client

    return asyncio.run(sync.sync_company(pool, "c", settings=_app(), client=_client(handler)))


def _sql(pool: Any, needle: str) -> list[Any]:
    return [c for c in pool._conn.execute.await_args_list if needle in c.args[0]]


def test_a_clean_pass_writes_every_leases_history_and_claims_it_complete():
    from .test_rentvine_complete_leases import _handler, _pool
    from .test_rentvine_sync import export_leases

    handler, calls = _handler(export_leases(3))
    pool = _pool()
    result = _lease_pass(handler, pool)
    # The one rent read per lease also asks for the history; no extra call.
    reads = [c for c in calls if c.split("?")[0].rsplit("/", 1)[-1].isdigit()]
    assert len(reads) == 3
    assert all("includes=tenants%2Ctransactions" in c for c in reads), reads
    assert "helixis_lease_transactions" not in result.failed
    assert "helixis_lease_transactions" in result.confirmed_complete
    assert result.rows_by_table["helixis_lease_transactions"] == 3  # one payment each
    # Each lease is marked as read, so the next pass does not re-read it first.
    lease_writes = _sql(pool, "INSERT INTO helixis_leases")
    assert lease_writes and all(
        any(isinstance(a, str) and sync.TXN_READ_MARKER in a for a in c.args) for c in lease_writes
    )
    # The prune removes the history of a lease Rentvine no longer lists.
    (prune,) = [
        c for c in _sql(pool, "DELETE FROM helixis_lease_transactions") if "NOT (" in c.args[0]
    ]
    assert "pms = 'rentvine'" in prune.args[0] and sorted(prune.args[2]) == [1, 2, 3]
    assert result.pruned["helixis_lease_transactions"] == 1  # what the DELETE reported


def test_a_lease_answered_without_its_history_leaves_the_table_stale():
    import httpx

    from .test_rentvine_complete_leases import _handler, _pool
    from .test_rentvine_sync import export_leases

    def lease(request: httpx.Request) -> httpx.Response:
        lease_id = int(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"lease": {"leaseID": lease_id, "rentAmount": 1800}})

    handler, _ = _handler(export_leases(2), lease=lease)
    pool = _pool()
    result = _lease_pass(handler, pool)
    reason = result.failed["helixis_lease_transactions"]
    assert "2 lease read(s) came back without a transactions list" in reason
    assert "helixis_lease_transactions" not in result.confirmed_complete
    # Unknown is left alone: no lease's stored history was deleted.
    assert not [
        c
        for c in _sql(pool, "DELETE FROM helixis_lease_transactions")
        if "$2" in c.args[0] and "NOT (" not in c.args[0]
    ]
    # And the rent read itself still counts (the leases stage is unaffected).
    assert "helixis_leases" not in result.failed


def test_leases_past_the_cap_keep_the_table_stale(monkeypatch: pytest.MonkeyPatch):
    from .test_rentvine_complete_leases import _handler, _pool
    from .test_rentvine_sync import export_leases

    monkeypatch.setattr(sync, "LEASE_RENT_READS_PER_SYNC", 2)
    handler, _ = _handler(export_leases(3))
    result = _lease_pass(handler, _pool())
    reason = result.failed["helixis_lease_transactions"]
    assert "1 lease(s) whose transactions have never been read" in reason


def test_a_failed_export_marks_the_history_stale_too():
    import httpx

    from .test_rentvine_complete_leases import _handler, _pool

    base, _ = _handler([])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/leases/export"):
            return httpx.Response(403, json={"error": "forbidden"})
        return base(request)

    result = _lease_pass(handler, _pool())
    assert result.failed["helixis_lease_transactions"] == "skipped: the lease export failed first"


def test_a_failed_change_search_is_logged_and_does_not_stale_the_history(caplog):
    """Re-pointed 2026-10-02. The search was the only way to see a payment on an
    unchanged lease until it was MEASURED that a payment moves the lease's own
    ``dateTimeModified`` (lease 1 moved, control leases 3 and 6 did not). It is a
    backup now: a failure is said in the log and the history stays claimable,
    so a server that ignores its filter cannot hold the rent readers back."""
    import httpx

    from .test_rentvine_complete_leases import _handler, _pool
    from .test_rentvine_sync import export_leases

    base, _ = _handler(export_leases(2))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(sync.TRANSACTIONS_SEARCH_PATH):
            return httpx.Response(403, json={"error": "forbidden"})
        return base(request)

    stamp = StageSync(synced_at=datetime(2026, 10, 2, tzinfo=UTC), row_count=2)
    with patch.object(sync, "get_stage_sync", AsyncMock(return_value=stamp)):
        result = _lease_pass(handler, _pool())
    assert "helixis_lease_transactions" not in result.failed
    assert "helixis_lease_transactions" in result.confirmed_complete
    assert any("change search skipped" in r.getMessage() for r in caplog.records)
