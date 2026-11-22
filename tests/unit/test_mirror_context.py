"""Mirror-context "Helixis noticed" — cross-record facts, not restatement.

Pins the founder's 2026-07-06 rule: the noticed section must carry
things a PM CAN'T read in the message — balance owed, lease ending,
repeat tickets, a pending promise — pulled from the local mirror with
zero API calls. Covers the gatherer's fact phrasing + priority cap, the
email-sender lease resolve, per-probe failure containment, and the
planner's deterministic merge (facts first, LLM rephrasings deduped).
"""

from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.agent.planner.action_planner import (
    _build_user_prompt,
    _merge_mirror_signals,
)
from helixis.agent.planner.models import ActionSurface
from helixis.webhooks.mirror_context import gather_mirror_context

COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
TODAY = dt.date(2026, 7, 15)


class _Conn:
    def __init__(
        self,
        fetchrow_results: list[Any] | None = None,
        fetch_results: list[Any] | None = None,
    ) -> None:
        self.fetchrow = AsyncMock(side_effect=fetchrow_results or [])
        self.fetch = AsyncMock(side_effect=fetch_results or [])


def _pool(conn: _Conn) -> MagicMock:
    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


def _lease_row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "total_balance": None,
        "lease_to_date": None,
        "paid_this_month": True,
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_gathers_cross_record_facts_in_priority_order_capped() -> None:
    """Everything fires at once → the five highest-priority facts, money
    first — a dossier is capped to what a card can carry."""
    conn = _Conn(
        fetchrow_results=[
            _lease_row(
                total_balance=450.0,
                paid_this_month=False,
                lease_to_date=TODAY + dt.timedelta(days=45),
            ),
            {"promised": TODAY + dt.timedelta(days=2)},  # pending promise
            {"open_wos": 2, "recent_tasks": 3},
            {"move_out_date": TODAY + dt.timedelta(days=20)},  # turnover
            {"overdue": 4},
        ],
        fetch_results=[[{"email": "sarah@x.com"}]],  # lease → tenant emails
    )
    facts = await gather_mirror_context(
        _pool(conn),
        company_id=COMPANY_ID,
        property_id=700,
        unit_id=55,
        lease_id=9101,
        exclude_task_id=42,
        today=TODAY,
    )
    assert len(facts) == 5  # capped at MAX_FACTS
    assert "outstanding balance of $450.00" in facts[0]
    assert "No rent payment" in facts[1]
    assert "renewal window" in facts[2] and "45 days" in facts[2]
    assert "promised payment by 2026-07-17" in facts[3]
    assert "2 work orders are already open" in facts[4]


@pytest.mark.asyncio
async def test_email_sender_resolves_their_lease() -> None:
    """An email event has no lease — the sender's address finds it, so
    money/lease facts light up for emails too."""
    conn = _Conn(
        fetchrow_results=[
            {"lease_id": 9101, "property_id": 700, "unit_id": 55},  # resolve
            _lease_row(total_balance=450.0),
            None,  # no promise
            {"open_wos": 0, "recent_tasks": 0},
            None,  # no turnover
            {"overdue": 0},
        ],
    )
    facts = await gather_mirror_context(
        _pool(conn),
        company_id=COMPANY_ID,
        tenant_emails=["Sarah@X.com"],
        today=TODAY,
    )
    assert facts == ["This lease carries an outstanding balance of $450.00."]
    resolve_sql = conn.fetchrow.await_args_list[0].args[0]
    assert "lower(t.email) = ANY($2::text[])" in resolve_sql
    assert conn.fetchrow.await_args_list[0].args[2] == ["sarah@x.com"]


@pytest.mark.asyncio
async def test_operational_signal_leads_the_noticed_card() -> None:
    """#534 Stage B: a stored late-payer signal for the lease is injected into
    the noticed facts (and leads, being the sharpest thing a PM can't read off
    the message)."""
    conn = _Conn(
        fetchrow_results=[
            _lease_row(total_balance=720.0, paid_this_month=False),
            None,  # no promise
            None,  # no unit facts probe row
            None,  # no turnover
            None,  # no property
        ],
        fetch_results=[[{"email": "sarah@x.com"}]],  # lease → tenant emails
    )
    signal = "Unit 2B: chronic late-payer — $620.00 is 30+ days overdue"
    with patch(
        "helixis.webhooks.operational_signals.get_lease_entity_signals",
        new=AsyncMock(return_value=[signal]),
    ):
        facts = await gather_mirror_context(
            _pool(conn), company_id=COMPANY_ID, lease_id=9101, today=TODAY
        )
    assert facts[0] == signal  # the operational signal leads
    assert any("outstanding balance of $720.00" in f for f in facts)  # existing facts still present


@pytest.mark.asyncio
async def test_operational_signal_probe_failure_is_contained() -> None:
    """A broken entity-signals read costs that fact only, never the card."""
    conn = _Conn(
        fetchrow_results=[_lease_row(total_balance=450.0), None, None, None, None],
        fetch_results=[[{"email": "s@x.com"}]],
    )
    with patch(
        "helixis.webhooks.operational_signals.get_lease_entity_signals",
        new=AsyncMock(side_effect=RuntimeError("entity signals table missing")),
    ):
        facts = await gather_mirror_context(
            _pool(conn), company_id=COMPANY_ID, lease_id=9101, today=TODAY
        )
    # No signal injected, but the balance fact still lands.
    assert any("outstanding balance of $450.00" in f for f in facts)


