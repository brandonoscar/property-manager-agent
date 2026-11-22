"""Will the card on file still be valid on the day we charge it?

WHAT BREAKS WITHOUT THIS
------------------------
A card expires mid-cycle. The renewal declines. `invoice.payment_failed`
lands, the row goes `past_due`, and `notices.py` finally speaks — AFTER the
money did not move, with the grace clock already running and the customer
wondering what they did wrong. The expiry was knowable the whole time and
nothing read it.

The advance warning is worth more than every other billing notice here
precisely because the fix is trivial and free: thirty seconds in the portal,
before anything fails, versus a dunning window and a support email after.

⚠ **A CARD IS VALID THROUGH THE LAST DAY OF ITS EXPIRY MONTH**, not from the
first. `exp_month=9, exp_year=2026` works on 2026-09-30 and fails on
2026-10-01. Reading it as "expires on the 1st" would warn a month early, every
month, for every customer — and a notice that is wrong in the alarming
direction is one people learn to dismiss, which costs the warning that matters
(gotcha 52's switched-off-guard shape).

⚠ **Everything uncertain warns NOBODY.** No card, no expiry, no next charge
date: all of them return `None` rather than a guess. Telling somebody their
card is about to expire when we could not read it is a false alarm about their
money; the opposite costs one declined renewal that the existing dunning path
already handles (gotchas 55/57 — unknown is not a value).
"""

from __future__ import annotations

import calendar
from datetime import UTC, datetime

#: How far ahead a warning is useful. Longer is not better: a warning that
#: arrives six weeks out is background noise by the time it matters, and the
#: action it asks for (update the card) is one somebody does in the moment
#: rather than schedules. Thirty days covers a full monthly cycle, so every
#: customer sees it at least once before the charge that would fail.
WARN_WITHIN_DAYS = 30


def card_valid_through(exp_month: int, exp_year: int) -> datetime:
    """The last instant the card works — the END of its expiry month, UTC.

    `calendar.monthrange` rather than a hardcoded 28/30/31 table, so February
    in a leap year is right without anybody maintaining a list.
    """
    last_day = calendar.monthrange(exp_year, exp_month)[1]
    return datetime(exp_year, exp_month, last_day, 23, 59, 59, tzinfo=UTC)


def expires_before_charge(
    expiry: tuple[int, int] | None,
    charge_at: datetime | None,
    *,
    now: datetime | None = None,
) -> bool | None:
    """Will the card be dead by the time Stripe charges it?

    ``True`` warn · ``False`` fine · ``None`` cannot say.

    ⚠ The three-valued return is the point, and collapsing it to a bool is the
    bug this signature exists to prevent: "we could not read the card" and
    "the card is fine" are the same `False` to a caller, and the first one
    would then render as reassurance.

    ⚠ It also declines when the charge is FURTHER OUT than the warning window.
    A card expiring in eight months is not news, and surfacing it on the
    billing panel every single visit for eight months trains the reader to
    ignore the banner.
    """
    if expiry is None or charge_at is None:
        return None
    now = now or datetime.now(UTC)
    if charge_at.tzinfo is None:
        # A naive datetime compared against an aware one raises, and the raise
        # would land inside a billing endpoint. Read it as UTC — every date on
        # this path comes from a Stripe unix timestamp, which is UTC by
        # definition.
        charge_at = charge_at.replace(tzinfo=UTC)
    if (charge_at - now).days > WARN_WITHIN_DAYS:
        return False
    return card_valid_through(*expiry) < charge_at


def expiry_label(expiry: tuple[int, int]) -> str:
    """``09/2026`` — the format printed on the card itself.

    Deliberately NOT "September 2026": the reader is about to compare this
    against a physical card or a wallet entry, and matching what is printed
    there is what makes the check instant.
    """
    month, year = expiry
    return f"{month:02d}/{year}"


__all__ = [
    "WARN_WITHIN_DAYS",
    "card_valid_through",
    "expires_before_charge",
    "expiry_label",
]
