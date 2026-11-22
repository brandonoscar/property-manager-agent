"""Vendor analytics existed for the Operations page and not for the agent.

``list_vendor_ops`` computes 12-month billing, work-order volume, 90-day
activity and average completion time per vendor. It was reachable only from
the Operations REST route, so "who do we use most", "which vendor bills us the
most" and "who should I call for plumbing" had no rollup — the model either
guessed or hydrated vendor by vendor, which is the N+1 that gotchas 25/32/34/37
took four PRs to bound.

Two things the exposed rollup must NOT say, both measured 2026-08-29 and both
the confident-false-claim shape this codebase keeps producing:

⚠ **"Spend" is the wrong word.** 0 of Blue Ridge's 67 mirrored bills are marked
paid — the sync can only fetch unpaid ones (gotchas 14/57) — so the 12-month
figure is what a vendor INVOICED. "We spent $15,300 with Apex" is false, and
it is false about money.

⚠ **There are no completion times at all.** ``helixis_work_order_status_history``
holds 0 rows for Blue Ridge and 3 across every company, so
``avg_days_to_complete`` is NULL for essentially every vendor. A blank there
invites "this vendor is slow"; the rollup has to say the data does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from helixis.agents import mirror_tools
from helixis.agents.mirror_tools import _vendor_performance_impl


@dataclass
class _V:
    buildium_id: int = 1
    name: str = "Apex Plumbing Co"
    category_name: str | None = "Plumbing"
    is_active: bool | None = True
    phone: str | None = None
    email: str | None = None
    open_work_orders: int = 3
    total_work_orders: int = 18
    jobs_90d: int = 4
    avg_days_to_complete: float | None = None
    spend_12m: float = 15300.0
    bill_count_12m: int = 34
    last_bill_date: Any = None


async def _run(rows: list[_V] | Exception) -> str:
    loader = (
        AsyncMock(side_effect=rows) if isinstance(rows, Exception) else AsyncMock(return_value=rows)
    )
    with (
        patch.object(mirror_tools, "_company_id", return_value="co-1"),
        patch.object(mirror_tools, "get_pool", return_value=object()),
        patch.object(mirror_tools, "_freshness_line", AsyncMock(return_value="(synced)")),
        patch("helixis.db.repositories.operations.list_vendor_ops", loader),
    ):
        return await _vendor_performance_impl()


# ── the two claims it must not make ──────────────────────────────────


@pytest.mark.asyncio
async def test_amounts_are_called_billed_never_spent() -> None:
    """⚠ 0 of 67 bills are paid. Calling an invoice total "spend" is a false
    claim about money, and it is the claim a vendor-ranking question invites."""
    out = await _run([_V()])
    assert "BILLED, not PAID" in out
    assert "do NOT describe them as money spent" in out
    # ⚠ The ROW, not just the footer. A footer caveat passed this test while
    # the row read "$15,300 spent across 34 bills" — and a model reading a row
    # that says "spent" under a footer that says "billed" goes with the row.
    # Mutation caught it; the footer assertions alone could not.
    row = next(x for x in out.splitlines() if "Apex" in x)
    assert "billed" in row, f"the row itself must say billed — {row!r}"
    assert "spent" not in row.lower(), f"the row claims money was spent — {row!r}"


@pytest.mark.asyncio
async def test_missing_completion_times_are_declared_not_left_blank() -> None:
    """⚠ Zero status-history rows exist, so the average is NULL for every
    vendor. A silent blank is what the model turns into "Apex is slow"."""
    out = await _run([_V(avg_days_to_complete=None), _V(buildium_id=2, name="Volt")])
    assert "No completion times are on file" in out
    assert "NOT evidence a vendor is slow" in out


@pytest.mark.asyncio
async def test_the_completion_caveat_is_computed_not_hardcoded() -> None:
    """Both directions (gotcha 57). The day status webhooks start landing, a
    caveat saying the data does not exist becomes false — and a false caveat
    teaches the model to discount the true ones."""
    out = await _run([_V(avg_days_to_complete=4.5)])
    assert "No completion times are on file" not in out
    assert "avg 4.5 days" in out


@pytest.mark.asyncio
async def test_one_vendor_with_times_suppresses_the_caveat() -> None:
    """The bar is "any data at all", not "every vendor". Saying times do not
    exist while showing one would contradict the row above it."""
    out = await _run([_V(avg_days_to_complete=None), _V(buildium_id=2, avg_days_to_complete=2.0)])
    assert "No completion times are on file" not in out


# ── the row carries what the question needs ──────────────────────────


@pytest.mark.asyncio
async def test_a_row_carries_billing_and_volume_together() -> None:
    """The join IS the question ("who costs most and how much work do they
    do"). Leaving either off is what sends the model hydrating per vendor —
    gotcha 50, stated as a rule rather than rediscovered."""
    out = await _run([_V()])
    line = next(x for x in out.splitlines() if "Apex" in x)
    for needed in ("15,300", "34 bills", "18 work orders", "3 open", "Plumbing"):
        assert needed in line, f"{needed!r} missing from the row — {line!r}"


@pytest.mark.asyncio
async def test_an_inactive_vendor_is_marked_loudly() -> None:
    """⚠ Uppercase for gotcha 51's reason: a lowercase mid-row qualifier is
    what gets smoothed away in a summary, and recommending a retired vendor
    for a live job is a real error with a real callout attached."""
    out = await _run([_V(is_active=False)])
    assert "INACTIVE" in out


@pytest.mark.asyncio
async def test_an_active_vendor_is_not_marked() -> None:
    """Both directions — flagging everything makes the flag meaningless."""
    assert "INACTIVE" not in await _run([_V(is_active=True)])


@pytest.mark.asyncio
async def test_it_tells_the_model_not_to_re_query_per_vendor() -> None:
    out = await _run([_V()])
    assert "do NOT look these up per vendor" in out


# ── absence is never fabricated ──────────────────────────────────────


@pytest.mark.asyncio
async def test_no_vendors_is_reported_as_a_sync_fact() -> None:
    """ "No vendors" must read as "none have synced", not as "this company
    uses no vendors" — the fabricated-absence failure (gotchas 26/55)."""
    out = await _run([])
    assert "not evidence the company uses none" in out


@pytest.mark.asyncio
async def test_no_vendors_names_the_company_s_own_system(monkeypatch: pytest.MonkeyPatch) -> None:
    """It said "none have synced from Buildium" to every company."""
    from helixis.db.pms_coverage import Coverage

    async def on(_pool: Any, _company_id: str) -> Coverage:
        return Coverage("propertyware")

    monkeypatch.setattr(mirror_tools, "coverage_for", on)
    out = await _run([])
    assert "none have synced from Propertyware" in out
    assert "Buildium" not in out


@pytest.mark.asyncio
async def test_a_query_failure_does_not_report_an_empty_book() -> None:
    """Our error is never their emptiness (gotcha 42)."""
    out = await _run(RuntimeError("db down"))
    assert "error on OUR side" in out
    assert "Do NOT report the company as having no vendors" in out


# ── it is wired into the budget registries ───────────────────────────


def test_the_rollup_is_registered_and_memoized() -> None:
    """⚠ A mirror read missing from ``_MIRROR_READS`` has no memo, no cap and
    no soft-deadline channel — it silently re-opens the loop the call budget
    exists to close. The registry test caught this one on the first run."""
    from helixis.agents.call_budget import _MIRROR_READS
    from helixis.agents.mirror_tools import mirror_tools

    assert "vendor_performance" in {t.name for t in mirror_tools}
    assert "vendor_performance" in _MIRROR_READS
