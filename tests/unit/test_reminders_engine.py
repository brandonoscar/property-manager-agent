"""Inbox reminders engine (migration 0045) — repo lifecycle, producers, sweep.

Pins the design contract from the 2026-07-06 discussion: one card per
(producer, entity_key) with in-place escalation; dismissed/handled rows
stay hidden while their condition persists; snoozes survive content
refreshes until due; self-clear runs from ANY status; a failing producer
leaves its existing cards untouched (never wipes the desk); the neglect
gates live in the producers' SQL.
"""

from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.db.repositories import reminders as reminders_repo
from helixis.db.repositories.reminders import ReminderCandidate
from helixis.reminders import producers, sweep

COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"


@pytest.fixture(autouse=True)
def _a_buildium_company(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests describe a Buildium company, which fills every mirror table.

    The reminder sweep resolves the company's PMS strictly before running anything;
    the fake pools here were written before that lookup existed and cannot
    answer it. Stating the PMS is what keeps them testing what they were
    written to test.
    """
    from helixis.reminders import sweep

    monkeypatch.setattr(sweep, "company_pms_strict", AsyncMock(return_value="buildium"))


class _Conn:
    def __init__(self, fetch_rows: list | None = None, fetchrow_result: Any = None) -> None:
        self.execute = AsyncMock()
        self.fetch = AsyncMock(return_value=fetch_rows or [])
        self.fetchrow = AsyncMock(return_value=fetchrow_result)


def _pool(conn: _Conn) -> MagicMock:
    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


# ── repo lifecycle ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_upserts_candidates_and_clears_the_rest() -> None:
    conn = _Conn()
    await reminders_repo.sync_producer(
        _pool(conn),
        company_id=COMPANY_ID,
        producer="bill_due",
        candidates=[ReminderCandidate(entity_key="bill:1", title="t1")],
    )
    calls = conn.execute.await_args_list
    assert len(calls) == 2  # one upsert + one clear
    upsert_sql = calls[0].args[0]
    # Dismissed stays hidden while the condition persists. ⚠ This used to read
    # `IN ('dismissed', 'handled')` — the two shared an arm, which is what made
    # `handled` a permanent mute despite 0045's comment calling it a cooldown.
    # They are separate arms now and the split is the point: dismiss is
    # unconditional, handled expires. Pinned in detail in test_reminder_aging.
    assert "THEN 'dismissed'" in upsert_sql
    assert "helixis_reminders.handled_at" in upsert_sql
    # A live snooze survives a content refresh.
    assert "snoozed_until > now()" in upsert_sql
    # Escalation only ever ratchets up — no downgrade churn.
    assert "GREATEST(helixis_reminders.escalation" in upsert_sql
    clear_sql, clear_args = calls[1].args[0], calls[1].args[1:]
    assert "SET status = 'cleared'" in clear_sql
    assert "status != 'cleared'" in clear_sql  # clears from ANY live status
    assert clear_args[-1] == ["bill:1"]


@pytest.mark.asyncio
async def test_sync_with_no_candidates_still_clears() -> None:
    """A producer whose condition stopped everywhere drains its cards."""
    conn = _Conn()
    await reminders_repo.sync_producer(
        _pool(conn), company_id=COMPANY_ID, producer="bill_due", candidates=[]
    )
    calls = conn.execute.await_args_list
    assert len(calls) == 1
    assert "SET status = 'cleared'" in calls[0].args[0]
    assert calls[0].args[-1] == []


@pytest.mark.asyncio
async def test_set_status_validates_and_requires_snooze_until() -> None:
    with pytest.raises(ValueError):
        await reminders_repo.set_status(
            _pool(_Conn()), company_id=COMPANY_ID, reminder_id="x", status="cleared"
        )
    with pytest.raises(ValueError):
        await reminders_repo.set_status(
            _pool(_Conn()), company_id=COMPANY_ID, reminder_id="x", status="snoozed"
        )


@pytest.mark.asyncio
async def test_list_active_includes_expired_snoozes() -> None:
    conn = _Conn(fetch_rows=[])
    await reminders_repo.list_active(_pool(conn), company_id=COMPANY_ID)
    sql = conn.fetch.await_args.args[0]
    assert "status = 'open'" in sql
    assert "snoozed_until <= now()" in sql  # a due snooze surfaces again


# ── producers — grouping, escalation, neglect gates in SQL ────────────


@pytest.mark.asyncio
async def test_renewal_window_groups_by_property_and_escalates() -> None:
    soonest = dt.datetime.now(dt.UTC) + dt.timedelta(days=25)
    conn = _Conn(
        fetch_rows=[
            {
                "property_id": 700,
                "property_name": "Garden Row",
                "lease_count": 3,
                "soonest": soonest,
                "lease_ids": [1, 2, 3],
            }
        ]
    )
    out = await producers.renewal_window(_pool(conn), COMPANY_ID, today=dt.date.today())
    sql = conn.fetch.await_args.args[0]
    # The neglect gate: an offer row in ANY status suppresses the lease.
    assert "o.id IS NULL" in sql
    assert len(out) == 1
    c = out[0]
    assert c.entity_key == "property:700"
    assert "3 leases at Garden Row" in c.title
    assert c.escalation == 2 and c.severity == "high"  # ≤30 days out


@pytest.mark.asyncio
async def test_bills_due_flags_overdue_high() -> None:
    """⚠ The ROW SHAPE here changed with the 2026-08-30 grouping rewrite, and
    the assertions did not. ``bills_due`` now selects grouped columns
    (``bill_count`` / ``bill_ids`` / ``priced``), so the old per-bill mock
    described a query that no longer exists — gotcha 41's "changing a tool's
    output shape means changing its mock", which is a fiction if left alone.

    What this test is FOR is unchanged and still pinned: overdue reads high,
    upcoming reads normal, and the two do not merge. The one deliberate
    behaviour change is the key, ``bill:<id>`` → ``vendor:<id>|due:<date>``;
    ``sync_producer`` self-clears keys a producer stops emitting, so the old
    per-bill cards retire themselves.

    Grouping itself is covered in ``test_bill_reminder_grouping.py``.
    """
    overdue_on = dt.date.today() - dt.timedelta(days=2)
    soon_on = dt.date.today() + dt.timedelta(days=3)
    conn = _Conn(
        fetch_rows=[
            {
                "vendor_id": 5100,
                "vendor_name": "Apex Plumbing",
                "due_date": overdue_on,
                "bill_count": 1,
                "total_amount": 480.0,
                "priced": 1,
                "bill_ids": [9100],
            },
            {
                "vendor_id": 0,
                "vendor_name": None,
                "due_date": soon_on,
                "bill_count": 1,
                "total_amount": 120.0,
                "priced": 1,
                "bill_ids": [9101],
            },
        ]
    )
    out = await producers.bills_due(_pool(conn), COMPANY_ID, today=dt.date.today())
    overdue, soon = out
    assert overdue.severity == "high" and "was due" in overdue.title
    assert soon.severity == "normal" and "due soon" in soon.title
    # A vendor-less row still names something a human recognises rather than
    # rendering "Vendor #0".
    assert "Bill #9101" in soon.title
    assert soon.entity_key == f"vendor:0|due:{soon_on.isoformat()}"


@pytest.mark.asyncio
async def test_rent_missing_respects_grace_day_and_activity_gate() -> None:
    # Before the grace day: silent, no query at all.
    conn = _Conn()
    fake_today = dt.date.today().replace(day=5)
    with patch.object(producers.dt, "date", MagicMock(today=lambda: fake_today)):
        out = await producers.rent_missing(
            _pool(conn), COMPANY_ID, grace_day=10, today=dt.date.today()
        )
    assert out == []
    conn.fetch.assert_not_awaited()

    # After the grace day: the SQL carries BOTH DB neglect gates and the
    # per-lease rows group per property in the card.
    conn = _Conn(
        fetch_rows=[
            {
                "buildium_id": 101,
                "property_id": 700,
                "property_name": "Garden Row",
                "unit_number": "1A",
                "tenant_emails": ["a@example.com"],
                "promised_date": None,
            },
            {
                "buildium_id": 102,
                "property_id": 700,
                "property_name": "Garden Row",
                "unit_number": "3C",
                "tenant_emails": [],
                "promised_date": None,
            },
        ]
    )
    fake_today = dt.date.today().replace(day=15)
    with patch.object(producers.dt, "date", MagicMock(today=lambda: fake_today)):
        out = await producers.rent_missing(
            _pool(conn), COMPANY_ID, grace_day=10, today=dt.date.today()
        )
    sql = conn.fetch.await_args.args[0]
    assert "transaction_type = 'Payment'" in sql  # payment posted → not a candidate
    assert "helixis_activity_log" in sql  # recent outreach → suppressed
    assert "helixis_payment_promises" in sql  # promise date rides along (v1.5)
    assert len(out) == 1
    assert out[0].entity_key.endswith(":property:700")  # month-scoped key
    assert out[0].entity_key.startswith(fake_today.strftime("%Y-%m"))
    assert "2 leases at Garden Row" in out[0].title
    assert "no rent payment this month" in out[0].title
    assert "Unit 1A, Unit 3C" in (out[0].body or "")


@pytest.mark.asyncio
async def test_webhooks_quiet_only_fires_for_previously_active_feed() -> None:
    # Never active → silent.
    conn = _Conn(fetchrow_result={"last_event": None, "recent": 0})
    assert await producers.webhooks_quiet(_pool(conn), COMPANY_ID, today=dt.date.today()) == []

    # Active an hour ago → silent.
    conn = _Conn(
        fetchrow_result={
            "last_event": dt.datetime.now(dt.UTC) - dt.timedelta(hours=1),
            "recent": 12,
        }
    )
    assert await producers.webhooks_quiet(_pool(conn), COMPANY_ID, today=dt.date.today()) == []

    # Previously active, silent 30h → card.
    conn = _Conn(
        fetchrow_result={
            "last_event": dt.datetime.now(dt.UTC) - dt.timedelta(hours=30),
            "recent": 12,
        }
    )
    out = await producers.webhooks_quiet(_pool(conn), COMPANY_ID, today=dt.date.today())
    assert len(out) == 1 and out[0].severity == "high"


# ── sweep — producer isolation ────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_isolates_failing_producer_and_never_clears_its_cards() -> None:
    good = AsyncMock(return_value=[ReminderCandidate(entity_key="x", title="t")])
    bad = AsyncMock(side_effect=RuntimeError("query broke"))
    synced: list[str] = []

    async def _capture_sync(pool: Any, *, company_id: str, producer: str, candidates: Any) -> None:
        synced.append(producer)

    with (
        patch.object(sweep, "PRODUCERS", {"good": good, "bad": bad}),
        patch.object(sweep, "PRODUCER_READS", {"good": frozenset(), "bad": frozenset()}),
        patch.object(sweep.reminders_repo, "sync_producer", _capture_sync),
    ):
        counts = await sweep.run_reminder_sweep(MagicMock(), COMPANY_ID)
    # The failing producer is skipped ENTIRELY — no sync call means no
    # clear pass, so its existing cards survive a transient failure.
    assert synced == ["good"]
    assert counts == {"good": 1}


# ── turnover — event-driven card from Lease.MoveOut.Created ───────────


@pytest.mark.asyncio
async def test_upsert_card_never_clears_other_cards() -> None:
    """Event-driven cards (turnover) upsert exactly one row — no clear
    pass, so a handler firing for lease A can't wipe lease B's card."""
    conn = _Conn()
    await reminders_repo.upsert_card(
        _pool(conn),
        company_id=COMPANY_ID,
        producer="turnover",
        candidate=ReminderCandidate(entity_key="lease:5678", title="t"),
    )
    calls = conn.execute.await_args_list
    assert len(calls) == 1
    assert "INSERT INTO helixis_reminders" in calls[0].args[0]
    assert "cleared" not in calls[0].args[0].split("ON CONFLICT")[0]


@pytest.mark.asyncio
async def test_moveout_handler_drops_turnover_card() -> None:
    from contextlib import asynccontextmanager as _acm

    from helixis.webhooks.handlers import leases_v2 as leases_h

    @_acm
    async def _yield_api(api):
        yield api

    api = MagicMock()
    move_out = MagicMock()
    move_out.move_out_date = dt.datetime(2026, 8, 31)
    api.leases.list_move_outs = AsyncMock(return_value=[move_out])

    conn = _Conn(
        fetchrow_result={
            "property_id": 700,
            "unit_id": 55,
            "property_name": "Garden Row",
            "unit_number": "3C",
        }
    )
    with (
        patch.object(leases_h, "open_api_for_company", return_value=_yield_api(api)),
        patch.object(leases_h, "get_pool", return_value=_pool(conn)),
        patch.object(leases_h, "write_activity", new=AsyncMock()),
        # The card is dropped by the shared trigger (turnovers.start_turnover).
        patch.object(
            leases_h.turnovers_repo.reminders_repo, "upsert_card", new=AsyncMock()
        ) as card,
        patch.object(leases_h.turnovers_repo, "open_turnover", new=AsyncMock()) as turnover,
    ):
        await leases_h.handle_lease_moveout(
            COMPANY_ID,
            {"EventName": "Lease.MoveOut.Created", "AccountId": 42, "LeaseId": 5678},
        )
    card.assert_awaited_once()
    kwargs = card.await_args.kwargs
    assert kwargs["producer"] == "turnover"
    c = kwargs["candidate"]
    assert c.entity_key == "lease:5678"
    assert "Garden Row · Unit 3C" in c.title and "2026-08-31" in c.title
    assert c.severity == "high"
    assert c.link_path == "/properties/700"
    # The same trigger opens the property-page checklist row (0046).
    turnover.assert_awaited_once()
    tk = turnover.await_args.kwargs
    assert tk["lease_buildium_id"] == 5678
    assert tk["property_id"] == 700 and tk["unit_id"] == 55
    assert tk["move_out_date"] == dt.date(2026, 8, 31)


@pytest.mark.asyncio
async def test_moveout_card_failure_never_breaks_the_handler() -> None:
    from contextlib import asynccontextmanager as _acm

    from helixis.webhooks.handlers import leases_v2 as leases_h

    @_acm
    async def _yield_api(api):
        yield api

    api = MagicMock()
    mo = MagicMock()
    mo.move_out_date = None
    api.leases.list_move_outs = AsyncMock(return_value=[mo])
    with (
        patch.object(leases_h, "open_api_for_company", return_value=_yield_api(api)),
        patch.object(leases_h, "get_pool", return_value=object()),  # acquire will blow up
        patch.object(leases_h, "write_activity", new=AsyncMock()) as activity,
    ):
        await leases_h.handle_lease_moveout(
            COMPANY_ID,
            {"EventName": "Lease.MoveOut.Created", "AccountId": 42, "LeaseId": 5678},
        )
    activity.assert_awaited_once()  # the webhook still succeeds
