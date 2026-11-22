"""Reminders v1.5 — payment promises + Gmail-outbound suppression.

Pins the two new neglect signals from the 2026-07-06 discussion ("someone
said they'd pay on the 17th → no card until the 17th passes"; "the PM
already emailed them → no card"): deterministic promise extraction, the
pending/passed suppression rule, the fail-open outbound checker with its
batched-probe-then-attribute call shape, and how rent_missing /
leads_gone_cold / the sweep consume both.
"""

from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.db.repositories import promises as promises_repo
from helixis.reminders import outbound as outbound_mod
from helixis.reminders import producers, sweep
from helixis.reminders.promises import extract_promise_date, promise_pending

COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
TODAY = dt.date(2026, 7, 6)  # a Monday


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
    def __init__(self, fetch_rows: list | None = None) -> None:
        self.execute = AsyncMock()
        self.fetch = AsyncMock(return_value=fetch_rows or [])
        self.fetchrow = AsyncMock(return_value=None)


def _pool(conn: _Conn) -> MagicMock:
    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


# ── promise extraction ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I'll pay rent on the 17th, sorry for the delay", dt.date(2026, 7, 17)),
        ("can pay the 3rd", dt.date(2026, 8, 3)),  # past this month → next month
        ("I will pay by July 17", dt.date(2026, 7, 17)),
        ("payment coming 17 July for sure", dt.date(2026, 7, 17)),
        ("I can zelle you 7/20", dt.date(2026, 7, 20)),
        ("will send the check by Friday", dt.date(2026, 7, 10)),
        ("I'll pay tomorrow", dt.date(2026, 7, 7)),
        ("rent will be paid next week", dt.date(2026, 7, 13)),
        ("I'll call you back on the 17th", None),  # no payment word
        ("I'll pay as soon as I can", None),  # no concrete date
        ("I can pay on October 17", None),  # beyond the 45-day window
        ("the unit rents for the 200", None),  # bare number, no ordinal
        ("", None),
    ],
)
def test_extract_promise_date(text: str, expected: dt.date | None) -> None:
    assert extract_promise_date(text, today=TODAY) == expected


def test_promise_pending_suppresses_through_grace_then_lapses() -> None:
    promised = dt.date(2026, 7, 17)
    assert promise_pending(promised, today=dt.date(2026, 7, 10))
    assert promise_pending(promised, today=dt.date(2026, 7, 17))
    assert promise_pending(promised, today=dt.date(2026, 7, 18))  # +1 grace day
    assert not promise_pending(promised, today=dt.date(2026, 7, 19))  # follow-up time
    assert not promise_pending(None, today=TODAY)


@pytest.mark.asyncio
async def test_record_promise_upserts_latest_per_sender() -> None:
    conn = _Conn()
    await promises_repo.record_promise(
        _pool(conn),
        company_id=COMPANY_ID,
        email="Sarah@Example.com",
        promised_date=dt.date(2026, 7, 17),
        source_message_id="m1",
    )
    sql = conn.execute.await_args.args[0]
    assert "ON CONFLICT (company_id, email)" in sql  # newest word wins
    assert "lower($2)" in sql


# ── outbound checker ──────────────────────────────────────────────────


def _composio(execute_results: list[Any]) -> MagicMock:
    composio = MagicMock()
    composio.available = True
    composio.is_connected = MagicMock(return_value=True)
    composio.execute_tool = MagicMock(side_effect=execute_results)
    return composio


@pytest.mark.asyncio
async def test_outbound_clean_probe_is_one_call_and_no_suppression() -> None:
    composio = _composio([{"data": {"messages": []}}])
    checker = outbound_mod.GmailOutboundChecker(composio, ["u1"])
    out = await checker.recently_contacted({"a@x.com", "b@x.com"}, days=5)
    assert out == set()
    assert composio.execute_tool.call_count == 1  # one batched probe
    query = composio.execute_tool.call_args.kwargs["arguments"]["query"]
    assert "in:sent" in query and "newer_than:5d" in query
    assert "to:a@x.com" in query and "to:b@x.com" in query


