"""``expiring_leases`` lists an active lease already past its end date.

The window started at ``now()``, so an Active lease whose end date had passed
(running month to month, or a renewal never recorded) fell out of the query, and
an empty result said "No active leases expire". Measured 2026-10-03: 7 of the
Rentvine sandbox's 8 active leases are in that state, and Buildium's query was
the same (gotcha 189). Those leases are renewal decisions that are already due,
so they are listed apart, most recently ended first.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from helixis.agents import mirror_tools
from helixis.db import pms_coverage
from helixis.db.pms_coverage import RENTVINE, SYNC_STATE_STAGE

from .test_mirror_tools import _ctx, _expiring_row, _pool, _summary


@pytest.fixture(autouse=True)
def _balances_stage_landed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The balances stage has synced, as in ``test_mirror_tools`` (whose
    autouse fixture does not travel with the helpers imported from it)."""
    stamped = mirror_tools.sync_state.StageSync(synced_at=datetime.now(UTC), row_count=1)
    monkeypatch.setattr(mirror_tools.sync_state, "get_stage_sync", AsyncMock(return_value=stamped))


def _lapsed(lease_id: int, ended: datetime, **over: Any) -> dict[str, Any]:
    return _expiring_row(buildium_id=lease_id, lease_to_date=ended, past_end=True, **over)


def _upcoming(lease_id: int, ends: datetime) -> dict[str, Any]:
    return _expiring_row(buildium_id=lease_id, lease_to_date=ends, past_end=False)


async def _run(rows: list[dict[str, Any]], days: int = 90) -> tuple[str, Any]:
    pool = _pool(fetch_rows=rows)
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=pool),
        patch.object(mirror_tools, "_freshness_line", AsyncMock(return_value="(synced)")),
        patch.object(
            mirror_tools.cockpit, "list_property_summaries", AsyncMock(return_value=[_summary()])
        ),
    ):
        out = await mirror_tools._expiring_leases_impl(days)
    conn = pool.acquire.side_effect()._conn
    return out, conn


@pytest.mark.asyncio
async def test_the_query_keeps_leases_already_past_their_end() -> None:
    _out, conn = await _run([])
    sql, company, days = conn.fetch.await_args.args
    flat = " ".join(sql.split())
    assert "l.status = 'Active'" in flat
    assert "AND l.lease_to_date < now() + make_interval(days => $2)" in flat
    assert "(l.lease_to_date < now()) AS past_end" in flat
    assert "BETWEEN now()" not in flat
    assert (company, days) == ("co-1", 90)


@pytest.mark.asyncio
async def test_lapsed_leases_are_listed_apart_most_recent_first() -> None:
    rows = [
        _lapsed(1, datetime(2025, 6, 30, tzinfo=UTC), tenant_names="Avery Older"),
        _lapsed(2, datetime(2026, 1, 31, tzinfo=UTC), tenant_names="Riley Newer"),
        _upcoming(3, datetime(2026, 11, 14, tzinfo=UTC)),
    ]
    out, _conn = await _run(rows)
    lines = out.split("\n")
    assert lines[0] == "Leases expiring in the next 90 days:"
    assert "expires 2026-11-14" in lines[1]
    assert lines[2].startswith("2 active leases are already past their end date")
    assert "NOT expired: Buildium still has each one active" in lines[2]
    assert lines[3].startswith("- Riley Newer") and "ended 2026-01-31, still active" in lines[3]
    assert lines[4].startswith("- Avery Older") and "ended 2025-06-30, still active" in lines[4]
    # The row keeps everything a renewal call needs (gotcha 41: on the row).
    for needed in ("owes $640", "2 open maintenance items", "$1,050/mo"):
        assert needed in lines[3], (needed, lines[3])
    assert "NOT look" in out


@pytest.mark.asyncio
async def test_only_lapsed_leases_never_reads_as_nothing_expiring() -> None:
    out, _conn = await _run([_lapsed(1, datetime(2025, 6, 30, tzinfo=UTC))])
    first, second = out.split("\n")[:2]
    assert first == "No active leases expire in the next 90 days."
    assert second.startswith("1 active lease is already past its end date")
    assert "ended 2025-06-30, still active" in out


@pytest.mark.asyncio
async def test_a_long_lapsed_list_says_what_it_left_off() -> None:
    n = mirror_tools._PAST_END_LIMIT + 5
    rows = [_lapsed(i, datetime(2025, 1, 1 + i, tzinfo=UTC)) for i in range(n)]
    out, _conn = await _run(rows)
    listed = [line for line in out.split("\n") if "still active" in line and line.startswith("- ")]
    assert len(listed) == mirror_tools._PAST_END_LIMIT
    assert f"{n} active leases are already past their end date" in out
    limit = mirror_tools._PAST_END_LIMIT
    assert f"Showing the {limit} most recently ended of {n} leases" in out
    assert "2025-01-30" in listed[0]  # the newest first


@pytest.mark.asyncio
async def test_the_note_names_the_companys_own_system(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _rentvine(_pool: Any, _company_id: str) -> str:
        return RENTVINE

    landed = {stage: "2026-10-02T00:00:00Z" for stage in SYNC_STATE_STAGE.values()}
    monkeypatch.setattr(pms_coverage, "company_pms_strict", _rentvine)
    monkeypatch.setattr(pms_coverage, "get_freshness", AsyncMock(return_value=landed))
    out, _conn = await _run([_lapsed(1, datetime(2025, 6, 30, tzinfo=UTC))])
    assert "NOT expired: Rentvine still has each one active" in out


@pytest.mark.asyncio
async def test_nothing_at_all_is_still_said_plainly() -> None:
    out, _conn = await _run([])
    assert out == "No active leases expire in the next 90 days. (synced)"


# ── a deposit nobody sent is not "no deposit" ────────────────────────────────


@pytest.mark.asyncio
async def test_an_unknown_deposit_is_said_never_implied_as_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rentvine documents no per-lease deposit, so every Rentvine lease holds
    NULL (rentvine/mapper.py). The footer used to say each row "already
    carries ... its lease deposit" over rows that showed none, which reads as
    "no deposit": unknown rendered as none (rule 14)."""

    async def _rentvine(_pool: Any, _company_id: str) -> str:
        return RENTVINE

    landed = {stage: "2026-10-02T00:00:00Z" for stage in SYNC_STATE_STAGE.values()}
    monkeypatch.setattr(pms_coverage, "company_pms_strict", _rentvine)
    monkeypatch.setattr(pms_coverage, "get_freshness", AsyncMock(return_value=landed))
    rows = [
        _upcoming(1, datetime(2026, 11, 14, tzinfo=UTC)),
        _lapsed(2, datetime(2026, 1, 31, tzinfo=UTC)),
    ]
    for row in rows:
        row["security_deposit"] = None
    out, _conn = await _run(rows)
    assert "its lease deposit" not in out
    assert "deposit" not in " ".join(line for line in out.split("\n") if line.startswith("- "))
    assert (
        "Occupella has no security deposit on file for these leases, so none is shown. "
        "That is NOT the same as no deposit: check it in Rentvine."
    ) in out
    # The rest of the footer still holds.
    assert "Each row already carries its tenant, what they owe, and its open-maintenance" in out


@pytest.mark.asyncio
async def test_a_known_deposit_keeps_the_claim_and_needs_no_caveat() -> None:
    out, _conn = await _run([_upcoming(1, datetime(2026, 11, 14, tzinfo=UTC))])
    assert "deposit $1,050" in out
    assert "Each row already carries its tenant, what they owe, its lease deposit and" in out
    assert "no security deposit on file" not in out
