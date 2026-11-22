"""Tests for the deposit_return_clock reminder producer — the pure scheduling
core (move-out date + state deposit rules → when to remind, message text) and
the dark-by-default env gate (the invoice_no_work_order precedent). Mocked
pool, zero live calls — same style as test_reminders_engine.py."""

from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.geo import deadline_math, jurisdiction
from helixis.reminders import producers

COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"

_DEPOSIT = {
    "cap_months": 2,
    "return_deadline_days": 30,
    "deadline_trigger": "tenancy_end",
    "citation": "§77-1",
}


# ── pure scheduling core ──────────────────────────────────────────────


def test_no_deposit_group_or_non_numeric_deadline_means_no_clock() -> None:
    move_out = dt.date(2026, 8, 1)
    today = dt.date(2026, 8, 25)
    assert producers.deposit_clock_schedule(move_out, "GA", None, today=today) is None
    vague = {"return_deadline_days": "reasonable time"}
    assert producers.deposit_clock_schedule(move_out, "GA", vague, today=today) is None


def test_before_reminder_window_stays_quiet() -> None:
    # Move-out 2026-08-01, 30 calendar days → due 2026-08-31 (Mon),
    # completion Fri 2026-08-28; window opens 5 days before completion.
    reminder = producers.deposit_clock_schedule(
        dt.date(2026, 8, 1), "GA", _DEPOSIT, today=dt.date(2026, 8, 10)
    )
    assert reminder is None


def test_in_window_reminds_with_due_completion_and_citation() -> None:
    reminder = producers.deposit_clock_schedule(
        dt.date(2026, 8, 1), "GA", _DEPOSIT, today=dt.date(2026, 8, 24)
    )
    assert reminder is not None
    assert reminder.due_date == dt.date(2026, 8, 31)
    assert reminder.completion_date == dt.date(2026, 8, 28)
    assert reminder.severity == "normal" and reminder.escalation == 0
    assert "returned by 2026-08-31" in reminder.body
    assert "Schedule completion by 2026-08-28" in reminder.body
    assert "never rely on weekend roll-forward" in reminder.body
    assert "§77-1" in reminder.body


def test_past_completion_escalates_and_past_due_escalates_harder() -> None:
    closing = producers.deposit_clock_schedule(
        dt.date(2026, 8, 1), "GA", _DEPOSIT, today=dt.date(2026, 8, 30)
    )
    assert closing is not None
    assert closing.severity == "high" and closing.escalation == 1
    assert "completion window is closing" in closing.body

    passed = producers.deposit_clock_schedule(
        dt.date(2026, 8, 1), "GA", _DEPOSIT, today=dt.date(2026, 9, 3)
    )
    assert passed is not None
    assert passed.severity == "high" and passed.escalation == 2
    assert "PASSED" in passed.body


def test_long_past_due_self_clears() -> None:
    reminder = producers.deposit_clock_schedule(
        dt.date(2026, 8, 1), "GA", _DEPOSIT, today=dt.date(2026, 10, 1)
    )
    assert reminder is None


def test_business_day_unit_rides_deadline_math() -> None:
    group = dict(_DEPOSIT, return_deadline_days=2, return_deadline_unit="business")
    # Move-out Wed 2025-12-31: New Year's Day skipped, Fri Jan 2 (1),
    # weekend skipped, Mon Jan 5 (2) → due 2026-01-05.
    reminder = producers.deposit_clock_schedule(
        dt.date(2025, 12, 31), "GA", group, today=dt.date(2026, 1, 2)
    )
    assert reminder is not None
    assert reminder.due_date == dt.date(2026, 1, 5)
    expected = deadline_math.compliance_completion_date(dt.date(2026, 1, 5), state="GA")
    assert reminder.completion_date == expected


# ── producer wiring (env gate + mocked pool) ──────────────────────────


class _Conn:
    def __init__(self, fetch_rows: list | None = None) -> None:
        self.fetch = AsyncMock(return_value=fetch_rows or [])


def _pool(conn: _Conn) -> MagicMock:
    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


@pytest.mark.asyncio
async def test_producer_is_dark_by_default(monkeypatch) -> None:
    monkeypatch.delenv("HELIXIS_REMINDERS__ENABLED_IDS", raising=False)
    conn = _Conn([{"lease_buildium_id": 1}])
    assert (
        await producers.deposit_return_clock(_pool(conn), COMPANY_ID, today=dt.date.today()) == []
    )
    conn.fetch.assert_not_awaited()  # gate short-circuits before any SQL


@pytest.mark.asyncio
async def test_producer_registered_in_sweep_registry() -> None:
    assert producers.PRODUCERS["deposit_return_clock"] is producers.deposit_return_clock


def _row(**over: Any) -> dict:
    row = {
        "lease_buildium_id": 42,
        "move_out_date": dt.date(2026, 8, 1),
        "property_id": 700,
        "property_name": "Garden Row",
        "state_fips": "13",
        "unit_number": "3C",
    }
    row.update(over)
    return row


_XA_STATE = {"name": "Alphaland", "deposit": dict(_DEPOSIT)}


@pytest.mark.asyncio
async def test_enabled_producer_emits_a_card_per_lease(monkeypatch) -> None:
    monkeypatch.setenv("HELIXIS_REMINDERS__ENABLED_IDS", "deposit_return_clock")
    conn = _Conn([_row()])
    fake_answer = jurisdiction.JurisdictionAnswer(
        state_abbr="GA", state_name="Georgia", version="t", state=dict(_XA_STATE)
    )

    fake_today = dt.date(2026, 8, 24)
    with (
        patch.object(producers.jurisdiction, "lookup", MagicMock(return_value=fake_answer)),
        patch.object(producers.dt, "date", MagicMock(today=lambda: fake_today)),
    ):
        cards = await producers.deposit_return_clock(_pool(conn), COMPANY_ID, today=dt.date.today())
    assert len(cards) == 1
    card = cards[0]
    assert card.entity_key == "lease:42"
    assert "Deposit return due 2026-08-31" in card.title
    assert "Garden Row · Unit 3C" in card.title
    assert card.link_path == "/properties/700"
    assert card.payload["state"] == "GA"
    assert card.payload["completion_date"] == "2026-08-28"


@pytest.mark.asyncio
async def test_unresolved_state_is_skipped_never_guessed(monkeypatch) -> None:
    monkeypatch.setenv("HELIXIS_REMINDERS__ENABLED_IDS", "deposit_return_clock")
    conn = _Conn([_row(state_fips=None)])
    with patch.object(producers.jurisdiction, "lookup") as lookup:
        cards = await producers.deposit_return_clock(_pool(conn), COMPANY_ID, today=dt.date.today())
    assert cards == []
    lookup.assert_not_called()