@pytest.mark.asyncio
async def test_outbound_probe_hit_attributes_per_address() -> None:
    hit = {"data": {"messages": [{"id": "m"}]}}
    miss = {"data": {"messages": []}}
    # Probe hits, then per-address: a@x.com hit, b@x.com miss.
    composio = _composio([hit, hit, miss])
    checker = outbound_mod.GmailOutboundChecker(composio, ["u1"])
    out = await checker.recently_contacted({"a@x.com", "b@x.com"}, days=5)
    assert out == {"a@x.com"}
    assert composio.execute_tool.call_count == 3


@pytest.mark.asyncio
async def test_outbound_single_address_hit_needs_no_followup() -> None:
    composio = _composio([{"data": {"messages": [{"id": "m"}]}}])
    checker = outbound_mod.GmailOutboundChecker(composio, ["u1"])
    out = await checker.recently_contacted({"a@x.com"}, days=5)
    assert out == {"a@x.com"}
    assert composio.execute_tool.call_count == 1


@pytest.mark.asyncio
async def test_outbound_fails_open() -> None:
    composio = _composio([RuntimeError("composio down")])
    checker = outbound_mod.GmailOutboundChecker(composio, ["u1"])
    out = await checker.recently_contacted({"a@x.com"}, days=5)
    assert out == set()  # no evidence → no suppression, never a crash


@pytest.mark.asyncio
async def test_build_outbound_checker_gates() -> None:
    assert await outbound_mod.build_outbound_checker(MagicMock(), None, COMPANY_ID) is None
    dark = MagicMock(available=False)
    assert await outbound_mod.build_outbound_checker(MagicMock(), dark, COMPANY_ID) is None
    live = MagicMock(available=True)
    with patch(
        "helixis.db.repositories.gmail_poll_state.list_poll_targets",
        AsyncMock(return_value=[("u1", COMPANY_ID), ("u2", "other-co")]),
    ):
        checker = await outbound_mod.build_outbound_checker(MagicMock(), live, COMPANY_ID)
    assert checker is not None
    assert checker._user_ids == ["u1"]  # company-scoped


# ── rent_missing with the new gates ───────────────────────────────────


def _lease_row(
    lease_id: int,
    unit: str,
    emails: list[str],
    promised: dt.date | None = None,
) -> dict[str, Any]:
    return {
        "buildium_id": lease_id,
        "property_id": 700,
        "property_name": "Garden Row",
        "unit_number": unit,
        "tenant_emails": emails,
        "promised_date": promised,
    }


def _mid_month(day: int = 15) -> dt.date:
    return dt.date.today().replace(day=day)


@pytest.mark.asyncio
async def test_rent_missing_pending_promise_suppresses_lease() -> None:
    fake_today = _mid_month()
    conn = _Conn(
        fetch_rows=[
            _lease_row(101, "1A", ["a@x.com"], promised=fake_today + dt.timedelta(days=2)),
            _lease_row(102, "3C", ["b@x.com"]),
        ]
    )
    with patch.object(producers.dt, "date", MagicMock(today=lambda: fake_today)):
        out = await producers.rent_missing(
            _pool(conn), COMPANY_ID, grace_day=10, today=dt.date.today()
        )
    assert len(out) == 1
    assert out[0].payload["count"] == 1  # the promising lease sat out
    assert "Unit 3C" in (out[0].body or "") and "Unit 1A" not in (out[0].body or "")


