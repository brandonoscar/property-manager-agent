"""Phase-2 recon analyst — the grounding gate, dedup, and safe-off posture.

Pins the trust contract: an ungrounded claim (missing anchor, missing
citation, invented dollar figure) NEVER reaches the ledger; the cycle is
a no-op unless explicitly enabled; a budget-spent company is skipped; an
LLM failure writes nothing.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.reconciliation.analyst import (
    analyze_company,
    run_analyst_cycle,
    validate_risks,
)
from helixis.settings import Settings

COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"

DOSSIER = {
    "leases": [
        {
            "lease_id": 4001,
            "property_id": 77,
            "unit_number": "4B",
            "rent": 1000.0,
            "balance": 2180.0,
            "lease_end": "2026-09-30",
            "days_to_end": 60,
            "charges_3mo": 3000.0,
            "payments_3mo": 820.0,
        }
    ],
    "work_orders": [
        {
            "wo_id": 9001,
            "property_id": 77,
            "unit_id": 12,
            "title": "AC not cooling",
            "status": "New",
            # ⚠ NO ``age_days``. The dossier query used to derive it from
            # ``updated_at``, which is when the MIRROR row was last written —
            # live-measured 2026-08-28, all 60 Blue Ridge work orders share one
            # sync date, so every open item reported age 0. Buildium sends no
            # created/updated date on /workorders at all, so there is nothing to
            # point the column at and it was deleted rather than repaired. A
            # fixture carrying a plausible 9 was the reason nothing looked wrong.
        }
    ],
    "open_rule_flags": [],
}


def _risk(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "churn_risk",
        "severity": "warning",
        "title": "Unit 4B is a churn risk",
        "detail": "Balance $2,180 with the lease ending in 60 days and an open AC ticket.",
        "entity_type": "lease",
        "entity_id": 4001,
        "property_id": 77,
        "citations": [
            {"section": "leases", "id": 4001},
            {"section": "work_orders", "id": 9001},
        ],
    }
    base.update(overrides)
    return base


# ── the grounding gate ───────────────────────────────────────────────


def test_grounded_risk_survives_and_ungrounded_variants_drop() -> None:
    raw = {
        "risks": [
            _risk(),  # fully grounded → kept
            _risk(entity_id=9999),  # anchor not in dossier
            _risk(citations=[]),  # no citations
            _risk(citations=[{"section": "leases", "id": 123}]),  # cites a ghost row
            _risk(detail="Tenant owes $5,000 right now."),  # invented dollar figure
            _risk(kind="vibes"),  # unknown kind
            _risk(severity="apocalyptic"),  # unknown severity
        ]
    }
    kept, dropped = validate_risks(raw, DOSSIER, max_flags=5)
    assert len(kept) == 1
    assert kept[0]["entity_id"] == 4001
    assert len(dropped) == 6
    assert any("anchor" in d for d in dropped)
    assert any("no citations" in d for d in dropped)
    assert any("not in dossier" in d for d in dropped)
    assert any("5000" in d for d in dropped)


def test_validator_enforces_the_cap_and_tolerates_garbage() -> None:
    raw = {"risks": [_risk(entity_id=4001) for _ in range(4)]}
    kept, dropped = validate_risks(raw, DOSSIER, max_flags=2)
    assert len(kept) == 2 and any("cap" in d for d in dropped)

    assert validate_risks({"risks": "nope"}, DOSSIER, 5) == ([], ["risks is not a list"])
    assert validate_risks("not even a dict", DOSSIER, 5)[0] == []
    assert validate_risks({"risks": [42]}, DOSSIER, 5)[0] == []


# ── analyze_company ──────────────────────────────────────────────────


def _pool(conn: Any) -> MagicMock:
    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


def _llm_response(payload: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
    )


def _settings(enabled: bool = True) -> Settings:
    return Settings(
        env="test",
        memory={"enabled": False},
        recon={"analyst_enabled": enabled, "analyst_max_flags": 5},
    )


def _dossier_conn() -> MagicMock:
    """conn whose three dossier fetches return DOSSIER's sections."""
    conn = MagicMock()
    conn.fetch = AsyncMock(
        side_effect=[DOSSIER["leases"], DOSSIER["work_orders"], DOSSIER["open_rule_flags"]]
    )
    conn.execute = AsyncMock()
    return conn


@pytest.mark.asyncio
async def test_analyze_company_writes_grounded_flags_with_analyst_source() -> None:
    conn = _dossier_conn()
    complete = AsyncMock(return_value=_llm_response({"risks": [_risk()]}))

    written = await analyze_company(_pool(conn), _settings(), COMPANY_ID, completion_fn=complete)

    assert written == 1
    upsert = conn.execute.await_args_list[0].args
    assert "'analyst', 'analyst'" in upsert[0]
    assert upsert[2] == "churn_risk:lease:4001"  # kind-anchored fingerprint = dedupable
    assert upsert[3] == "warning"
    evidence = json.loads(upsert[9])
    assert evidence["citations"][0] == {"section": "leases", "id": 4001}
    # last execute = the 7-day age-out for undetected analyst flags
    assert "last_seen_at < now()" in conn.execute.await_args_list[-1].args[0]
    # the dossier is the entire user prompt — no other context leaks in
    sent = complete.await_args.kwargs["messages"][1]["content"]
    assert "4001" in sent and "AC not cooling" in sent


