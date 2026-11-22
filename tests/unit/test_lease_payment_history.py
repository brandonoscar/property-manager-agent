"""``lease_payment_history``: one lease's charges and payments, on Rentvine.

A Rentvine workspace's agent was told it can answer "what each lease was
charged and paid, and when" (``run_pms.RENTVINE_RUN_NOTE``), and no tool read
``helixis_lease_transactions`` for one lease: the live Buildium ledger reads are
hidden there (``run_pms``). This is that read.

Pinned here:

* one lease's rows, newest first, with the charged, paid and credited totals,
  the last payment and what it owes now, all on the answer (gotcha 41);
* a row with no stated amount is said as such and kept out of the totals,
  and a history that has not synced is never "nothing paid" (rule 14);
* several matches are a question, never a dump; a long history says what it
  left off (gotcha 26);
* it is visible on Rentvine runs only, on the specialist only.

All rows are synthetic.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agents import RunContextWrapper

from helixis.agents import call_budget, mirror_tools, run_pms, step_narration
from helixis.agents.buildium_agent import create_buildium_agent
from helixis.agents.orchestrator import create_orchestrator
from helixis.db import pms_coverage
from helixis.db.pms_coverage import RENTVINE, SYNC_STATE_STAGE
from helixis.db.repositories.mirror_sync_state import StageSync
from helixis.settings import Settings

CO = "co-rv"
TODAY = date(2026, 10, 3)


def _on(monkeypatch: pytest.MonkeyPatch, *, unlanded: str | None = None) -> None:
    async def _pms(_pool: Any, _company_id: str) -> str:
        return RENTVINE

    landed = {s: "2026-10-02T00:00:00Z" for s in SYNC_STATE_STAGE.values() if s != unlanded}
    monkeypatch.setattr(pms_coverage, "company_pms_strict", _pms)
    monkeypatch.setattr(pms_coverage, "get_freshness", AsyncMock(return_value=landed))


class _Conn:
    def __init__(self, leases: list[dict[str, Any]], txns: list[dict[str, Any]]) -> None:
        self.leases = leases
        self.txns = txns
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        if "FROM helixis_leases l" in sql:
            return self.leases
        if "FROM helixis_lease_transactions" in sql:
            wanted = set(args[1])
            return [t for t in self.txns if t["lease_id"] in wanted]
        return []


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _Conn:
        return self.conn

    async def __aexit__(self, *_a: object) -> bool:
        return False


@contextmanager
def _tools(
    leases: list[dict[str, Any]],
    txns: list[dict[str, Any]] | None = None,
    *,
    landed: frozenset[str] = frozenset({"transactions", "balances"}),
) -> Iterator[_Conn]:
    conn = _Conn(leases, txns or [])
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _Acquire(conn))
    ctx = MagicMock()
    ctx.company_id = CO

    async def _stage(_pool: Any, _company: str, stage: str) -> StageSync | None:
        return StageSync(synced_at=datetime.now(UTC), row_count=1) if stage in landed else None

    with (
        patch.object(mirror_tools, "current_memory_context", return_value=ctx),
        patch.object(mirror_tools, "get_pool", return_value=pool),
        patch.object(mirror_tools, "_freshness_line", AsyncMock(return_value="(as of now)")),
        patch.object(mirror_tools.sync_state, "get_stage_sync", AsyncMock(side_effect=_stage)),
        patch.object(mirror_tools, "company_timezone", AsyncMock(return_value="America/Denver")),
        patch.object(mirror_tools, "today_in", return_value=TODAY),
    ):
        yield conn


def _lease(lease_id: int = 7, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "buildium_id": lease_id,
        "status": "Active",
        "rent": 1250.0,
        "property_name": "Redbud",
        "unit_number": "4B",
        "tenant_names": "Dana Example",
        "total_balance": None,
    }
    row.update(over)
    return row


def _txn(
    lease_id: int, day: date, kind: str, amount: float | None, description: str | None = "Rent"
) -> dict[str, Any]:
    return {
        "lease_id": lease_id,
        "transaction_date": datetime(day.year, day.month, day.day, tzinfo=UTC),
        "transaction_type": kind,
        "total_amount": amount,
        "description": description,
    }


# ── one lease ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_lease_reads_newest_first_with_its_totals(monkeypatch) -> None:
    _on(monkeypatch)
    txns = [
        _txn(7, date(2026, 9, 1), "Charge", 1250.0),
        _txn(7, date(2026, 9, 3), "Payment", -1250.0, None),
        _txn(7, date(2026, 10, 1), "Charge", 1250.0),
        _txn(7, date(2026, 9, 15), "Credit", -50.0, "Concession"),
    ]
    # The SQL orders newest first; the fake returns them in that order.
    txns.sort(key=lambda t: t["transaction_date"], reverse=True)
    with _tools([_lease(total_balance=1200.0)], txns) as conn:
        out = await mirror_tools._lease_payment_history_impl("Dana", 6)
    lines = out.split("\n")
    assert lines[0] == (
        "Payment history for Dana Example — Redbud · Unit 4B (Active lease, $1,250/mo), "
        "since 2026-05-01:"
    )
    assert lines[1] == (
        "- Charged $2,500, paid $1,250, credited $50 across 4 transactions. "
        "Last payment 2026-09-03 ($1,250)."
    )
    assert lines[2:6] == [
        "- 2026-10-01 Charge $1,250 — Rent",
        "- 2026-09-15 Credit $50 — Concession",
        "- 2026-09-03 Payment $1,250",
        "- 2026-09-01 Charge $1,250 — Rent",
    ]
    assert "- Owes $1,200 now." in lines
    assert lines[-1] == "(as of now)"
    # Scoped to this company (rule 11), and the transactions read only the
    # matched lease.
    (lease_sql, lease_args), (txn_sql, txn_args) = conn.calls
    assert "WHERE l.company_id = $1::uuid" in lease_sql and lease_args[0] == CO
    assert "WHERE company_id = $1::uuid AND lease_id = ANY($2::bigint[])" in txn_sql
    assert "ORDER BY lease_id, transaction_date DESC NULLS LAST" in " ".join(txn_sql.split())
    assert txn_args == (CO, [7])


@pytest.mark.asyncio
async def test_a_returned_payment_and_an_unstated_amount_are_said_and_kept_out(
    monkeypatch,
) -> None:
    _on(monkeypatch)
    txns = [
        _txn(7, date(2026, 9, 20), "Payment", 1250.0, "NSF"),
        _txn(7, date(2026, 9, 12), "Charge", None, "Late fee"),
        _txn(7, date(2026, 9, 10), "Lease Payout", None, None),
        _txn(7, date(2026, 9, 3), "Payment", -1250.0),
    ]
    with _tools([_lease()], txns):
        out = await mirror_tools._lease_payment_history_impl("Redbud 4B", 6)
    assert "- 2026-09-20 Payment returned $1,250 — NSF" in out
    assert "- 2026-09-10 Lease Payout: amount not stated" in out
    # A known type with no stated amount is not a $0 charge either.
    assert "- 2026-09-12 Charge: amount not stated — Late fee" in out
    # The return nets the payment out; the payout and the unstated charge
    # are in no total.
    assert "Charged $0, paid $0, credited $0 across 4 transactions." in out
    assert "2 rows of another kind or with no stated amount" in out
    assert "$0" not in out.split("Lease Payout")[1].split("\n")[0]


@pytest.mark.asyncio
async def test_an_unsynced_history_is_never_nothing_paid(monkeypatch) -> None:
    _on(monkeypatch)
    with _tools([_lease()], [], landed=frozenset({"balances"})):
        out = await mirror_tools._lease_payment_history_impl("Dana", 6)
    assert "has not finished syncing" in out
    assert "NOT the same as nothing paid" in out
    assert "No charges" not in out

    with _tools([_lease()], []):
        out = await mirror_tools._lease_payment_history_impl("Dana", 6)
    assert "- No charges, payments or credits recorded since 2026-05-01." in out


@pytest.mark.asyncio
async def test_rows_outside_the_window_are_counted_not_shown(monkeypatch) -> None:
    _on(monkeypatch)
    txns = [
        _txn(7, date(2026, 10, 1), "Charge", 1250.0),
        _txn(7, date(2026, 3, 1), "Charge", 1200.0),
        _txn(7, date(2026, 2, 1), "Charge", 1200.0),
    ]
    with _tools([_lease()], txns):
        out = await mirror_tools._lease_payment_history_impl("Dana", 6)
    assert "2026-03-01" not in out
    assert "- 2 earlier rows before 2026-05-01 not shown; ask for more months (up to 24)." in out
    assert "across 1 transaction." in out


@pytest.mark.asyncio
async def test_a_long_history_says_what_it_left_off(monkeypatch) -> None:
    _on(monkeypatch)
    n = mirror_tools._HISTORY_ROWS_PER_LEASE + 3
    txns = [_txn(7, date(2026, 9, 1 + i % 28), "Charge", 10.0) for i in range(n)]
    with _tools([_lease()], txns):
        out = await mirror_tools._lease_payment_history_impl("Dana", 6)
    shown = [ln for ln in out.split("\n") if ln.startswith("- 2026-")]
    assert len(shown) == mirror_tools._HISTORY_ROWS_PER_LEASE
    assert "…and 3 older rows in this period, counted in the totals above." in out
    assert f"across {n} transactions." in out


@pytest.mark.asyncio
async def test_unsynced_balances_are_not_owing_nothing(monkeypatch) -> None:
    _on(monkeypatch)
    with _tools([_lease(total_balance=None)], [], landed=frozenset({"transactions"})):
        out = await mirror_tools._lease_payment_history_impl("Dana", 6)
    assert "What it owes now is not synced yet, which is NOT the same as owing nothing." in out
    assert "Owes nothing" not in out

    with _tools([_lease(total_balance=None)], []):
        out = await mirror_tools._lease_payment_history_impl("Dana", 6)
    assert "- Owes nothing now (not in the synced balance snapshot)." in out


# ── finding the lease ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_several_matches_are_a_question_not_a_dump(monkeypatch) -> None:
    _on(monkeypatch)
    leases = [_lease(i, unit_number=str(i), tenant_names=f"Tenant {i}") for i in range(1, 6)]
    with _tools(leases, [_txn(1, date(2026, 9, 1), "Charge", 1.0)]) as conn:
        out = await mirror_tools._lease_payment_history_impl("Redbud", 6)
    assert out.split("\n")[0] == "5 leases match 'Redbud'. Which one?"
    assert "- Tenant 3 — Redbud · Unit 3 (Active lease)" in out
    assert out.endswith("Ask again with the tenant's full name or the property and unit.")
    # No ledger was read for a question nobody asked yet.
    assert len(conn.calls) == 1

    many = [_lease(i, unit_number=str(i)) for i in range(1, 12)]
    with _tools(many):
        out = await mirror_tools._lease_payment_history_impl("Redbud", 6)
    assert out.startswith("More than 10 leases match 'Redbud'.")


@pytest.mark.asyncio
async def test_no_match_says_how_to_ask(monkeypatch) -> None:
    _on(monkeypatch)
    with _tools([]):
        out = await mirror_tools._lease_payment_history_impl("Nobody", 6)
    assert out.startswith("No lease matches 'Nobody'.")


@pytest.mark.asyncio
async def test_the_query_is_normalised_and_a_number_is_a_lease_id(monkeypatch) -> None:
    _on(monkeypatch)
    with _tools([]) as conn:
        await mirror_tools._lease_payment_history_impl("  Redbud,   4B ", 6)
        await mirror_tools._lease_payment_history_impl("990000123", 6)
        blank = await mirror_tools._lease_payment_history_impl("  ", 6)
    assert conn.calls[0][1] == (CO, "Redbud 4B", None)
    assert conn.calls[1][1] == (CO, "990000123", 990000123)
    assert blank == (
        "Name the tenant, the property and unit, or the lease whose payment history you want."
    )
    assert len(conn.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("months", "since"), [(0, "2026-10-01"), (100, "2024-11-01"), ("x", "2026-05-01")]
)
async def test_months_is_clamped(monkeypatch, months: Any, since: str) -> None:
    _on(monkeypatch)
    with _tools([_lease()], []):
        out = await mirror_tools._lease_payment_history_impl("Dana", months)
    assert f"since {since}:" in out


@pytest.mark.asyncio
async def test_it_declines_until_leases_and_transactions_are_seen(monkeypatch) -> None:
    _on(monkeypatch, unlanded="leases")
    with _tools([_lease()]) as conn:
        out = await mirror_tools._lease_payment_history_impl("Dana", 6)
    assert conn.calls == []
    assert "lease" in out.lower()

    _on(monkeypatch, unlanded="transactions")
    with _tools([_lease()]) as conn:
        out = await mirror_tools._lease_payment_history_impl("Dana", 6)
    assert conn.calls == []
    assert "charges and payments" in out


# ── who can see it ────────────────────────────────────────────────────────────


async def _names(agent: Any, pms: str | None) -> set[str]:
    token = run_pms.bind_run_pms(pms)
    try:
        return {t.name for t in await agent.get_all_tools(RunContextWrapper(context=None))}
    finally:
        run_pms.reset_run_pms(token)


@pytest.mark.asyncio
async def test_only_the_rentvine_specialist_sees_it() -> None:
    specialist = create_buildium_agent(Settings())
    assert "lease_payment_history" in await _names(specialist, RENTVINE)
    for pms in ("buildium", None):
        assert "lease_payment_history" not in await _names(specialist, pms)
    # A one-lease read, like property_snapshot: not a router rollup.
    for loop_mode in (False, True):
        router = create_orchestrator(Settings(), loop_mode=loop_mode)
        assert "lease_payment_history" not in await _names(router, RENTVINE)


def test_no_buildium_run_prompt_names_it_and_the_rentvine_section_does() -> None:
    orchestrator = create_orchestrator(Settings())
    agents = [orchestrator, *(h for h in orchestrator.handoffs if hasattr(h, "instructions"))]
    agents.append(create_buildium_agent(Settings()))
    for agent in agents:
        assert not re.search(r"\blease_payment_history\b", str(agent.instructions)), agent.name
    swapped = "\n".join(text for _, text in run_pms.RENTVINE_SECTIONS)
    assert re.search(r"\blease_payment_history\b", swapped)


def test_it_is_memoised_and_narrated() -> None:
    assert "lease_payment_history" in call_budget._MIRROR_READS
    assert "lease_payment_history" in step_narration._EXPLICIT