@pytest.mark.asyncio
async def test_rent_missing_passed_promise_returns_as_followup() -> None:
    fake_today = _mid_month()
    conn = _Conn(
        fetch_rows=[
            _lease_row(101, "1A", ["a@x.com"], promised=fake_today - dt.timedelta(days=3)),
        ]
    )
    with patch.object(producers.dt, "date", MagicMock(today=lambda: fake_today)):
        out = await producers.rent_missing(
            _pool(conn), COMPANY_ID, grace_day=10, today=dt.date.today()
        )
    assert len(out) == 1
    assert out[0].payload["broken_promises"] == 1
    assert "now passed without posting" in (out[0].body or "")


@pytest.mark.asyncio
async def test_rent_missing_outbound_suppresses_contacted_lease() -> None:
    fake_today = _mid_month()
    conn = _Conn(
        fetch_rows=[
            _lease_row(101, "1A", ["a@x.com"]),
            _lease_row(102, "3C", ["b@x.com"]),
        ]
    )
    outbound = MagicMock()
    outbound.recently_contacted = AsyncMock(return_value={"a@x.com"})
    with patch.object(producers.dt, "date", MagicMock(today=lambda: fake_today)):
        out = await producers.rent_missing(
            _pool(conn), COMPANY_ID, grace_day=10, outbound=outbound, today=dt.date.today()
        )
    outbound.recently_contacted.assert_awaited_once()
    assert outbound.recently_contacted.await_args.kwargs["days"] == producers.SUPPRESS_DAYS
    assert len(out) == 1
    assert "Unit 3C" in (out[0].body or "") and "Unit 1A" not in (out[0].body or "")


@pytest.mark.asyncio
async def test_rent_missing_clean_month_never_calls_gmail() -> None:
    fake_today = _mid_month()
    conn = _Conn(fetch_rows=[])
    outbound = MagicMock()
    outbound.recently_contacted = AsyncMock()
    with patch.object(producers.dt, "date", MagicMock(today=lambda: fake_today)):
        out = await producers.rent_missing(
            _pool(conn), COMPANY_ID, grace_day=10, outbound=outbound, today=dt.date.today()
        )
    assert out == []
    outbound.recently_contacted.assert_not_awaited()  # zero survivors → zero API


@pytest.mark.asyncio
async def test_leads_gone_cold_outbound_suppresses_contacted_lead() -> None:
    lead_a = MagicMock(email="a@x.com", id="L1", stage="new", unit_label=None, phone=None)
    lead_a.name = "Ana"
    lead_b = MagicMock(email="b@x.com", id="L2", stage="new", unit_label=None, phone=None)
    lead_b.name = "Ben"
    outbound = MagicMock()
    outbound.recently_contacted = AsyncMock(return_value={"a@x.com"})
    with patch.object(producers.leads_repo, "cold_leads", AsyncMock(return_value=[lead_a, lead_b])):
        out = await producers.leads_gone_cold(
            MagicMock(), COMPANY_ID, outbound=outbound, today=dt.date.today()
        )
    assert len(out) == 1
    assert "Ben" in out[0].title


# ── sweep wiring ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_passes_outbound_only_to_aware_producers() -> None:
    aware = AsyncMock(return_value=[])
    plain = AsyncMock(return_value=[])
    sentinel = MagicMock()
    with (
        patch.object(sweep, "PRODUCERS", {"lead_cold": aware, "bill_due": plain}),
        patch.object(sweep.reminders_repo, "sync_producer", AsyncMock()),
    ):
        await sweep.run_reminder_sweep(MagicMock(), COMPANY_ID, outbound=sentinel)
    assert aware.await_args.kwargs["outbound"] is sentinel
    assert "outbound" not in plain.await_args.kwargs