@pytest.mark.asyncio
async def test_probe_failure_is_contained_per_fact() -> None:
    """One broken probe (unapplied table, bad query) costs that fact
    only — never the card."""
    conn = _Conn(
        fetchrow_results=[
            RuntimeError("lease probe broke"),
            {"open_wos": 1, "recent_tasks": 0},
            None,  # turnover
            {"overdue": 0},
        ],
        fetch_results=[[]],  # no tenant emails
    )
    facts = await gather_mirror_context(
        _pool(conn),
        company_id=COMPANY_ID,
        property_id=700,
        unit_id=55,
        lease_id=9101,
        today=TODAY,
    )
    assert facts == ["1 work order is already open on this unit."]


@pytest.mark.asyncio
async def test_quiet_records_produce_no_facts() -> None:
    conn = _Conn(
        fetchrow_results=[
            _lease_row(),  # no balance, paid, no end date
            {"open_wos": 0, "recent_tasks": 1},  # 1 recent task < threshold
            None,
            {"overdue": 0},
        ],
        fetch_results=[[]],
    )
    facts = await gather_mirror_context(
        _pool(conn),
        company_id=COMPANY_ID,
        property_id=700,
        unit_id=55,
        lease_id=9101,
        today=TODAY,
    )
    assert facts == []


@pytest.mark.asyncio
async def test_early_month_suppresses_the_rent_fact() -> None:
    """Before day 5 'no rent this month' is true of everyone — noise."""
    conn = _Conn(fetchrow_results=[_lease_row(paid_this_month=False)], fetch_results=[[]])
    facts = await gather_mirror_context(
        _pool(conn),
        company_id=COMPANY_ID,
        lease_id=9101,
        today=dt.date(2026, 7, 2),
    )
    assert facts == []


# ── planner integration ───────────────────────────────────────────────


def _surface(signals: list[str] | None) -> ActionSurface:
    return ActionSurface(
        schema_version=2,
        summary="Sarah Chen reported a leaking sink in unit 3C.",
        actions=[],
        freeform_placeholder="e.g. 'schedule a plumber for Tuesday'",
        signals=signals,
    )


def test_merge_puts_facts_first_and_dedupes_rephrasings() -> None:
    facts = [
        "This lease carries an outstanding balance of $450.00.",
        "The lease ends 2026-08-29 — 45 days out, inside the renewal window.",
    ]
    merged = _merge_mirror_signals(
        _surface(
            [
                "Tenant has an outstanding balance of $450.00 on the lease",  # rephrase → drop
                "They need an answer by Friday",  # message-level catch → keep
            ]
        ),
        facts,
    )
    assert merged.signals is not None
    assert merged.signals[:2] == facts
    assert merged.signals[2] == "They need an answer by Friday"
    assert len(merged.signals) == 3


def test_merge_without_facts_is_a_noop() -> None:
    surface = _surface(["a signal"])
    assert _merge_mirror_signals(surface, None) is surface
    assert _merge_mirror_signals(surface, []) is surface


def test_prompt_carries_the_verified_context_block_only_when_present() -> None:
    enriched = MagicMock()
    enriched.model_dump = MagicMock(return_value={"event_name": "Task.Created"})
    with_facts = _build_user_prompt(
        enriched, mirror_facts=["This lease carries an outstanding balance of $450.00."]
    )
    assert "VERIFIED CONTEXT FROM OCCUPELLA'S RECORDS" in with_facts
    assert "NOT in the message" in with_facts
    assert "outstanding balance of $450.00" in with_facts
    assert "NEVER change a number or date" in with_facts

    without = _build_user_prompt(enriched)
    assert "VERIFIED CONTEXT" not in without


@pytest.mark.asyncio
@pytest.mark.parametrize("landed", [True, False])
async def test_no_rent_claim_for_rentvine_until_its_ledger_has_landed(
    monkeypatch: pytest.MonkeyPatch, landed: bool
) -> None:
    """ "No rent payment has posted" is a claim about ABSENCE. Before Rentvine's
    lease history lands, every lease lacks a Payment row, so every email from a
    Rentvine tenant would have carried it."""
    from helixis.db import pms_coverage
    from helixis.rentvine.sync import SYNC_STATE_STAGE

    stages = {s: "2026-10-02T00:00:00Z" for s in SYNC_STATE_STAGE.values()}
    if not landed:
        stages.pop("transactions")
    monkeypatch.setattr(pms_coverage, "company_pms_strict", AsyncMock(return_value="rentvine"))
    monkeypatch.setattr(pms_coverage, "get_freshness", AsyncMock(return_value=stages))
    conn = _Conn(fetchrow_results=[_lease_row(paid_this_month=False)], fetch_results=[[]])
    facts = await gather_mirror_context(
        _pool(conn), company_id=COMPANY_ID, lease_id=9101, today=TODAY
    )
    assert any("No rent payment" in f for f in facts) is landed
