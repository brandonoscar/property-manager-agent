"""A card expiring before the next charge is the billing failure nobody is told about.

The renewal declines, `invoice.payment_failed` lands, the row goes `past_due`,
and `notices.py` finally speaks — AFTER the money did not move and with the
grace clock already running. Stripe knew the expiry the whole time and nothing
read it. The advance warning is worth more than every other notice here because
the fix is free and takes thirty seconds, and only before the charge.

⚠ The two properties that actually matter are OPPOSITE ONES, and a one-sided
test would pass against either bug (gotcha 38): warn when the card really dies
first, and stay silent on every kind of "cannot say". A guard that only proves
the warning fires would happily accept a version that warns everybody.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from helixis.billing.card_expiry import (
    WARN_WITHIN_DAYS,
    card_valid_through,
    expires_before_charge,
    expiry_label,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def _at(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# The month-end rule, which is where the off-by-a-month lives
# ---------------------------------------------------------------------------


def test_a_card_is_valid_through_the_last_day_of_its_month() -> None:
    """⚠ The single most likely defect in this file. `exp_month=9` means the
    card works on 2026-09-30 and fails on 2026-10-01 — reading it as "expires
    on the 1st" warns a month early for every customer every month, and a
    notice wrong in the alarming direction is one people learn to dismiss."""
    assert card_valid_through(9, 2026) == datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC)


def test_february_in_a_leap_year_is_not_hardcoded() -> None:
    """`calendar.monthrange`, not a 28/30/31 table somebody has to maintain."""
    assert card_valid_through(2, 2028).day == 29
    assert card_valid_through(2, 2027).day == 28


def test_a_charge_on_the_last_valid_day_still_goes_through() -> None:
    """The boundary in the SAFE direction. A card good through 09/30 charged on
    09/30 is fine, and warning there would be a false alarm."""
    assert expires_before_charge((9, 2026), _at(2026, 9, 30), now=NOW) is False


def test_a_charge_the_day_after_expiry_is_warned_about() -> None:
    assert expires_before_charge((9, 2026), _at(2026, 10, 1), now=NOW) is True


# ---------------------------------------------------------------------------
# Every uncertainty warns nobody
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expiry,charge",
    [
        (None, _at(2026, 10, 1)),  # no card on file, or unreadable
        ((9, 2026), None),  # Stripe gave no next-charge date
        (None, None),
    ],
)
def test_unknown_is_none_and_never_a_warning(expiry: object, charge: object) -> None:
    """⚠ `None`, not `False`. Collapsing the two makes "we could not read the
    card" indistinguishable from "the card is fine" to the caller, and the
    first one would then render as reassurance (gotchas 55/57)."""
    assert expires_before_charge(expiry, charge, now=NOW) is None  # type: ignore[arg-type]


def test_a_naive_charge_date_does_not_raise_inside_a_billing_endpoint() -> None:
    """Comparing a naive datetime against an aware one raises, and the raise
    would land in the route. Read as UTC — every date on this path comes from
    a Stripe unix timestamp, which is UTC by definition."""
    assert expires_before_charge((9, 2026), datetime(2026, 10, 1, 12, 0), now=NOW) is True


# ---------------------------------------------------------------------------
# The window, and why it is not "warn about everything"
# ---------------------------------------------------------------------------


def test_an_expiry_far_beyond_the_next_charge_is_not_news() -> None:
    assert expires_before_charge((12, 2030), _at(2026, 10, 1), now=NOW) is False


def test_a_charge_further_out_than_the_window_is_not_warned_about_yet() -> None:
    """⚠ Deliberate, and the reason is that a banner shown every visit for eight
    months is one nobody reads by the time it matters (gotcha 52's
    switched-off-guard shape). The card DOES die first here — the answer is
    "not yet", not "fine forever"."""
    far = _at(2027, 6, 1)
    assert (far - NOW).days > WARN_WITHIN_DAYS
    assert expires_before_charge((1, 2027), far, now=NOW) is False


def test_the_window_still_warns_at_its_far_edge() -> None:
    """The other side of the same boundary — a window that silently excluded
    its own edge would be shorter than it says, and this is what makes the
    constant mean what it claims."""
    edge = NOW.replace(hour=12) + (datetime(2026, 10, 9, 12, 0, tzinfo=UTC) - NOW)
    assert (edge - NOW).days == WARN_WITHIN_DAYS
    assert expires_before_charge((9, 2026), edge, now=NOW) is True


# ---------------------------------------------------------------------------
# What the reader sees
# ---------------------------------------------------------------------------


def test_the_label_matches_what_is_printed_on_the_card() -> None:
    """`09/2026`, not "September 2026". The reader is about to compare this
    against a physical card or a wallet entry, and matching the printed form is
    what makes the check instant."""
    assert expiry_label((9, 2026)) == "09/2026"
    assert expiry_label((12, 2030)) == "12/2030"
