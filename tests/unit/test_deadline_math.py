"""Tests for geo/deadline_math.py — pure, deterministic deadline arithmetic.

Pins: the day-after-trigger clock start, calendar vs business counting
(weekends AND federal + state holidays skipped), the court-unit flag, the
unknown-unit longer-interpretation rule, the compliance completion date
(last business day STRICTLY before the due date), the pending_change
resolution (past / future / year-precision), and the holidays-package-missing
degrade (weekend-only + flag, never a crash).
"""

from __future__ import annotations

import sys
from datetime import date

from helixis.geo import deadline_math

# ── compute_deadline: calendar ────────────────────────────────────────


def test_calendar_count_is_straight_and_starts_day_after_trigger() -> None:
    # 30 calendar days triggered Jan 1 → due Jan 31 (day 1 is Jan 2).
    computed = deadline_math.compute_deadline(date(2026, 1, 1), 30, "calendar", state="GA")
    assert computed.due_date == date(2026, 1, 31)
    assert computed.unit_used == "calendar"
    assert computed.flags == []


def test_calendar_zero_days_is_the_trigger_date() -> None:
    computed = deadline_math.compute_deadline(date(2026, 3, 4), 0, "calendar", state="GA")
    assert computed.due_date == date(2026, 3, 4)


# ── compute_deadline: business (weekends + holidays) ──────────────────


def test_business_days_skip_weekend_and_federal_holiday() -> None:
    # Trigger Wed 2025-12-31; day after is New Year's Day (skipped), Fri
    # Jan 2 counts (1), Sat/Sun skipped, Mon Jan 5 counts (2).
    computed = deadline_math.compute_deadline(date(2025, 12, 31), 2, "business", state="GA")
    assert computed.due_date == date(2026, 1, 5)
    assert computed.unit_used == "business"
    assert computed.flags == []


def test_business_days_skip_state_specific_holiday() -> None:
    # Patriots' Day (Mon 2026-04-20) is an MA holiday but not a GA one.
    trigger = date(2026, 4, 16)  # Thursday
    ma = deadline_math.compute_deadline(trigger, 2, "business", state="MA")
    ga = deadline_math.compute_deadline(trigger, 2, "business", state="GA")
    assert ga.due_date == date(2026, 4, 20)
    assert ma.due_date == date(2026, 4, 21)


def test_court_unit_is_business_math_plus_verify_flag() -> None:
    trigger = date(2025, 12, 31)
    court = deadline_math.compute_deadline(trigger, 2, "court", state="GA")
    business = deadline_math.compute_deadline(trigger, 2, "business", state="GA")
    assert court.due_date == business.due_date
    assert court.unit_used == "court"
    assert deadline_math.COURT_FLAG in court.flags
    judicial = deadline_math.compute_deadline(trigger, 2, "judicial", state="GA")
    assert deadline_math.COURT_FLAG in judicial.flags


# ── compute_deadline: unknown unit takes the longer interpretation ────


def test_unknown_unit_returns_the_later_due_date_with_flag() -> None:
    # Trigger Fri 2026-08-14, 3 days: calendar → Mon 08-17; business →
    # Wed 08-19 (weekend skipped). Business is later, so it wins.
    computed = deadline_math.compute_deadline(date(2026, 8, 14), 3, None, state="GA")
    assert computed.due_date == date(2026, 8, 19)
    assert deadline_math.UNKNOWN_UNIT_FLAG in computed.flags
    unknown = deadline_math.compute_deadline(date(2026, 8, 14), 3, "lunar", state="GA")
    assert unknown.due_date == date(2026, 8, 19)
    assert deadline_math.UNKNOWN_UNIT_FLAG in unknown.flags


# ── compliance_completion_date ────────────────────────────────────────


def test_completion_date_is_last_business_day_strictly_before_due() -> None:
    # Due Monday → completion the prior Friday (never the weekend, never
    # the due date itself).
    assert deadline_math.compliance_completion_date(date(2026, 8, 17), state="GA") == date(
        2026, 8, 14
    )
    # A mid-week due date still steps back one full day.
    assert deadline_math.compliance_completion_date(date(2026, 8, 19), state="GA") == date(
        2026, 8, 18
    )


