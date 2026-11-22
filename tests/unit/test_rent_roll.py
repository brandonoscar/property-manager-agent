"""``rent_roll``: the rent roll a Rentvine workspace had no tool for.

The sandbox run on the founder's machine (2026-10-02, ``rentvine_ai_e2e.py
--sandbox``) failed "Show me the rent roll." on a Rentvine workspace: the live
``buildium_get_rent_roll`` is hidden there (``run_pms``), and no mirror tool
listed every active lease with its rent (gotcha 184).

Pinned here:

* the roll itself: every active lease, highest rent first, the count and the
  monthly total, and an UNREAD rent said as such, never as $0 (rule 14);
* it declines until the leases stage has landed, and claims no "owes nothing"
  until the balances stage has (gotcha 174);
* it is visible on Rentvine runs only, read through the SDK's own
  ``get_all_tools``; on a Buildium run the live rent roll answers, and no
  Buildium-run prompt names the hidden tool (a named tool gets called).
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

from helixis.agents import mirror_tools, run_pms
from helixis.agents.buildium_agent import create_buildium_agent
from helixis.agents.orchestrator import create_orchestrator
from helixis.db import pms_coverage
from helixis.db.pms_coverage import RENTVINE, SYNC_STATE_STAGE
from helixis.db.repositories import cockpit
from helixis.db.repositories.mirror_sync_state import StageSync
from helixis.settings import Settings

CO = "co-rv"
TODAY = date(2026, 10, 3)


def _on(monkeypatch: pytest.MonkeyPatch, pms: str, *, unlanded: str | None = None) -> None:
    async def _pms(_pool: Any, _company_id: str) -> str:
        return pms

    landed = {s: "2026-10-02T00:00:00Z" for s in SYNC_STATE_STAGE.values() if s != unlanded}
    monkeypatch.setattr(pms_coverage, "company_pms_strict", _pms)
    monkeypatch.setattr(pms_coverage, "get_freshness", AsyncMock(return_value=landed))


class _Conn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        return self.rows if "FROM helixis_leases l" in sql else []


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _Conn:
        return self.conn

    async def __aexit__(self, *_a: object) -> bool:
        return False


@contextmanager
def _tools(rows: list[dict[str, Any]], *, balances_landed: bool = True) -> Iterator[_Conn]:
    conn = _Conn(rows)
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _Acquire(conn))
    ctx = MagicMock()
    ctx.company_id = CO
    stamp = StageSync(synced_at=datetime.now(UTC), row_count=len(rows))
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=ctx),
        patch.object(mirror_tools, "get_pool", return_value=pool),
        patch.object(mirror_tools, "_freshness_line", AsyncMock(return_value="(as of now)")),
        patch.object(
            mirror_tools.sync_state,
            "get_stage_sync",
            AsyncMock(return_value=stamp if balances_landed else None),
        ),
        patch.object(mirror_tools, "company_timezone", AsyncMock(return_value="America/Denver")),
        patch.object(mirror_tools, "today_in", return_value=TODAY),
    ):
        yield conn


def _lease(lease_id: int, rent: float | None, **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "buildium_id": lease_id,
        "rent": rent,
        "lease_to_date": datetime(2027, 3, 18, tzinfo=UTC),
        "property_name": "Redbud",
        "unit_number": str(lease_id),
        "total_balance": None,
        "tenant_names": f"Tenant {lease_id}",
    }
    row.update(over)
    return row


# ── the roll ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_roll_lists_every_lease_with_the_count_and_the_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on(monkeypatch, RENTVINE)
    rows = [_lease(1, 1250.0, property_name="Maple Court"), _lease(2, 900.0)]
    with _tools(rows) as conn:
        out = await mirror_tools._rent_roll_impl()
    assert out.startswith("Rent roll: 2 active leases, $2,150 a month in rent:")
    assert "- Tenant 1 — Maple Court · Unit 1: $1,250/mo, lease ends 2027-03-18" in out
    assert "- Tenant 2 — Redbud · Unit 2: $900/mo" in out
    assert out.endswith("(as of now)")
    sql, args = conn.calls[0]
    assert "l.status = 'Active'" in sql and "ORDER BY l.rent DESC NULLS LAST" in sql
    assert args == (CO, "")


@pytest.mark.asyncio
async def test_an_unread_rent_is_never_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, RENTVINE)
    with _tools([_lease(1, 1250.0), _lease(2, None)]):
        out = await mirror_tools._rent_roll_impl()
    assert "$1,250 a month in rent across the 1 whose rent is known" in out
    assert "1 lease has no rent read yet, so the total leaves them out" in out
    assert "- Tenant 2 — Redbud · Unit 2: rent not read yet" in out
    assert "$0" not in out


@pytest.mark.asyncio
async def test_a_roll_with_no_rent_read_has_no_total(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every Rent Manager lease, until its recurring charges are read (RM-11):
    the header said "$0.00 a month in rent across the 0 whose rent is known"."""
    _on(monkeypatch, RENTVINE)
    with _tools([_lease(1, None), _lease(2, None)]):
        out = await mirror_tools._rent_roll_impl()
    assert out.startswith(
        "Rent roll: 2 active leases. No lease's rent has been read yet, so there is no rent total:"
    )
    assert "- Tenant 1 — Redbud · Unit 1: rent not read yet" in out
    assert "$0" not in out and "a month in rent" not in out


