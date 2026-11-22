"""Bridge detectors (#3/#4/#5) + the on-demand compliance sweep (#12).

Pins each finding's shape from a representative mocked row, confirms the
bridges are registered but dark, and that the compliance sweep projects
matched leases into findings under its per-sweep rule_key.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.detectors import bridge_rules as b
from helixis.detectors import compliance_sweep as cs
from helixis.detectors.registry import enabled_state_rule_detectors, state_rule_detectors

COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"


def _conn(rows: list[dict[str, Any]]) -> MagicMock:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    conn.execute = AsyncMock()
    return conn


def _pool(conn: Any) -> MagicMock:
    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


# ── registered but dark ──────────────────────────────────────────────


def test_bridges_registered_and_only_the_consolidated_one_is_live() -> None:
    """Dark-by-default is the rule; ``rent_not_collected`` is the documented
    exception.

    It went live 2026-08-21 when the ``rent_missing`` reminder producer — which
    tested the identical condition and grouped per property, producing 41 of the
    Inbox's 67 open cards — was unregistered. Leaving this dark would have
    deleted the signal rather than improved it, so the two must move together:
    if a future change re-registers that producer, this assertion is where the
    double-alert gets caught.
    """
    ids = {d.detector_id for d in state_rule_detectors()}
    three = {"rent_not_collected", "chronic_late_resident", "promise_to_pay_broken"}
    assert three <= ids
    default_ids = {d.detector_id for d in enabled_state_rule_detectors()}
    assert "rent_not_collected" in default_ids
    assert {"chronic_late_resident", "promise_to_pay_broken"}.isdisjoint(default_ids)

    from helixis.reminders.producers import PRODUCERS

    assert "rent_missing" not in PRODUCERS, (
        "rent_missing duplicates the now-live rent_not_collected detector — "
        "re-registering it double-alerts the same condition."
    )


# ── #3 rent not collected ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rent_not_collected() -> None:
    conn = _conn(
        [{"lease_id": 4001, "property_id": 77, "unit_id": 12, "unit_number": "4B", "rent": 1000.0}]
    )
    findings = await b.RentNotCollectedDetector().scan(conn, COMPANY_ID)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_key == "rent_not_collected"
    assert f.fingerprint == "lease:4001"
    assert f.entity_type == "lease" and f.entity_id == 4001
    assert f.amount == 1000.0


# ── #4 chronic-late resident ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_chronic_late_resident_over_90_is_critical() -> None:
    conn = _conn(
        [
            {
                "lease_id": 4001,
                "property_id": 77,
                "unit_id": 12,
                "unit_number": "4B",
                "balance": 2400.0,
                "b61_90": 800.0,
                "b_over_90": 1600.0,
            }
        ]
    )
    findings = await b.ChronicLateResidentDetector().scan(conn, COMPANY_ID)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_key == "chronic_late_resident"
    assert f.severity == "critical"  # 90+ balance present
    assert f.amount == 2400.0
    assert f.evidence["balance_over_90"] == 1600.0


# ── #5 promise-to-pay came due ───────────────────────────────────────


@pytest.mark.asyncio
async def test_promise_to_pay_broken() -> None:
    conn = _conn([{"email": "josh@example.com", "promised_date": date(2026, 7, 15)}])
    findings = await b.PromiseToPayBrokenDetector().scan(conn, COMPANY_ID)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_key == "promise_to_pay_broken"
    assert f.fingerprint == "promise:josh@example.com:2026-07-15"
    assert f.entity_type == "contact" and f.entity_id is None
    assert f.evidence["email"] == "josh@example.com"


# ── #12 on-demand compliance sweep ───────────────────────────────────


@pytest.mark.asyncio
async def test_compliance_sweep_projects_leases_with_remediation() -> None:
    conn = _conn(
        [
            {
                "lease_id": 4001,
                "property_id": 77,
                "unit_id": 12,
                "unit_number": "4B",
                "rent": 1000.0,
                "lease_from_date": date(2025, 1, 1),
                "lease_to_date": date(2026, 12, 31),
            }
        ]
    )
    findings = await cs.sweep_leases(
        conn,
        COMPANY_ID,
        label="no_admin_fee_2026",
        remediation="Stop billing the admin fee and credit charges since 2026-07-01.",
        property_ids=[77],
    )
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_key == "compliance:no_admin_fee_2026"
    assert f.fingerprint == "lease:4001"
    assert f.entity_type == "lease" and f.entity_id == 4001
    assert "admin fee" in f.evidence["remediation"]
    assert f.evidence["filters"]["property_ids"] == [77]


@pytest.mark.asyncio
async def test_run_compliance_sweep_persists_under_sweep_rule_key() -> None:
    conn = _conn(
        [
            {
                "lease_id": 4001,
                "property_id": 77,
                "unit_id": 12,
                "unit_number": "4B",
                "rent": 1000.0,
                "lease_from_date": date(2025, 1, 1),
                "lease_to_date": date(2026, 12, 31),
            }
        ]
    )
    findings = await cs.run_compliance_sweep(
        _pool(conn),
        COMPANY_ID,
        label="no_admin_fee_2026",
        remediation="Stop billing the admin fee.",
    )
    assert len(findings) == 1
    # 1 upsert (the matched lease) + 1 stale-resolve for the sweep's rule_key.
    calls = conn.execute.await_args_list
    assert len(calls) == 2
    assert calls[0].args[2] == "compliance:no_admin_fee_2026"  # rule_key on the upsert
    assert "SET status = 'resolved'" in calls[1].args[0]
