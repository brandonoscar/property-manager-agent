"""A record deleted (or deactivated) in Rentvine leaves the mirror.

The ``SOURCES`` tables (properties, tenants, owners, vendors, work orders, the
chart of accounts) were upsert-only, so a vendor removed in Rentvine went on
being offered for work and a property taken off the books went on counting
toward occupancy for as long as the connection lasted (gotcha 182). Buildium
evicts through its ``*.Deleted`` webhooks; Rentvine's are refused until their
signature scheme is known, so the poll's complete read is the only delete path.

Every test drives the real ``sync.sync_company`` over an HTTP mock, with a pool
that refuses what asyncpg refuses and records the statement order. All rows
are synthetic.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from helixis.rentvine import sync
from helixis.settings import Settings

from ._pg_bind_contract import reject_unbindable, reject_untyped_mirror_bind
from .test_rentvine_sync import CONTACT, PROPERTY, _client, export_leases, lease_route, wire

CO = "c"
KEEP, GONE_BY_NOW = 990000201, 990000202


def _app() -> Settings:
    app = Settings()
    return app.model_copy(update={"rentvine": app.rentvine.model_copy(update={"enabled": True})})


def _pool() -> tuple[Any, list[str]]:
    """A pool whose ``execute`` enforces the bind contract and whose every
    statement, transaction start and transaction end lands in one ordered log."""
    log: list[str] = []
    conn = MagicMock()

    def _execute(sql: str, *args: Any) -> str:
        reject_unbindable(sql, args)
        reject_untyped_mirror_bind(sql, args)
        log.append(sql)
        return "DELETE 0" if sql.startswith("DELETE") else "INSERT 0 1"

    def _begin(*_: Any) -> None:
        log.append("BEGIN")

    def _commit(*_: Any) -> bool:
        log.append("COMMIT")
        return False

    conn.execute = AsyncMock(side_effect=_execute)
    conn.fetch = AsyncMock(return_value=[])
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(side_effect=_begin)
    tx.__aexit__ = AsyncMock(side_effect=_commit)
    conn.transaction = MagicMock(return_value=tx)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    pool._conn = conn
    return pool, log


def _vendor(contact_id: Any) -> dict[str, Any]:
    return {**CONTACT, "contactID": contact_id}


def _complete(rows: list[dict[str, Any]]) -> dict[str, str]:
    """The headers by which Rentvine names the end of a collection."""
    return {"pagination-total-pages": "1", "pagination-total-items": str(len(rows))}


def _handler(
    vendors: list[dict[str, Any]],
    *,
    vendor_headers: Callable[[list[dict[str, Any]]], dict[str, str]] = _complete,
    vendor_status: int = 200,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/vendors"):
            if vendor_status != 200:
                return httpx.Response(vendor_status, json={"error": "forbidden"})
            body = wire("GET /vendors", *vendors)
            return httpx.Response(200, json=body, headers=vendor_headers(vendors))
        if path.endswith("/properties"):
            body = wire("GET /properties", PROPERTY)
            return httpx.Response(200, json=body, headers=_complete([PROPERTY]))
        answered = lease_route(request, export_leases(1))
        if answered is not None:
            return answered
        return httpx.Response(200, json=[], headers={"pagination-total-pages": "1"})

    return handler


async def _sync(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[sync.SyncResult, list[str], Any]:
    pool, log = _pool()
    result = await sync.sync_company(pool, CO, settings=_app(), client=_client(handler))
    return result, log, pool


def _deletes(pool: Any, table: str) -> list[Any]:
    calls = pool._conn.execute.await_args_list
    return [c for c in calls if c.args[0].startswith(f"DELETE FROM {table} ")]


# ── what a complete read prunes ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_complete_read_prunes_what_it_no_longer_returns() -> None:
    result, _log, pool = await _sync(_handler([_vendor(KEEP)]))
    (prune,) = _deletes(pool, "helixis_vendors")
    sql, company, kept, window = prune.args
    # Only this company's Rentvine rows (rule 11; gotcha 112), only the ones the
    # read did not return, and never one touched since the read began.
    assert "WHERE company_id = $1::uuid AND pms = 'rentvine'" in sql
    assert "NOT (buildium_id = ANY($2::bigint[]))" in sql
    assert "updated_at < now() - make_interval(secs => $3::double precision)" in sql
    assert (company, kept) == (CO, [KEEP])
    assert window >= sync.PRUNE_TOUCH_MARGIN_S
    # Said in sync_cycle's prune log line, even at zero.
    assert result.pruned["helixis_vendors"] == 0
    assert "helixis_vendors" not in result.failed


@pytest.mark.asyncio
async def test_every_sources_table_is_pruned_from_its_own_complete_read() -> None:
    result, _log, pool = await _sync(_handler([_vendor(KEEP)]))
    for table, _path in sync.SOURCES:
        assert _deletes(pool, table), f"{table}: a complete read pruned nothing"
        assert table in result.pruned


@pytest.mark.asyncio
async def test_the_window_covers_the_whole_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row another writer put in while the read ran (the web Re-sync beside
    the worker's poll, or a webhook refresh) is not in the list and still exists
    in Rentvine. The window is the read's own length plus the margin."""
    ticks: Iterator[float] = iter(float(n * 300) for n in range(1000))
    monkeypatch.setattr(sync, "_monotonic", lambda: next(ticks))
    _result, _log, pool = await _sync(_handler([_vendor(KEEP)]))
    (prune,) = _deletes(pool, "helixis_vendors")
    assert prune.args[3] == pytest.approx(300 + sync.PRUNE_TOUCH_MARGIN_S)


