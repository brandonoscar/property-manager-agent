"""What the mirror can and cannot honestly say — battery v13's two slow losses.

Both questions cost ~150-180s, opened ``buildium_agent`` three-to-five times,
and were rescued by deadline synthesis. Neither failure was a loop the caps
could stop; each was a rollup that returned half a question's answer.

**q05 "Which tenants are completely paid up?"** — ``delinquency_report``
returns only leases that HAVE a balance, so the paid-up set was unreachable
from it. The model called the rollup, got 20 rows, then enumerated the rest
through the specialist. Blue Ridge: 50 active leases, 20 owing → 30 current,
one query on data already in the mirror.

**q06 "When did we last pay Volt & Vine Electric?"** — a different shape
entirely, and the reason this file is not just "add another rollup". Live-
verified 2026-08-28: **0 of Blue Ridge's 67 mirrored bills are marked paid.**
The sync only lands unpaid ones, which is what gotcha 14 predicts. A
``vendor_payments`` rollup built on that data would answer "no payments on
record" for every vendor — a FABRICATED ABSENCE, this product's worst failure
mode (gotchas 26/55). The honest fix is to say the mirror cannot answer it.

⚠ Every assertion here has a partner in the opposite direction. A caveat
emitted unconditionally is as useless as no caveat (gotcha 53's tie note, and
gotcha 38's both-directions rule).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.agents import mirror_tools


def _pool(*, list_rows: list, scalar: dict | None) -> MagicMock:
    """A pool whose ``fetch`` returns the rollup rows and whose ``fetchrow``
    DISPATCHES ON THE QUERY.

    ⚠ A single ``return_value`` cannot exercise these tools. Each runs two
    different ``fetchrow`` queries — the count helper's, and ``_latest_updated``'s
    freshness stamp — and answering both with one dict makes the second raise
    ``KeyError: 'latest'`` from inside library code, which reads as a product
    bug rather than as a wrong fixture. Same lesson as #935's per-call fetch
    sequence, arriving through a different door.

    ``scalar=None`` models a count that could not be read at all; the
    freshness query still answers, because that is the real shape — one query
    failing does not fail the other.
    """

    async def _fetchrow(sql: str, *args, **kwargs):
        if "MAX(" in sql or "GREATEST" in sql:
            return {"latest": None}
        return scalar

    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=list_rows)
    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _balance_row(**over) -> dict:
    row = {
        "lease_id": 1,
        "total_balance": 1200,
        "balance_0_30": 0,
        "balance_31_60": 0,
        "balance_61_90": 0,
        "balance_over_90": 0,
        "snapshot_at": datetime(2026, 8, 28, tzinfo=UTC),
        "property_name": "AK — 120 4th St",
        "unit_number": "1",
        "open_work_orders": 0,
        "open_tasks": 0,
        "tenant_names": "Ada Lovelace",
    }
    row.update(over)
    return row


def _bill_row(**over) -> dict:
    row = {
        "buildium_id": 7,
        "due_date": date(2026, 9, 1),
        "reference_number": None,
        "total_amount": 265,
        "vendor_name": "Redbud General Maintenance",
        "memo": "gutter cleaning",
        "properties": "AK — 120 4th St",
    }
    row.update(over)
    return row


# ── q05: the paid-up side ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delinquency_states_how_many_leases_are_current(monkeypatch) -> None:
    """The count that stops the enumeration. Without it the model has the
    owing set and no way to reach its complement but row-by-row."""
    monkeypatch.setattr(mirror_tools, "_company_id", lambda: "co-1")
    monkeypatch.setattr(
        mirror_tools, "get_pool", lambda: _pool(list_rows=[_balance_row()], scalar={"active": 50})
    )
    out = await mirror_tools._delinquency_report_impl()
    assert "49 other active lease(s) carry NO outstanding balance" in out, out
    assert "do NOT enumerate" in out, out


@pytest.mark.asyncio
async def test_the_count_is_omitted_when_it_cannot_be_read(monkeypatch) -> None:
    """⚠ The decline-to-assert direction. A failed count must produce NO
    sentence — "0 tenants are paid up" would be a fabricated absence, and it
    is exactly the claim a naive ``or 0`` default would make."""
    monkeypatch.setattr(mirror_tools, "_company_id", lambda: "co-1")
    monkeypatch.setattr(
        mirror_tools, "get_pool", lambda: _pool(list_rows=[_balance_row()], scalar=None)
    )
    out = await mirror_tools._delinquency_report_impl()
    assert "carry NO outstanding balance" not in out, out
    # The rest of the report is unaffected — a missing extra never costs the answer.
    assert "Total outstanding:" in out, out


@pytest.mark.asyncio
async def test_more_owing_than_active_states_nothing_rather_than_a_negative(
    monkeypatch,
) -> None:
    """Balance rows attached to leases this count cannot see. A negative
    remainder means the arithmetic is wrong, and a confidently wrong number is
    worse than no number (gotcha 41)."""
    monkeypatch.setattr(mirror_tools, "_company_id", lambda: "co-1")
    rows = [_balance_row(lease_id=i) for i in range(5)]
    monkeypatch.setattr(
        mirror_tools, "get_pool", lambda: _pool(list_rows=rows, scalar={"active": 2})
    )
    out = await mirror_tools._delinquency_report_impl()
    assert "carry NO outstanding balance" not in out, out
    assert "-3" not in out, out


@pytest.mark.asyncio
async def test_a_null_count_column_is_not_read_as_zero_paid_up(monkeypatch) -> None:
    """The row comes back but the count is NULL — a distinct shape from "no
    row at all", and the one the ``.get`` path actually handles. It must reach
    the same silence, never "0 tenants are paid up"."""
    monkeypatch.setattr(mirror_tools, "_company_id", lambda: "co-1")
    monkeypatch.setattr(
        mirror_tools,
        "get_pool",
        lambda: _pool(list_rows=[_balance_row()], scalar={"active": None}),
    )
    out = await mirror_tools._delinquency_report_impl()
    assert "carry NO outstanding balance" not in out, out


@pytest.mark.asyncio
async def test_everyone_owing_states_zero_rather_than_going_silent(monkeypatch) -> None:
    """⚠ Zero is an ANSWER here, not a failure. "0 other active leases carry no
    balance" tells the model nobody is paid up — which is exactly what q05
    asked. Suppressing it would send the model back to enumerating to discover
    a fact the rollup already held."""
    monkeypatch.setattr(mirror_tools, "_company_id", lambda: "co-1")
    rows = [_balance_row(lease_id=i) for i in range(3)]
    monkeypatch.setattr(
        mirror_tools, "get_pool", lambda: _pool(list_rows=rows, scalar={"active": 3})
    )
    out = await mirror_tools._delinquency_report_impl()
    assert "0 other active lease(s) carry NO outstanding balance" in out, out


def test_the_paid_up_count_is_active_leases_only() -> None:
    """⚠ Counting ALL leases would include ended ones and report former
    tenants as 'paid up' — gotcha 51's false-tense claim arriving through
    arithmetic instead of through a renderer. Asserted on the SQL because the
    filter is the whole correctness argument."""
    import inspect

    sql = inspect.getsource(mirror_tools._current_lease_count)
    assert "helixis_leases" in sql
    assert "'active'" in sql, "the count must be restricted to active leases"
    assert "company_id = $1::uuid" in sql, "gotcha 47: every helixis_* read is scoped"


# ── q06: the mirror holds no payment history ─────────────────────────────


@pytest.mark.asyncio
async def test_unpaid_bills_says_payment_history_is_not_mirrored(monkeypatch) -> None:
    """The gap notice. Its job is to stop five specialist consults from being
    spent rediscovering that the data was never synced."""
    monkeypatch.setattr(mirror_tools, "_company_id", lambda: "co-1")
    monkeypatch.setattr(
        mirror_tools, "get_pool", lambda: _pool(list_rows=[_bill_row()], scalar={"paid": 0})
    )
    out = await mirror_tools._unpaid_bills_impl()
    assert "PAYMENT HISTORY IS NOT MIRRORED" in out, out
    # It must say what an empty result does NOT mean — that is the half that
    # prevents a fabricated absence, not the half that names the gap.
    assert "NOT evidence that a vendor was never paid" in out, out


@pytest.mark.asyncio
async def test_the_notice_disappears_once_paid_bills_are_synced(monkeypatch) -> None:
    """⚠ The other direction, and the reason the count is computed rather
    than hardcoded. The day the sync lands paid bills the caveat is false, and
    a false caveat trains the model to discount true ones."""
    monkeypatch.setattr(mirror_tools, "_company_id", lambda: "co-1")
    monkeypatch.setattr(
        mirror_tools, "get_pool", lambda: _pool(list_rows=[_bill_row()], scalar={"paid": 12})
    )
    out = await mirror_tools._unpaid_bills_impl()
    assert "PAYMENT HISTORY IS NOT MIRRORED" not in out, out


@pytest.mark.asyncio
async def test_an_unreadable_paid_count_makes_no_claim_either_way(monkeypatch) -> None:
    """Unknown is not zero. It must not produce the gap notice."""
    monkeypatch.setattr(mirror_tools, "_company_id", lambda: "co-1")
    monkeypatch.setattr(
        mirror_tools, "get_pool", lambda: _pool(list_rows=[_bill_row()], scalar=None)
    )
    out = await mirror_tools._unpaid_bills_impl()
    assert "PAYMENT HISTORY IS NOT MIRRORED" not in out, out


@pytest.mark.asyncio
async def test_no_unpaid_bills_does_not_claim_everything_is_paid(monkeypatch) -> None:
    """⚠ THE SUBTLE ONE. "No unpaid bills — everything is paid" is a claim
    about the PAID set, made by a tool that only reads the unpaid one. With
    zero paid bills on file it is flatly false: an empty result means nothing
    unpaid is recorded, which is a different sentence."""
    monkeypatch.setattr(mirror_tools, "_company_id", lambda: "co-1")
    monkeypatch.setattr(mirror_tools, "get_pool", lambda: _pool(list_rows=[], scalar={"paid": 0}))
    monkeypatch.setattr(
        mirror_tools.cockpit, "list_property_summaries", AsyncMock(return_value=[{"x": 1}])
    )
    out = await mirror_tools._unpaid_bills_impl()
    assert "everything is paid" not in out, out
    assert "not\nevidence" in out.replace(" ", "\n") or "not evidence" in out, out


@pytest.mark.asyncio
async def test_no_unpaid_bills_with_a_real_paid_history_still_reads_plainly(
    monkeypatch,
) -> None:
    """The other direction again — a company that genuinely has paid bills and
    no unpaid ones gets the ordinary sentence, not a hedge."""
    monkeypatch.setattr(mirror_tools, "_company_id", lambda: "co-1")
    monkeypatch.setattr(mirror_tools, "get_pool", lambda: _pool(list_rows=[], scalar={"paid": 40}))
    monkeypatch.setattr(
        mirror_tools.cockpit, "list_property_summaries", AsyncMock(return_value=[{"x": 1}])
    )
    out = await mirror_tools._unpaid_bills_impl()
    assert "everything is paid" in out, out


# ── the analyst's schema notes ───────────────────────────────────────────
#
# ``describe_mirror_schema`` returned a bare column inventory, which reads as
# though every column means what it is called. Two do not, and the analyst
# writes its own SQL, so no code change can stop it reaching for them — only
# knowing can, which is why the note ships WITH the schema.
#
# Live-measured 2026-08-28, Blue Ridge only: leases, bills, tasks, tenants,
# properties, work orders and lease transactions ALL hold exactly ONE distinct
# ``updated_at`` date across every row. A "what changed this week" query over
# that column returns the whole table or nothing, depending only on which side
# of the last sync the window falls.


def test_the_schema_notes_name_what_the_timestamps_actually_are() -> None:
    from helixis.agents.analysis_tools import _SCHEMA_NOTES

    joined = " ".join(_SCHEMA_NOTES).lower()
    assert "sync timestamp" in joined
    assert "created_at" in joined and "updated_at" in joined
    assert "what changed" in joined, "the note must name the question it forbids"


def test_the_notes_point_somewhere_real_and_not_only_at_a_prohibition() -> None:
    """⚠ A prohibition with no permitted substitute is an instruction to
    improvise — gotcha 52's finding, one domain over. Every column named here
    is Buildium-sourced and was confirmed present in information_schema."""
    from helixis.agents.analysis_tools import _SCHEMA_NOTES

    joined = " ".join(_SCHEMA_NOTES)
    for real in ("bill_date", "due_date", "transaction_date", "lease_to_date", "move_in_date"):
        assert real in joined, f"the note never points at {real}"


def test_the_named_safe_columns_actually_exist_in_the_allowlisted_tables() -> None:
    """⚠ Reads the SCHEMA MODULE's own allowlist, not a list typed here
    (gotcha 45). A note naming a column that has been renamed is worse than no
    note: the analyst trusts it and writes a query that errors, or worse,
    silently reaches for the timestamp it was steered away from.
    """
    import re

    from helixis.agents.analysis_tools import _SCHEMA_NOTES, ALLOWED_TABLES

    joined = " ".join(_SCHEMA_NOTES)
    referenced = set(re.findall(r"\bhelixis_[a-z_]+", joined))
    assert referenced, "the notes reference no table at all"
    unknown = referenced - set(ALLOWED_TABLES)
    assert not unknown, f"the notes name tables the analyst cannot read: {sorted(unknown)}"


def test_the_paid_bill_gap_is_stated_where_the_analyst_will_see_it() -> None:
    """The same gap ``unpaid_bills`` states for the chat path (gotcha 57). The
    analyst reaches helixis_bills directly and would otherwise return an empty
    result for "when did we last pay X" — a fabricated absence."""
    from helixis.agents.analysis_tools import _SCHEMA_NOTES

    joined = " ".join(_SCHEMA_NOTES).lower()
    # Re-pointed 2026-10-07: was ``"unpaid" in joined``, which leaned on the
    # note's old claim that the table holds unpaid bills only (false since
    # Buildium's backfill reads paid bills too). The gap itself is the claim.
    assert "helixis_bills" in joined
    assert "when did we last pay" in joined and "unanswerable" in joined
