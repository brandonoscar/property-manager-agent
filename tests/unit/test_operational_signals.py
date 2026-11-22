"""Operational memory — deterministic entity signals (#534 Stage B).

The DB is mocked (raw asyncpg conn), so these lock the plumbing without a
Postgres: the aging-buckets read → AgingSummary mapping, the derive→store→audit
path, the auto-resolve (clear) when a lease catches up, the label-prefixed
read-back for injection, company-scoping, and the best-effort "never break the
pipeline" posture. The grounded aging→signal math is covered in
test_entity_memory.py.
"""

from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.webhooks import operational_signals as ops

COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
LEASE_ID = 24195


class _Conn:
    def __init__(
        self,
        *,
        fetchrow: list[Any] | None = None,
        fetch: list[Any] | None = None,
        execute: list[Any] | None = None,
    ) -> None:
        self.fetchrow = AsyncMock(side_effect=fetchrow or [])
        self.fetch = AsyncMock(side_effect=fetch or [])
        self.execute = AsyncMock(side_effect=execute or [])


def _pool(conn: _Conn) -> MagicMock:
    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


def _balance_row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "total_balance": 720.0,
        "balance_31_60": 100.0,
        "balance_61_90": 170.0,
        "balance_over_90": 350.0,
        "snapshot_at": dt.datetime(2026, 7, 11, tzinfo=dt.UTC),
        "unit_id": 30667,
        "unit_number": "2B",
    }
    base.update(over)
    return base


# ── read_lease_delinquency ───────────────────────────────────────


@pytest.mark.asyncio
async def test_read_lease_delinquency_maps_aging_and_label() -> None:
    conn = _Conn(fetchrow=[_balance_row()])
    out = await ops.read_lease_delinquency(conn, company_id=COMPANY_ID, lease_id=LEASE_ID)
    assert out is not None
    assert out.label == "Unit 2B"
    assert out.aging is not None
    assert out.aging.overdue_30_plus == 620.0
    assert out.aging.as_of == dt.date(2026, 7, 11)


@pytest.mark.asyncio
async def test_read_lease_delinquency_no_row_is_caught_up() -> None:
    conn = _Conn(fetchrow=[None])
    out = await ops.read_lease_delinquency(conn, company_id=COMPANY_ID, lease_id=LEASE_ID)
    assert out is not None
    assert out.aging is None  # no balance row → caught up
    assert out.label == f"Lease {LEASE_ID}"  # falls back to lease id when no unit


@pytest.mark.asyncio
async def test_read_lease_delinquency_swallows_query_error() -> None:
    conn = _Conn()
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("db down"))
    out = await ops.read_lease_delinquency(conn, company_id=COMPANY_ID, lease_id=LEASE_ID)
    assert out is None


# ── derive_and_store_lease_signals ───────────────────────────────


@pytest.mark.asyncio
async def test_derive_and_store_flags_chronic_and_audits() -> None:
    # fetchrow #1 = balance read; fetchrow #2 = store INSERT RETURNING id.
    conn = _Conn(fetchrow=[_balance_row(), {"id": "sig-1"}])
    with patch.object(ops, "write_operational_audit", new=AsyncMock()) as audit:
        fact = await ops.derive_and_store_lease_signals(
            _pool(conn), company_id=COMPANY_ID, lease_id=LEASE_ID
        )
    assert fact is not None and "chronic late-payer" in fact
    # Stored with the right scoping + severity (critical → 90+ overdue).
    insert_args = conn.fetchrow.await_args_list[1].args
    assert COMPANY_ID in insert_args
    assert "lease" in insert_args and str(LEASE_ID) in insert_args
    assert "critical" in insert_args
    # Audited as an operational write, carrying the entity + memory id.
    audit.assert_awaited_once()
    kw = audit.await_args.kwargs
    assert kw["company_id"] == COMPANY_ID
    assert kw["memory_id"] == "sig-1"
    assert kw["metadata"]["op"] == "store"
    assert kw["metadata"]["entity_id"] == str(LEASE_ID)


@pytest.mark.asyncio
async def test_derive_and_store_auto_resolves_when_caught_up() -> None:
    # Balance present but nothing 30+ overdue → fact None → clear + audit.
    conn = _Conn(
        fetchrow=[_balance_row(balance_31_60=0.0, balance_61_90=0.0, balance_over_90=0.0)],
        execute=["DELETE 1"],
    )
    with patch.object(ops, "write_operational_audit", new=AsyncMock()) as audit:
        fact = await ops.derive_and_store_lease_signals(
            _pool(conn), company_id=COMPANY_ID, lease_id=LEASE_ID
        )
    assert fact is None
    conn.execute.assert_awaited_once()  # DELETE issued
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["metadata"]["op"] == "clear"


@pytest.mark.asyncio
async def test_derive_and_store_no_audit_when_nothing_to_clear() -> None:
    # No balance row + nothing stored → DELETE affects 0 rows → no audit.
    conn = _Conn(fetchrow=[None], execute=["DELETE 0"])
    with patch.object(ops, "write_operational_audit", new=AsyncMock()) as audit:
        fact = await ops.derive_and_store_lease_signals(
            _pool(conn), company_id=COMPANY_ID, lease_id=LEASE_ID
        )
    assert fact is None
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_derive_and_store_never_raises() -> None:
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=RuntimeError("pool boom"))
    fact = await ops.derive_and_store_lease_signals(pool, company_id=COMPANY_ID, lease_id=LEASE_ID)
    assert fact is None


# ── get_lease_entity_signals (injection read) ────────────────────


@pytest.mark.asyncio
async def test_get_lease_entity_signals_prefixes_label() -> None:
    conn = _Conn(
        fetch=[
            [
                {
                    "entity_label": "Unit 2B",
                    "signal": "chronic late-payer — $620.00 is 30+ days overdue",
                },
                {"entity_label": "", "signal": "behind on rent"},
                {"entity_label": "Unit 2B", "signal": ""},  # blank signal dropped
            ]
        ]
    )
    out = await ops.get_lease_entity_signals(conn, company_id=COMPANY_ID, lease_id=LEASE_ID)
    assert out == [
        "Unit 2B: chronic late-payer — $620.00 is 30+ days overdue",
        "behind on rent",
    ]


@pytest.mark.asyncio
async def test_get_lease_entity_signals_swallows_error() -> None:
    conn = _Conn()
    conn.fetch = AsyncMock(side_effect=RuntimeError("db down"))
    out = await ops.get_lease_entity_signals(conn, company_id=COMPANY_ID, lease_id=LEASE_ID)
    assert out == []
