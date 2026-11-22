"""Sixteen copies of one sentence is a bug report, not an Inbox.

Measured on helixis-test 2026-08-30, a test workspace:
``bill_due`` held **16 open cards carrying ONE distinct title** --
``Redbud General Maintenance: $265.00 ... Due 2026-07-21. OVERDUE.`` sixteen
times. Sixteen real bills, one at each of sixteen properties (the recurring
gutter charge gotcha 53 records). Nothing duplicated, nothing wrong, and it
read as a glitch that buried everything else on the desk.

The producer's own docstring asserted the opposite premise -- *"one card per
bill (amounts and vendors differ; these are few and each is actionable)"* --
and a comment cannot fail (gotcha 48), so it went on being wrong until someone
counted. Grouping by vendor + due date takes that 16 to **1**, verified
against the live rows.

⚠ This is the THIRD producer to learn the same lesson. ``tasks_overdue``
groups per property "so a messy week is one card per building, not ten", and
``rent_missing`` was RETIRED for "41 open cards out of 67". The rule was
already the house rule; ``bill_due`` was the one still opting out.

⚠ Grouping is NOT sufficient on its own, which is why the cap exists too. The
other company in the same measurement held **55 open ``bill_due`` cards with
55 DISTINCT titles**; grouping collapses those to 50. A fix validated only
against Blue Ridge would have looked complete and left that desk untouched.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from helixis.reminders import producers


class _FakeConn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.sql: str | None = None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.sql = sql
        return self._rows

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakePool:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.conn = _FakeConn(rows)

    def acquire(self) -> _FakeConn:
        return self.conn


_YESTERDAY = dt.date.today() - dt.timedelta(days=40)


def _group(
    *, n: int, vendor: str = "Redbud General Maintenance", total: float | None = 4240.0, priced: int
) -> dict[str, Any]:
    return {
        "vendor_id": 229919,
        "vendor_name": vendor,
        "due_date": _YESTERDAY,
        "bill_count": n,
        "total_amount": total,
        "priced": priced,
        "bill_ids": list(range(1, n + 1)),
    }


@pytest.mark.asyncio
async def test_sixteen_bills_become_one_card() -> None:
    """The reported bug, with the live numbers."""
    out = await producers.bills_due(  # type: ignore[arg-type]
        _FakePool([_group(n=16, priced=16)]), "c", today=dt.date.today()
    )
    assert len(out) == 1
    card = out[0]
    assert "16 unpaid bills" in card.title
    assert "$4,240.00 total" in card.title
    assert "Redbud General Maintenance" in card.title
    assert card.payload["bill_count"] == 16
    assert len(card.payload["bill_ids"]) == 16


@pytest.mark.asyncio
async def test_the_card_never_calls_them_duplicates() -> None:
    """⚠ Same vendor + same amount + same due date is what a double-billed
    invoice looks like AND exactly what a per-property recurring charge looks
    like. ``unpaid_bills`` already refuses to call it on that signal alone
    (gotcha 53); a card that decided otherwise would be gotcha 41's confident
    false claim pointed at money."""
    out = await producers.bills_due(  # type: ignore[arg-type]
        _FakePool([_group(n=16, priced=16)]), "c", today=dt.date.today()
    )
    text = f"{out[0].title} {out[0].body}".lower()
    for verdict in ("duplicate", "duplicated", "double-billed", "billed twice"):
        assert verdict not in text, (
            f"the card asserts {verdict!r} on evidence that cannot support it"
        )


@pytest.mark.asyncio
async def test_a_single_bill_still_reads_like_a_single_bill() -> None:
    """⚠ "1 bill totalling $265" is a rollup wearing a costume. A group of one
    keeps the original wording exactly."""
    out = await producers.bills_due(  # type: ignore[arg-type]
        _FakePool([_group(n=1, total=265.0, priced=1)]), "c", today=dt.date.today()
    )
    assert len(out) == 1
    assert "1 unpaid bills" not in out[0].title
    assert "$265.00" in out[0].title
    assert out[0].payload["bill_id"] == 1


@pytest.mark.asyncio
async def test_an_unpriced_bill_is_named_not_folded_into_the_total() -> None:
    """SUM over a partly-NULL column returns the sum of the rest, silently.
    A total that quietly covers 12 of 16 bills is a plausible wrong number --
    gotcha 25's rule that a missing amount must never launder into a real one."""
    out = await producers.bills_due(  # type: ignore[arg-type]
        _FakePool([_group(n=16, total=3180.0, priced=12)]), "c", today=dt.date.today()
    )
    title = out[0].title
    assert "16 unpaid bills" in title
    assert "across 12 of them" in title
    assert "4 with no amount recorded" in title


@pytest.mark.asyncio
async def test_distinct_vendors_and_dates_stay_separate_cards() -> None:
    """The other direction, and the one that makes grouping safe: the 55-card
    company's bills are genuinely different and must NOT be merged into one
    card that hides 55 obligations behind a single line."""
    rows = [
        _group(n=1, vendor="Apex Plumbing", total=450.0, priced=1),
        _group(n=1, vendor="Volt & Vine", total=120.0, priced=1),
    ]
    rows[1]["vendor_id"] = 300000
    out = await producers.bills_due(_FakePool(rows), "c", today=dt.date.today())  # type: ignore[arg-type]
    assert len(out) == 2
    assert {c.entity_key for c in out} == {
        f"vendor:229919|due:{_YESTERDAY.isoformat()}",
        f"vendor:300000|due:{_YESTERDAY.isoformat()}",
    }


@pytest.mark.asyncio
async def test_the_query_still_groups_in_sql_and_keeps_the_int_cast() -> None:
    """Two things the rewrite could silently drop.

    ⚠ ``$2::int`` is load-bearing: without it Postgres cannot resolve
    ``date + unknown`` (42725), the whole query raises, and the sweep's
    per-producer catch logs "cards left as-is" -- which is how this producer
    emitted zero cards for its entire life without anyone noticing.
    """
    pool = _FakePool([])
    await producers.bills_due(pool, "c", today=dt.date.today())  # type: ignore[arg-type]
    sql = pool.conn.sql or ""
    assert "GROUP BY" in sql, "grouping moved out of SQL -- N rows are back to N cards"
    assert "$2::int" in sql, "the int cast is gone; this producer now raises silently"