@pytest.mark.asyncio
async def test_analyze_company_hallucination_writes_nothing() -> None:
    """The model invents a risk about a lease that doesn't exist — the
    validator eats it and the ledger stays untouched (except age-out)."""
    conn = _dossier_conn()
    complete = AsyncMock(return_value=_llm_response({"risks": [_risk(entity_id=31337)]}))

    written = await analyze_company(_pool(conn), _settings(), COMPANY_ID, completion_fn=complete)

    assert written == 0
    sqls = [c.args[0] for c in conn.execute.await_args_list]
    assert not any("INSERT INTO helixis_risk_flags" in s for s in sqls)


@pytest.mark.asyncio
async def test_empty_mirror_skips_the_llm_entirely() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[[], [], []])
    conn.execute = AsyncMock()
    complete = AsyncMock()

    written = await analyze_company(_pool(conn), _settings(), COMPANY_ID, completion_fn=complete)

    assert written == 0
    complete.assert_not_awaited()


# ── the cycle ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cycle_is_a_noop_when_dark() -> None:
    pool = MagicMock()
    await run_analyst_cycle(pool, _settings(enabled=False))
    pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_cycle_skips_budget_spent_companies_and_survives_failures() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[{"company_id": COMPANY_ID}])
    with (
        patch(
            "helixis.reconciliation.analyst.spend.budget_exceeded",
            AsyncMock(return_value=True),
        ),
        patch("helixis.reconciliation.analyst.analyze_company", AsyncMock()) as analyze,
    ):
        await run_analyst_cycle(_pool(conn), _settings())
    analyze.assert_not_awaited()  # budget spent → the analyst does not run

    with (
        patch(
            "helixis.reconciliation.analyst.spend.budget_exceeded",
            AsyncMock(return_value=False),
        ),
        patch(
            "helixis.reconciliation.analyst.analyze_company",
            AsyncMock(side_effect=RuntimeError("gemini down")),
        ),
    ):
        # must not raise — one bad company never kills the cycle
        await run_analyst_cycle(_pool(conn), _settings())


def test_the_dossier_does_not_hand_the_analyst_a_sync_timestamp_as_an_age() -> None:
    """``age_days`` was ``current_date - updated_at::date`` — the age of our
    SYNC, not of the work order. Live-measured 2026-08-28 on Blue Ridge: all 60
    open work orders share a single ``updated_at`` date, so the analyst's
    dossier said every one of them was 0 days old, and the analyst cites what
    the dossier gives it.

    ⚠ Asserted against the SQL, not against a hand-built dossier dict. A test
    feeding its own rows would pass whatever the query selects (gotcha 45), and
    the fixture above carrying a plausible ``age_days: 9`` is exactly how this
    stayed invisible. The rule is the general one: a duration derived from a
    mirror-write timestamp is not a fact about the record.
    """
    from helixis.reconciliation.analyst import _DOSSIER_WOS_SQL

    select = _DOSSIER_WOS_SQL.split("FROM")[0]
    assert "age_days" not in select, (
        "the dossier is selecting an age again — Buildium sends no "
        "created/updated date on /workorders (schemas.WorkOrder records the "
        "probe), so any age here can only be measuring our own sync"
    )
    assert "current_date" not in select.lower(), (
        "a duration computed in the SELECT is measuring a mirror-write "
        "timestamp; use due_date, which the endpoint really does send"
    )


def test_the_dossier_hands_the_analyst_payments_as_money_received() -> None:
    """Buildium signs a ledger row by its effect on the BALANCE, so a Payment
    arrives negative. Live-verified across all three companies: 144 of 144
    Payment rows negative, 619 of 619 Charge rows positive.

    The mirror stores that faithfully and must keep doing so — flipping the
    sign at write time would corrupt the ledger (gotcha 21). What was wrong is
    what the dossier HANDED the analyst: ``payments_3mo: -820`` sitting beside
    ``charges_3mo: 3000``, with nothing saying the two use opposite
    conventions. The model either narrates a negative payment — the sanitizer's
    own comment quotes a shipped card reading "payments_3mo figure of -900" —
    or compares them directly and gets the arrears backwards.

    ⚠ Asserted on the SQL, not on a hand-built dossier. A test feeding its own
    rows proves nothing about what the query selects (gotcha 45), and that is
    exactly how the fixture above sat at a positive 820 while production
    computed a negative.
    """
    import re

    from helixis.reconciliation.analyst import _DOSSIER_LEASES_SQL

    # ⚠ Match the AGGREGATE, not the SELECT-list alias. Both read
    # "...payments_3mo", and splitting on the name alone lands on
    # ``COALESCE(p.payments_3mo, 0)::float`` — the outer reference, which
    # carries no sign at all. The first version of this guard did exactly that
    # and failed against correct code, which is the harmless direction; the
    # same mistake on a must-not assertion passes against a bug.
    sql = re.sub(r"\s+", " ", _DOSSIER_LEASES_SQL)
    assert "-SUM(total_amount) AS payments_3mo" in sql, (
        "payments_3mo is summing Buildium's raw signed amount again — the "
        "analyst is being handed a negative number labelled 'payments'"
    )
    # And the other direction: charges must NOT be negated. They are already
    # positive, so flipping them would invert every arrears comparison — the
    # same bug pointed the other way (gotcha 38).
    assert "SUM(total_amount) AS charges_3mo" in sql
    assert "-SUM(total_amount) AS charges_3mo" not in sql
