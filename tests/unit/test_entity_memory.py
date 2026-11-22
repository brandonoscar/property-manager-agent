"""Tests for entity-anchored memory (#534, Stage A).

The write/read wrappers are exercised against a fake manager (an in-memory
stand-in implementing the small ``_ManagerLike`` slice) so no mem0 / DB is
touched; the fact derivers are pure and tested directly.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from helixis.memory.entity_memory import (
    ENTITY_ID_KEY,
    ENTITY_TYPE_KEY,
    SOURCE_KEY,
    SOURCE_OPERATIONAL,
    AgingSummary,
    BalanceSummary,
    EntityRef,
    LatePaymentSummary,
    delinquency_severity,
    derive_balance_fact,
    derive_delinquency_fact,
    derive_payment_behavior_fact,
    format_entity_memories,
    get_entity_memories,
    store_entity_fact,
)


class FakeManager:
    """Minimal in-memory MemoryManager stand-in. Records adds and serves them
    back through get_all with the same {memory, metadata} shape mem0 returns."""

    def __init__(self, *, available: bool = True) -> None:
        self._available = available
        self.rows: list[dict[str, Any]] = []
        self._n = 0

    @property
    def available(self) -> bool:
        return self._available

    async def add(
        self,
        content: str,
        company_id: str = "",
        *,
        stored_by: str = "",
        category: str = "",
        extra_metadata: dict[str, Any] | None = None,
    ) -> str | None:
        self._n += 1
        mid = f"mem-{self._n}"
        meta = {"stored_by": stored_by, "category": category, **(extra_metadata or {})}
        self.rows.append({"id": mid, "memory": content, "metadata": meta, "_company": company_id})
        return mid

    async def get_all(self, company_id: str = "") -> list[dict[str, Any]]:
        return [r for r in self.rows if r["_company"] == company_id]


_UNIT_4B = EntityRef("unit", "4610", "Unit 4B")


# ── write ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_entity_fact_tags_and_prefixes() -> None:
    mgr = FakeManager()
    mid = await store_entity_fact(
        mgr,
        company_id="co-1",
        entity=_UNIT_4B,
        fact="paid rent late 4 of the last 6 months",
        category="financial",
    )
    assert mid == "mem-1"
    row = mgr.rows[0]
    # Label prefixed so the fact reads on its own + surfaces on a unit search.
    assert row["memory"] == "Unit 4B: paid rent late 4 of the last 6 months"
    assert row["metadata"][ENTITY_TYPE_KEY] == "unit"
    assert row["metadata"][ENTITY_ID_KEY] == "4610"
    assert row["metadata"][SOURCE_KEY] == SOURCE_OPERATIONAL
    assert row["metadata"]["category"] == "financial"


@pytest.mark.asyncio
async def test_store_entity_fact_noops_when_blank_or_unavailable() -> None:
    mgr = FakeManager()
    assert await store_entity_fact(mgr, company_id="co-1", entity=_UNIT_4B, fact="   ") is None
    off = FakeManager(available=False)
    assert await store_entity_fact(off, company_id="co-1", entity=_UNIT_4B, fact="x") is None
    assert mgr.rows == [] and off.rows == []


@pytest.mark.asyncio
async def test_store_entity_fact_without_label_stores_raw_fact() -> None:
    mgr = FakeManager()
    await store_entity_fact(
        mgr, company_id="co-1", entity=EntityRef("owner", "77"), fact="wants CC on everything"
    )
    assert mgr.rows[0]["memory"] == "wants CC on everything"


# ── read ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_entity_memories_filters_by_entity() -> None:
    mgr = FakeManager()
    await store_entity_fact(mgr, company_id="co-1", entity=_UNIT_4B, fact="late payer")
    await store_entity_fact(mgr, company_id="co-1", entity=_UNIT_4B, fact="dog complaint")
    # A different unit + a different company must NOT leak into the result.
    await store_entity_fact(
        mgr, company_id="co-1", entity=EntityRef("unit", "9999", "Unit 1A"), fact="quiet"
    )
    await store_entity_fact(mgr, company_id="co-2", entity=_UNIT_4B, fact="other company")

    got = await get_entity_memories(mgr, company_id="co-1", entity=_UNIT_4B)
    texts = sorted(m["memory"] for m in got)
    assert texts == ["Unit 4B: dog complaint", "Unit 4B: late payer"]


@pytest.mark.asyncio
async def test_get_entity_memories_matches_int_or_str_id() -> None:
    mgr = FakeManager()
    await store_entity_fact(
        mgr, company_id="co-1", entity=EntityRef("tenant", 4610, "Jane"), fact="x"
    )
    # Query with a string id — EntityRef normalizes both sides to str.
    got = await get_entity_memories(mgr, company_id="co-1", entity=EntityRef("tenant", "4610"))
    assert len(got) == 1


@pytest.mark.asyncio
async def test_get_entity_memories_empty_when_unavailable() -> None:
    off = FakeManager(available=False)
    assert await get_entity_memories(off, company_id="co-1", entity=_UNIT_4B) == []


def test_format_entity_memories_renders_bullets_with_category() -> None:
    mems = [
        {"memory": "Unit 4B: late payer", "metadata": {"category": "financial"}},
        {"memory": "Unit 4B: dog", "metadata": {}},
        {"memory": "", "metadata": {}},  # skipped
    ]
    out = format_entity_memories(mems)
    assert out == "- [financial] Unit 4B: late payer\n- Unit 4B: dog"
    assert format_entity_memories([]) == ""


# ── pure derivers ────────────────────────────────────────────────


def test_derive_payment_behavior_flags_chronic_late_payer() -> None:
    assert (
        derive_payment_behavior_fact(LatePaymentSummary(6, 4, 8))
        == "chronic late-payer — paid rent late 4 of the last 6 months (most recently 8 days late)"
    )
    # Exactly at the 50% bar with >=2 late — flagged, no days-late tail.
    assert derive_payment_behavior_fact(LatePaymentSummary(6, 3)) is not None


def test_derive_payment_behavior_returns_none_below_bar() -> None:
    assert derive_payment_behavior_fact(LatePaymentSummary(6, 1)) is None  # below min count
    assert derive_payment_behavior_fact(LatePaymentSummary(12, 2)) is None  # below 50% fraction
    assert derive_payment_behavior_fact(LatePaymentSummary(0, 0)) is None  # empty window


def test_derive_balance_fact() -> None:
    assert (
        derive_balance_fact(BalanceSummary(1450.0, date(2026, 7, 1)))
        == "carries an outstanding balance of $1,450.00 as of 2026-07-01"
    )
    assert derive_balance_fact(BalanceSummary(0.0, date(2026, 7, 1))) is None
    assert derive_balance_fact(BalanceSummary(-25.0, date(2026, 7, 1))) is None  # credit balance


# ── aging-based delinquency deriver (#534 Stage B) ───────────────


def test_derive_delinquency_flags_chronic_when_90plus() -> None:
    # Real sandbox shape: lease 24195 (grounded 2026-07-11).
    aging = AgingSummary(
        overdue_31_60=100.0, overdue_61_90=170.0, overdue_over_90=350.0, as_of=date(2026, 7, 11)
    )
    assert aging.overdue_30_plus == 620.0
    assert derive_delinquency_fact(aging) == (
        "chronic late-payer — $620.00 is 30+ days overdue, including $350.00 "
        "over 90 days (as of 2026-07-11)"
    )
    assert delinquency_severity(aging) == "critical"


def test_derive_delinquency_behind_when_30plus_but_not_90() -> None:
    aging = AgingSummary(
        overdue_31_60=75.0, overdue_61_90=25.0, overdue_over_90=0.0, as_of=date(2026, 7, 1)
    )
    assert derive_delinquency_fact(aging) == (
        "behind on rent — $100.00 is 30+ days overdue (as of 2026-07-01)"
    )
    assert delinquency_severity(aging) == "warning"


def test_derive_delinquency_none_when_current() -> None:
    # Nothing 30+ overdue (only a 0-30 balance, or nothing) → no flag.
    aging = AgingSummary(
        overdue_31_60=0.0, overdue_61_90=0.0, overdue_over_90=0.0, as_of=date.today()
    )
    assert derive_delinquency_fact(aging) is None
    assert delinquency_severity(aging) == "info"