def test_completion_date_steps_over_holidays_too() -> None:
    # Due Fri 2026-01-02: the day before is New Year's Day → Wed 2025-12-31.
    assert deadline_math.compliance_completion_date(date(2026, 1, 2), state="GA") == date(
        2025, 12, 31
    )


# ── holidays-package-missing degrade ──────────────────────────────────


def test_missing_holidays_package_degrades_to_weekend_only_with_flag(monkeypatch) -> None:
    # Poison the import: sys.modules[name] = None makes `import holidays`
    # raise, exercising the lazy-import failure path.
    monkeypatch.setitem(sys.modules, "holidays", None)
    computed = deadline_math.compute_deadline(date(2025, 12, 31), 2, "business", state="GA")
    # New Year's Day is NOT skipped (no calendar), weekends still are:
    # Thu Jan 1 counts (1), Fri Jan 2 counts (2).
    assert computed.due_date == date(2026, 1, 2)
    assert deadline_math.HOLIDAYS_UNAVAILABLE_FLAG in computed.flags
    # The completion helper degrades silently (weekend-only) — no crash.
    assert deadline_math.compliance_completion_date(date(2026, 8, 17), state="GA") == date(
        2026, 8, 14
    )


def test_missing_holidays_package_unknown_unit_still_flags_both(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "holidays", None)
    computed = deadline_math.compute_deadline(date(2026, 8, 14), 3, None, state="GA")
    assert deadline_math.UNKNOWN_UNIT_FLAG in computed.flags
    assert deadline_math.HOLIDAYS_UNAVAILABLE_FLAG in computed.flags


# ── apply_pending_changes ─────────────────────────────────────────────

_GROUP = {
    "cap_months": 2,
    "return_deadline_days": 30,
    "pending_change": {
        "field": "cap_months",
        "new_value": 1,
        "effective_from": "2026-01-01",
        "precision": "year",
        "citation": "SB23-184 follow-on",
    },
}


def test_pending_change_past_effective_applies_new_value_with_year_flag() -> None:
    updated, flags = deadline_math.apply_pending_changes(_GROUP, today=date(2026, 8, 18))
    assert updated["cap_months"] == 1
    assert _GROUP["cap_months"] == 2  # input never mutated
    assert len(flags) == 1
    assert "cap_months changed to 1 effective 2026 — verify exact date" in flags[0]
    assert "SB23-184" in flags[0]


def test_pending_change_future_keeps_current_value_and_flags_the_date() -> None:
    updated, flags = deadline_math.apply_pending_changes(_GROUP, today=date(2025, 6, 1))
    assert updated["cap_months"] == 2
    assert len(flags) == 1
    assert "upcoming change" in flags[0]
    assert "2026-01-01" in flags[0]


def test_pending_change_day_precision_flag_names_exact_date() -> None:
    group = {
        "cap_months": 2,
        "pending_change": {
            "field": "cap_months",
            "new_value": 1,
            "effective_from": "2026-07-01",
            "precision": "day",
            "citation": "HB 1",
        },
    }
    updated, flags = deadline_math.apply_pending_changes(group, today=date(2026, 8, 18))
    assert updated["cap_months"] == 1
    assert "effective 2026-07-01" in flags[0]
    assert "verify exact date" not in flags[0]


def test_pending_change_absent_or_malformed_is_a_no_op() -> None:
    plain = {"cap_months": 2}
    assert deadline_math.apply_pending_changes(plain, today=date(2026, 1, 1)) == (plain, [])
    partial = {"cap_months": 2, "pending_change": {"new_value": 1}}
    updated, flags = deadline_math.apply_pending_changes(partial, today=date(2026, 1, 1))
    assert updated["cap_months"] == 2
    assert flags == []


def test_pending_change_unparseable_date_keeps_value_and_flags() -> None:
    group = {
        "cap_months": 2,
        "pending_change": {"field": "cap_months", "new_value": 1, "effective_from": "someday"},
    }
    updated, flags = deadline_math.apply_pending_changes(group, today=date(2026, 1, 1))
    assert updated["cap_months"] == 2
    assert len(flags) == 1
    assert "unparseable" in flags[0]
