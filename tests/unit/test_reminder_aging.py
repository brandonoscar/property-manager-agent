"""An ignored reminder must get louder, and "handled" must not be forever.

WHAT THIS PINS, AND WHY IT IS SHAPED THIS WAY
---------------------------------------------
Two defects landed together because they are the same hole seen twice: a card
that nobody deals with had no way to say so.

* ``handled`` was a PERMANENT mute. ``0045_inbox_reminders.sql`` says in its
  own header that it is "suppressed for the producer's cooldown"; no cooldown
  existed, and ``handled_at`` was written by ``set_status`` and read by
  nothing. Gotcha 48 exactly — a comment cannot fail, so it went on describing
  a guarantee for a year.
* ``escalation`` came only from the entity's facts, never from the card's age,
  so a card raised this morning and one ignored for six weeks sorted
  identically. ``first_fired_at`` was on the row the whole time.

The arithmetic runs in POSTGRES, and this container has no database — so the
tests split deliberately:

1. the ladder as a pure function, both directions;
2. the SQL as something GENERATED from that same ladder, so the two cannot
   drift (gotcha 28's both-sides rule, with SQL as the far side) — a test that
   hand-wrote the expected CASE would just be a second definition;
3. the statement the repository actually executes, so a generator nothing
   calls fails (gotcha 62 — a behavioural test of a helper cannot see that no
   caller uses it).

⚠ **What this file does NOT prove** is that Postgres evaluates the expression
the way the ladder says. That was verified separately and behaviourally
against helixis-test inside a ``DO`` block that RAISES at the end, so it rolled
back by construction (gotchas 70/81); the result is recorded in CLAUDE.md. A
live-DB test would be the wrong guard here: CI has no Postgres service, so it
would skip forever and skipping reads as passing (gotcha 23).
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.db.repositories import reminders as reminders_repo
from helixis.db.repositories.reminders import ReminderCandidate
from helixis.reminders import aging

COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"


class _Conn:
    def __init__(self) -> None:
        self.execute = AsyncMock()


def _pool(conn: _Conn) -> MagicMock:
    @asynccontextmanager
    async def _acquire() -> Any:
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


async def _upsert_sql() -> str:
    """The statement the repository really sends for one candidate."""
    conn = _Conn()
    await reminders_repo.sync_producer(
        _pool(conn),
        company_id=COMPANY_ID,
        producer="bills_due",
        candidates=[ReminderCandidate(entity_key="bill:1", title="t")],
    )
    return str(conn.execute.await_args_list[0].args[0])


# ── the ladder itself ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("days_open", "expected"),
    [(0, 0), (6.9, 0), (7, 1), (7.1, 1), (20.9, 1), (21, 2), (400, 2)],
)
def test_the_ladder_fires_at_its_boundaries_and_not_before(days_open: float, expected: int) -> None:
    """Both directions. A ladder that only ever returns its top band would
    pass a one-sided test and make every card high on its first morning —
    the false-positive direction that gets a control switched off (gotcha 52).
    """
    assert aging.age_escalation(days_open) == expected


def test_the_ladder_is_ordered_highest_band_first() -> None:
    """``age_escalation`` returns the FIRST matching band, so a ladder written
    ascending would return 1 for a 400-day-old card and the function would go
    on looking correct. The order is load-bearing, not cosmetic."""
    days = [d for d, _ in aging.AGE_ESCALATION_LADDER]
    floors = [f for _, f in aging.AGE_ESCALATION_LADDER]
    assert days == sorted(days, reverse=True), "bands must descend by days"
    assert floors == sorted(floors, reverse=True), "bands must descend by floor"


def test_the_handled_cooldown_never_outlasts_the_longest_snooze() -> None:
    """The RELATION is the rule, not the number.

    Marking a card handled must not silence it for longer than the longest
    silence a user can explicitly ask for. Asserting equality would pin a
    coincidence; asserting the relation survives someone raising the snooze
    cap, which is the change that would otherwise uncouple them silently.
    """
    assert 0 < aging.HANDLED_COOLDOWN_DAYS <= aging.MAX_SNOOZE_DAYS


def test_the_route_takes_its_snooze_bound_from_the_one_definition() -> None:
    """Two copies of "30" would let the cooldown and the cap drift apart while
    both look right (gotcha 25 — the rule lives once)."""
    from helixis.api.routes import reminders as route

    assert route.MAX_SNOOZE_DAYS is aging.MAX_SNOOZE_DAYS
    # The metadata list carries one constraint object per annotation (Gt, Le,
    # …) in declaration order — index it and the test breaks on an unrelated
    # reordering, which reads as this rule failing.
    upper = [m.le for m in route.SnoozeIn.model_fields["days"].metadata if hasattr(m, "le")]
    assert upper == [aging.MAX_SNOOZE_DAYS], "the route's snooze ceiling is a second copy"


# ── the SQL is derived from the ladder, not restated beside it ───────


def test_every_band_reaches_the_sql_with_its_own_arm() -> None:
    sql = aging.age_escalation_sql(status_column="r.status", fired_column="r.first_fired_at")
    for days, floor in aging.AGE_ESCALATION_LADDER:
        assert f"interval '{days} days' THEN {floor}" in sql, f"band {days}d→{floor} missing"
    assert sql.count("WHEN") == len(aging.AGE_ESCALATION_LADDER) + 1  # + the cleared arm


def test_the_sql_arms_follow_the_ladder_when_it_changes(monkeypatch: Any) -> None:
    """Generation, not coincidence.

    A hand-written CASE would satisfy the test above forever while the Python
    ladder moved underneath it. Changing the tuple must change the SQL.
    """
    monkeypatch.setattr(aging, "AGE_ESCALATION_LADDER", ((99, 2), (3, 1)))
    sql = aging.age_escalation_sql(status_column="r.status", fired_column="r.first_fired_at")
    assert "interval '99 days' THEN 2" in sql
    assert "interval '3 days' THEN 1" in sql
    assert "interval '21 days'" not in sql, "the SQL is still quoting the old ladder"


def test_a_reborn_card_starts_its_age_clock_over() -> None:
    """THE ARM THAT IS INVISIBLE BY READING.

    Inside ``ON CONFLICT DO UPDATE`` a bare table reference reads the
    PRE-update row, so a card that self-cleared months ago and whose condition
    returns today still carries the old ``first_fired_at`` at the moment the
    ladder is evaluated. Without the ``cleared`` arm the new instance would be
    born at escalation 2, on its first morning, ignored by nobody — and the
    expression reads perfectly correct without it.
    """
    sql = aging.age_escalation_sql(status_column="r.status", fired_column="r.first_fired_at")
    first_arm = sql.split("WHEN", 2)[1]
    assert "r.status = 'cleared' THEN 0" in first_arm, (
        "the cleared arm must come FIRST — placed after an age band it never fires"
    )


def test_severity_is_raised_only_at_the_top_band_and_never_lowered() -> None:
    """Producers set severity and escalation INDEPENDENTLY (``bills_due``
    emits high/0), so a general mapping would invent a rule this codebase does
    not follow. The one defensible direction is upward at the top band."""
    expr = aging.age_severity_sql("AGE", producer_severity="EXCLUDED.severity")
    assert f"(AGE) >= {aging.HIGH_SEVERITY_ESCALATION}" in expr
    assert "THEN 'high'" in expr
    assert "ELSE EXCLUDED.severity" in expr
    assert "'low'" not in expr and "'normal'" not in expr, "severity must never be lowered"


# ── the statement the repository actually executes ───────────────────


def _clause(sql: str, start: str, end: str) -> str:
    """One assignment out of the DO UPDATE list.

    ⚠ Slicing is not fussiness. The ladder appears TWICE in the statement (it
    drives escalation and severity), so asserting its arms are "somewhere in
    the SQL" passes with the escalation half deleted — which is the entire
    change. A mutation battery caught exactly that: dropping the ladder from
    ``escalation`` left every test green.
    """
    assert start in sql, f"{start!r} is not in the statement at all"
    tail = sql.split(start, 1)[1]
    assert end in tail, f"{end!r} does not follow {start!r}"
    return tail.split(end, 1)[0]


@pytest.mark.asyncio
async def test_the_upsert_ages_the_escalation_itself() -> None:
    """A generator nothing calls is the ``comms_triage`` failure (gotcha 62).

    Scoped to the escalation assignment, not the whole statement — see
    :func:`_clause`.
    """
    clause = _clause(await _upsert_sql(), "escalation    = CASE", "last_fired_at")
    for days, floor in aging.AGE_ESCALATION_LADDER:
        assert f"interval '{days} days' THEN {floor}" in clause, (
            f"the {days}-day band does not reach the escalation the card carries"
        )
    assert "GREATEST(helixis_reminders.escalation" in clause, "the ratchet must survive"


@pytest.mark.asyncio
async def test_the_upsert_ages_the_severity_too() -> None:
    """The half that makes the ladder visible: ``list_active`` orders
    severity-first, so an escalation that never reaches severity leaves a
    stale card sitting exactly where it was."""
    clause = _clause(await _upsert_sql(), "severity      =", "title         =")
    for days, floor in aging.AGE_ESCALATION_LADDER:
        assert f"interval '{days} days' THEN {floor}" in clause
    assert "THEN 'high'" in clause


@pytest.mark.asyncio
async def test_the_upsert_expires_a_handled_card() -> None:
    """The whole point: ``handled`` is a claim with a shelf life now."""
    sql = await _upsert_sql()
    assert f"interval '{aging.HANDLED_COOLDOWN_DAYS} days'" in sql
    assert "helixis_reminders.handled_at" in sql, "the column that dates the claim is unread"
    assert "IN ('dismissed', 'handled')" not in sql, (
        "the old permanent-mute arm is back — handled would never expire again"
    )


@pytest.mark.asyncio
async def test_dismiss_stays_permanent() -> None:
    """The other direction, and it is a DECISION rather than an oversight:
    dismiss means "stop tracking this instance" and 0045 says so. Giving it a
    cooldown too would turn every deliberate silence into a recurring nag."""
    sql = await _upsert_sql()
    assert re.search(r"status\s*=\s*'dismissed'\s*\n?\s*THEN 'dismissed'", sql), (
        "dismissed must survive unconditionally"
    )
    assert "handled_at" not in sql.split("THEN 'dismissed'")[0].split("status = CASE")[-1], (
        "the dismissed arm must not depend on a date"
    )


@pytest.mark.asyncio
async def test_a_reborn_card_does_not_inherit_the_last_instances_urgency() -> None:
    """``first_fired_at`` already resets for a returning card; escalation had
    to follow it or the ratchet would be permanent across unrelated
    recurrences — a problem that happened in March making March's escalation
    the floor for the one in September.

    ⚠ Scoped to the arm BEFORE ``ELSE GREATEST``, not to the whole clause. The
    age ladder nested inside the GREATEST has a ``'cleared'`` arm of its own,
    so a looser assertion matched that instead and a mutation replacing this
    arm's condition with ``false`` stayed green — the test agreeing with a
    string it was not looking at.
    """
    sql = await _upsert_sql()
    arm = _clause(sql, "escalation    = CASE", "ELSE GREATEST")
    assert "helixis_reminders.status = 'cleared'" in arm, (
        "the reborn-card arm no longer tests for a cleared row"
    )
    assert "THEN EXCLUDED.escalation" in arm


@pytest.mark.asyncio
async def test_a_live_snooze_still_survives_a_content_refresh() -> None:
    """Unchanged behaviour, pinned because this change rewrote the CASE it
    lives in and a lost arm there would silently un-snooze every card."""
    sql = await _upsert_sql()
    assert "helixis_reminders.snoozed_until > now()" in sql
    assert "THEN 'snoozed'" in sql