@pytest.mark.asyncio
async def test_a_long_roll_with_no_rent_read_ranks_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, RENTVINE)
    n = mirror_tools._RENT_ROLL_LIMIT + 5
    with _tools([_lease(i, None) for i in range(n, 0, -1)]):
        out = await mirror_tools._rent_roll_impl()
    assert f"Showing {mirror_tools._RENT_ROLL_LIMIT} of {n} active leases; the count above" in out
    assert "highest rents" not in out and "total above" not in out and "$0" not in out


@pytest.mark.asyncio
async def test_rent_is_read_to_the_cent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other mirror tools round to the dollar; a rent roll may not."""
    _on(monkeypatch, RENTVINE)
    with _tools([_lease(1, 1249.99, total_balance=10.5), _lease(2, 900.0)]):
        out = await mirror_tools._rent_roll_impl()
    assert "$2,149.99 a month in rent" in out
    assert "Unit 1: $1,249.99/mo, lease ends 2027-03-18, owes $10.50" in out


@pytest.mark.asyncio
async def test_a_lease_past_its_end_date_is_still_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """Measured 2026-10-03: 7 of the sandbox's 8 active Rentvine leases are past
    their end date, i.e. month to month. They are on the roll, said plainly."""
    _on(monkeypatch, RENTVINE)
    with _tools([_lease(1, 800.0, lease_to_date=datetime(2025, 6, 30, tzinfo=UTC))]):
        out = await mirror_tools._rent_roll_impl()
    assert "past its 2025-06-30 end date and still active" in out
    assert "expired" not in out


@pytest.mark.asyncio
async def test_a_balance_is_on_the_row_once_the_balances_landed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on(monkeypatch, RENTVINE)
    rows = [_lease(1, 1250.0, total_balance=300), _lease(2, 900.0, total_balance=0)]
    with _tools(rows):
        out = await mirror_tools._rent_roll_impl()
    assert "Unit 1: $1,250/mo, lease ends 2027-03-18, owes $300" in out
    assert "Unit 2: $900/mo, lease ends 2027-03-18\n" in out
    assert "i.e. it owes nothing" in out


@pytest.mark.asyncio
async def test_before_the_balances_land_nobody_is_cleared(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, RENTVINE)
    with _tools([_lease(1, 1250.0, total_balance=300)], balances_landed=False):
        out = await mirror_tools._rent_roll_impl()
    assert "owes" not in out
    assert "NOT the same as nothing overdue" in out and "rent balances" in out


@pytest.mark.asyncio
async def test_before_the_leases_land_the_roll_declines(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, RENTVINE, unlanded="leases")
    with _tools([_lease(1, 1250.0)]) as conn:
        out = await mirror_tools._rent_roll_impl()
    assert out.startswith("Occupella doesn't sync leases from Rentvine yet")
    assert conn.calls == []


@pytest.mark.asyncio
async def test_a_long_roll_says_what_it_left_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, RENTVINE)
    n = mirror_tools._RENT_ROLL_LIMIT + 5
    with _tools([_lease(i, 1000.0 + i) for i in range(n, 0, -1)]):
        out = await mirror_tools._rent_roll_impl()
    rows = [line for line in out.split("\n") if line.startswith("- ")]
    assert len(rows) == mirror_tools._RENT_ROLL_LIMIT
    assert f"Showing the {mirror_tools._RENT_ROLL_LIMIT} highest rents of {n} active leases" in out
    assert out.startswith(f"Rent roll: {n} active leases,")


@pytest.mark.asyncio
async def test_a_property_narrows_the_roll(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, RENTVINE)
    with _tools([_lease(1, 1250.0)]) as conn:
        out = await mirror_tools._rent_roll_impl("  Redbud ")
    assert out.startswith("Rent roll for Redbud: 1 active lease,")
    assert conn.calls[0][1] == (CO, "Redbud")
    with _tools([]):
        out = await mirror_tools._rent_roll_impl("Nowhere")
    assert out.startswith("No active lease at a property matching 'Nowhere'.")


@pytest.mark.asyncio
async def test_no_lease_at_all_is_said_by_what_the_mirror_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on(monkeypatch, RENTVINE)
    with (
        _tools([]),
        patch.object(cockpit, "list_property_summaries", AsyncMock(return_value=[object()])),
    ):
        out = await mirror_tools._rent_roll_impl()
    assert out == "No active leases. (as of now)"
    with (
        _tools([]),
        patch.object(cockpit, "list_property_summaries", AsyncMock(return_value=[])),
        patch.object(mirror_tools, "_not_synced", AsyncMock(return_value="NOT SYNCED")),
    ):
        assert await mirror_tools._rent_roll_impl() == "NOT SYNCED"


# ── who can see it ───────────────────────────────────────────────────────────


async def _names(agent: Any, pms: str | None) -> set[str]:
    token = run_pms.bind_run_pms(pms)
    try:
        return {t.name for t in await agent.get_all_tools(RunContextWrapper(context=None))}
    finally:
        run_pms.reset_run_pms(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("loop_mode", [False, True])
async def test_only_a_rentvine_run_sees_it(loop_mode: bool) -> None:
    """On the router and on the specialist. ``None`` is a failed PMS lookup,
    which hides it, the direction the live Buildium tools fail in too."""
    for agent in (
        create_orchestrator(Settings(), loop_mode=loop_mode),
        create_buildium_agent(Settings()),
    ):
        assert "rent_roll" in await _names(agent, RENTVINE), agent.name
        for pms in ("buildium", None):
            assert "rent_roll" not in await _names(agent, pms), (agent.name, pms)


@pytest.mark.parametrize("loop_mode", [False, True])
def test_no_buildium_run_prompt_names_it(loop_mode: bool) -> None:
    """A tool name in a prompt is advice the model follows: named and hidden,
    the call finds no tool and the turn dies (``run_pms.rentvine_wording``)."""
    orchestrator = create_orchestrator(Settings(), loop_mode=loop_mode)
    agents = [orchestrator, *(h for h in orchestrator.handoffs if hasattr(h, "instructions"))]
    agents.append(create_buildium_agent(Settings()))
    for agent in agents:
        assert not re.search(r"\brent_roll\b", str(agent.instructions)), agent.name
    # The Rentvine specialist's own section names it, so the check can fail.
    swapped = "\n".join(text for _, text in run_pms.RENTVINE_SECTIONS)
    assert re.search(r"\brent_roll\b", swapped)
