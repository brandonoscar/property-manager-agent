"""New state-rule detectors #6-#11 over the mirror.

Each detector is exercised with a representative mocked row to pin its
finding shape (rule_key / fingerprint / severity / entity / evidence). The
detectors ship dark (default_enabled=False) so they don't run in the
default cycle — verified here too.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.detectors import mirror_state_rules as m
from helixis.detectors.registry import enabled_state_rule_detectors, state_rule_detectors

COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"


def _conn(rows: list[dict[str, Any]]) -> MagicMock:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows)
    return conn


# ── all six are registered but dark by default ───────────────────────


def test_new_detectors_registered_but_dark() -> None:
    ids = {d.detector_id for d in state_rule_detectors()}
    six = {
        "active_lease_no_rent",
        "invoice_no_work_order",
        "lease_credit_balance",
        "utilities_owner_paid_occupied",
        "invoice_above_norm",
        "same_day_trip_charges",
    }
    assert six <= ids  # all registered
    # None run in the default cycle (dark); only the 2 live reconciliation rules do.
    default_ids = {d.detector_id for d in enabled_state_rule_detectors()}
    assert six.isdisjoint(default_ids)
    # rent_not_collected went live 2026-08-21 replacing the retired
    # rent_missing reminder producer; the six mirror rules stay dark.
    assert default_ids == {
        "rent_not_collected",
        "rent_under_charged",
        "duplicate_vendor_bill",
    }
    # An explicit allowlist opts one in.
    optd = {
        d.detector_id for d in enabled_state_rule_detectors(frozenset({"lease_credit_balance"}))
    }
    assert "lease_credit_balance" in optd


# ── #6 active lease, no rent ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_active_lease_no_rent() -> None:
    conn = _conn([{"lease_id": 4001, "property_id": 77, "unit_id": 12, "unit_number": "4B"}])
    findings = await m.ActiveLeaseNoRentDetector().scan(conn, COMPANY_ID)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_key == "active_lease_no_rent"
    assert f.fingerprint == "lease:4001"
    assert f.entity_type == "lease" and f.entity_id == 4001
    assert "no rent" in f.title.lower()


def test_active_lease_no_rent_never_reads_an_unread_rent_as_zero() -> None:
    """NULL rent is a rent Occupella has not read (every Rent Manager lease:
    its rent lives on recurring charges), not $0. Reading it as $0 would tell
    a manager each of those leases bills nothing (rule 14). Checked on the SQL
    the scan sends, with its ``--`` comments stripped so the comment
    explaining the rule cannot pass."""
    import re

    sent = re.sub(r"--[^\n]*", "", m._ACTIVE_LEASE_NO_RENT_SQL)
    assert re.search(r"AND\s+l\.rent\s*=\s*0\b", sent)
    assert not re.search(r"COALESCE\s*\(\s*l\.rent", sent, re.IGNORECASE)
    assert not re.search(r"l\.rent\s+IS\s+NULL", sent, re.IGNORECASE)


# ── #7 unpaid maintenance invoice, no work order ─────────────────────


@pytest.mark.asyncio
async def test_invoice_no_work_order_reads_only_systems_whose_bills_carry_the_link() -> None:
    """A Rentvine payables row carries no workOrderID, so ``work_order_id IS
    NULL`` there is unknown, not absent; flagging it would accuse every
    Rentvine maintenance bill. Re-pointed 2026-10-05 (rule 19): the rule used
    to be ``pms <> 'rentvine'``, which let every new system in by default, and
    Rent Manager's bills are all NULL too. Now the scan sends the systems that
    carry the link, and the SQL reads only those. Checked on the SQL with its
    ``--`` comments stripped, and on the arguments the scan sends."""
    import re

    sent = re.sub(r"--[^\n]*", "", m._INVOICE_NO_WO_SQL)
    assert re.search(r"AND\s+b\.pms\s*=\s*ANY\(\$5::text\[\]\)", sent)
    conn = _conn([])
    await m.InvoiceNoWorkOrderDetector().scan(conn, COMPANY_ID)
    systems = conn.fetch.await_args.args[5]
    assert systems == ["buildium"]
    for unknown in ("rentvine", "rentmanager", "propertyware"):
        assert unknown not in systems, unknown


def test_a_system_that_declares_the_bill_link_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from helixis.pms import registry

    def _attr(key: str, module: str, attr: str) -> object:
        return key == "propertyware" and (module, attr) == ("mapper", "BILL_WORK_ORDER_LINK")

    monkeypatch.setattr(registry, "connector_attr", _attr)
    assert m.bill_link_systems() == ["buildium", "propertyware"]


@pytest.mark.asyncio
async def test_invoice_no_work_order() -> None:
    conn = _conn(
        [
            {
                "bill_id": 9100,
                "vendor_id": 55,
                "vendor_name": "Apex Plumbing",
                "amount": 640.0,
                "bill_date": date(2026, 7, 1),
            }
        ]
    )
    findings = await m.InvoiceNoWorkOrderDetector().scan(conn, COMPANY_ID)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_key == "invoice_no_work_order"
    assert f.fingerprint == "bill:9100"
    assert f.severity == "info"
    assert f.entity_type == "bill" and f.entity_id == 9100
    assert "Apex Plumbing" in f.title
    assert f.evidence["is_paid"] is False


# ── #8 lease credit balance ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_lease_credit_balance() -> None:
    conn = _conn(
        [
            {
                "lease_id": 4001,
                "property_id": 77,
                "unit_id": 12,
                "unit_number": "4B",
                "balance": -125.0,
            }
        ]
    )
    findings = await m.LeaseCreditBalanceDetector().scan(conn, COMPANY_ID)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_key == "lease_credit_balance"
    assert f.amount == 125.0  # credit magnitude
    assert f.evidence["total_balance"] == -125.0
    assert "credit" in f.title.lower()


# ── #9 utilities owner-paid on an occupied unit ──────────────────────


@pytest.mark.asyncio
async def test_utilities_owner_paid_occupied() -> None:
    conn = _conn(
        [
            {
                "unit_id": 12,
                "unit_number": "4B",
                "property_id": 77,
                "bill_count": 3,
                "utility_total": 410.0,
                "latest_bill": date(2026, 7, 20),
                "gl_names": ["Electric — Utilities", "Water/Sewer"],
            }
        ]
    )
    findings = await m.UtilitiesOwnerPaidOccupiedDetector().scan(conn, COMPANY_ID)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_key == "utilities_owner_paid_occupied"
    assert f.fingerprint == "unit:12"
    assert f.entity_type == "unit" and f.entity_id == 12
    assert f.amount == 410.0
    assert f.evidence["gl_accounts"] == ["Electric — Utilities", "Water/Sewer"]


# ── #10 invoice above the vendor norm ────────────────────────────────


@pytest.mark.asyncio
async def test_invoice_above_norm() -> None:
    conn = _conn(
        [
            {
                "bill_id": 9200,
                "vendor_id": 55,
                "vendor_name": "Apex Plumbing",
                "amount": 890.0,
                "bill_date": date(2026, 7, 10),
                "median": 310.0,
                "n": 8,
            }
        ]
    )
    findings = await m.InvoiceAboveNormDetector().scan(conn, COMPANY_ID)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_key == "invoice_above_norm"
    assert f.fingerprint == "bill:9200"
    assert f.amount == 580.0  # 890 - 310 excess
    assert f.evidence["multiple"] == 2.9
    assert f.evidence["sample_size"] == 8


# ── #11 same-day trip charges ────────────────────────────────────────


@pytest.mark.asyncio
async def test_same_day_trip_charges() -> None:
    conn = _conn(
        [
            {
                "vendor_id": 55,
                "vendor_name": "Apex Plumbing",
                "property_id": 77,
                "bill_date": date(2026, 7, 5),
                "bill_count": 2,
                "bill_ids": [9300, 9301],
                "total": 300.0,
            }
        ]
    )
    findings = await m.SameDayTripChargesDetector().scan(conn, COMPANY_ID)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_key == "same_day_trip_charges"
    assert f.fingerprint == "vendor:55:prop:77:date:2026-07-05"
    assert f.entity_type == "property" and f.entity_id == 77
    assert f.evidence["bill_ids"] == [9300, 9301]