# ── email-poll promise hook ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_records_promises_best_effort() -> None:
    from helixis.webhooks import email_poll

    promising = MagicMock(
        message_id="m1",
        from_email="sarah@x.com",
        subject="Rent",
        body_text="I'll pay on the 17th, promise",
    )
    plain = MagicMock(
        message_id="m2",
        from_email="tom@x.com",
        subject="Hi",
        body_text="The hallway light is out again",
    )
    record = AsyncMock()
    with patch.object(email_poll, "record_promise", record):
        await email_poll._record_payment_promises(MagicMock(), COMPANY_ID, [promising, plain])
    record.assert_awaited_once()
    kwargs = record.await_args.kwargs
    assert kwargs["email"] == "sarah@x.com"
    assert kwargs["promised_date"].day == 17
    assert kwargs["source_message_id"] == "m1"

    # A DB failure never propagates into the poll.
    record = AsyncMock(side_effect=RuntimeError("db down"))
    with patch.object(email_poll, "record_promise", record):
        await email_poll._record_payment_promises(MagicMock(), COMPANY_ID, [promising])


@pytest.mark.asyncio
async def test_sweep_clears_cards_from_retired_producers() -> None:
    """Retiring a producer must not strand its cards. ``sync_producer``
    reconciles only WITHIN a producer that still runs, so unregistering one
    leaves its open cards with no code left that could ever clear them —
    retiring ``rent_missing`` would have parked 41 of them in the Inbox."""
    clear = AsyncMock(return_value=3)
    with (
        patch.object(sweep, "PRODUCERS", {"bill_due": AsyncMock(return_value=[])}),
        patch.object(sweep.reminders_repo, "sync_producer", AsyncMock()),
        patch.object(sweep.reminders_repo, "clear_unknown_producers", clear),
    ):
        await sweep.run_reminder_sweep(MagicMock(), COMPANY_ID)
    assert clear.await_args.kwargs["known"] == ("bill_due",)


@pytest.mark.asyncio
async def test_a_failed_retirement_cleanup_never_stops_the_sweep() -> None:
    produce = AsyncMock(return_value=[])
    with (
        patch.object(sweep, "PRODUCERS", {"bill_due": produce}),
        patch.object(sweep.reminders_repo, "sync_producer", AsyncMock()),
        patch.object(
            sweep.reminders_repo,
            "clear_unknown_producers",
            AsyncMock(side_effect=RuntimeError("db down")),
        ),
    ):
        await sweep.run_reminder_sweep(MagicMock(), COMPANY_ID)
    produce.assert_awaited()


def test_a_noisy_producer_folds_into_one_card() -> None:
    """Fifteen cards saying the same thing in fifteen places bury the one
    card that mattered — and on single-unit buildings, per-property grouping
    IS per-lease grouping."""
    from helixis.db.repositories.reminders import ReminderCandidate
    from helixis.reminders.producers import FOLD_THRESHOLD, fold_when_noisy

    many = [
        ReminderCandidate(entity_key=f"property:{i}", title=f"Lease at state {i}")
        for i in range(FOLD_THRESHOLD + 4)
    ]
    folded = fold_when_noisy(
        many, entity_key="k", title="{n} properties affected", lead="At:", link_path="/operations"
    )
    assert len(folded) == 1
    assert folded[0].title == f"{len(many)} properties affected"
    assert folded[0].payload["folded"] == len(many)

    few = many[:FOLD_THRESHOLD]
    assert fold_when_noisy(few, entity_key="k", title="x", lead="At:", link_path="/o") == few, (
        "under the threshold the per-property cards are the better UX"
    )


def test_folding_keeps_the_most_urgent_severity() -> None:
    """Folding must never quietly downgrade the item that needed attention."""
    from helixis.db.repositories.reminders import ReminderCandidate
    from helixis.reminders.producers import FOLD_THRESHOLD, fold_when_noisy

    cards = [
        ReminderCandidate(entity_key=f"p:{i}", title=f"t{i}", severity="normal", escalation=0)
        for i in range(FOLD_THRESHOLD)
    ]
    cards.append(
        ReminderCandidate(entity_key="p:hot", title="urgent", severity="high", escalation=2)
    )
    folded = fold_when_noisy(cards, entity_key="k", title="{n}", lead="At:", link_path="/o")
    assert folded[0].severity == "high"
    assert folded[0].escalation == 2