@pytest.mark.asyncio
async def test_a_property_that_is_gone_takes_its_units_in_the_same_transaction() -> None:
    result, log, pool = await _sync(_handler([_vendor(KEEP)]))
    (units,) = _deletes(pool, "helixis_units")
    sql, company, kept_properties, _window = units.args
    assert "WHERE company_id = $1::uuid AND pms = 'rentvine'" in sql
    assert "NOT (property_id = ANY($2::bigint[]))" in sql
    assert "updated_at < now() - make_interval" in sql
    assert (company, kept_properties) == (CO, [PROPERTY["propertyID"]])
    assert result.pruned["helixis_units"] == 0
    at = log.index(units.args[0])
    begin = max(i for i in range(at) if log[i] == "BEGIN")
    assert any(s.startswith("DELETE FROM helixis_properties ") for s in log[begin:at])
    assert "COMMIT" not in log[begin:at]


# ── what stops a prune ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_read_whose_end_was_guessed_prunes_nothing() -> None:
    result, _log, pool = await _sync(_handler([_vendor(KEEP)], vendor_headers=lambda _r: {}))
    assert result.rows_by_table["helixis_vendors"] == 1
    assert _deletes(pool, "helixis_vendors") == []
    assert "helixis_vendors" not in result.pruned


@pytest.mark.asyncio
async def test_a_failed_read_prunes_nothing() -> None:
    result, _log, pool = await _sync(_handler([], vendor_status=403))
    assert "helixis_vendors" in result.failed
    assert _deletes(pool, "helixis_vendors") == []
    assert "helixis_vendors" not in result.pruned


@pytest.mark.asyncio
async def test_a_row_with_no_usable_id_stops_the_prune(caplog: pytest.LogCaptureFixture) -> None:
    """That record exists in Rentvine and is only missing from the kept ids, so
    a prune would delete a live record's mirror row."""
    caplog.set_level(logging.WARNING, logger="helixis.rentvine.sync")
    result, _log, pool = await _sync(_handler([_vendor(KEEP), _vendor("not-a-number")]))
    assert result.skipped_no_id["helixis_vendors"] == 1
    assert _deletes(pool, "helixis_vendors") == []
    assert "helixis_vendors" not in result.pruned
    assert any("helixis_vendors NOT pruned" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_an_id_read_twice_stops_the_prune(caplog: pytest.LogCaptureFixture) -> None:
    """A record added or removed mid-read moves the page boundaries: one row
    repeats and another is never seen. The count still reconciles."""
    caplog.set_level(logging.WARNING, logger="helixis.rentvine.sync")
    result, _log, pool = await _sync(_handler([_vendor(KEEP), _vendor(KEEP)]))
    assert _deletes(pool, "helixis_vendors") == []
    assert "helixis_vendors" not in result.pruned
    assert any("page boundary moved" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_stopped_properties_prune_keeps_every_unit() -> None:
    """No properties prune, no units prune: the cascade rides on the parent's."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/properties"):
            body = wire("GET /properties", PROPERTY, {**PROPERTY, "propertyID": "x"})
            return httpx.Response(200, json=body, headers=_complete([PROPERTY, PROPERTY]))
        return _handler([_vendor(KEEP)])(request)

    result, _log, pool = await _sync(handler)
    assert result.skipped_no_id["helixis_properties"] == 1
    assert _deletes(pool, "helixis_properties") == []
    assert _deletes(pool, "helixis_units") == []
