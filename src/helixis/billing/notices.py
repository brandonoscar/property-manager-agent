"""The warning a customer gets BEFORE billing cuts them off.

Two things end service silently today: a trial running out, and a card that
stopped working. Both are enforced in `gate.py` and neither was ever
announced, so the first a customer knows of either is a refusal mid-sentence.
That is the difference between a lapse and a refund request.

**There is no transactional email provider in this deployment.** Auth mail
goes through GoTrue and Gmail is per-user OAuth for the agent, neither of
which can send "your trial ends Friday" from Occupella. So this is the
in-app half, and it is the half we can actually guarantee. The email half is
Stripe's own dunning mail, which is a dashboard toggle
(Billing → Automatic collection → Customer emails) and is invisible from here
— gotcha 29's rule pointed at a vendor console.

**Why a module and not two `if`s in the panel.** The same two facts drive the
chat banner, the billing page and anything added later; gotchas 25/67/73 are
all one rule shipped to one caller and not the next. The server answers, the
client renders — the same split `block` already uses, and for the same reason:
two booleans cannot tell "your trial is ending" from "your card failed", and
those need opposite actions from the reader.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from helixis.billing.gate import PAYMENT_GRACE_DAYS, TRIAL_NOTICE_DAYS, BillingState

Severity = Literal["info", "warning", "critical"]


@dataclass(frozen=True)
class BillingNotice:
    """A standing message about the account, not about this request.

    Distinct from `BillingBlock`: a block means the turn was REFUSED and is
    returned by the gate. A notice means service is fine and something is
    about to change. Rendering a notice as though it were a refusal is how a
    customer with nine days left decides we have already cut them off.
    """

    reason: str
    """'trial_ending' | 'payment_failed'. Machine-readable; do not parse the text."""
    severity: Severity
    message: str
    """What the reader sees. Always names the deadline AND the fix."""
    action: str | None = None
    """Where to go. `None` when there is nothing the customer can do."""


def notice_for(state: BillingState | None) -> BillingNotice | None:
    """The one standing notice for this company, or `None`.

    ⚠ Returns AT MOST ONE. A stack of banners is a stack nobody reads, and
    the two conditions here are close to mutually exclusive in practice (a
    trial has no card to decline). Payment failure wins when both somehow
    hold: it has a hard deadline and a fix the customer can act on today.

    ⚠ An unreadable state produces NO notice. `load_state` returns `None` on a
    database blip, and manufacturing "your trial is ending" out of a failed
    query would be a false alarm about somebody's money — the same asymmetry
    the gate's rule 1 takes, pointed at what we SAY rather than what we allow.
    """
    if state is None:
        return None

    days_left = state.grace_days_left
    if days_left is not None and days_left > 0:
        return BillingNotice(
            reason="payment_failed",
            severity="critical",
            message=(
                f"Your last payment didn't go through. Occupella keeps working "
                f"for {_days(days_left)}, then pauses until the card is updated."
            ),
            action="Update payment method",
        )

    trial_left = state.trial_days_left
    if trial_left is not None and trial_left <= TRIAL_NOTICE_DAYS:
        return BillingNotice(
            reason="trial_ending",
            # The last day is materially different from the fifth: one is a
            # reminder, the other is today's problem. A single severity for
            # both trains people to ignore the banner for four days and then
            # be surprised.
            severity="critical" if trial_left <= 1 else "warning",
            message=(
                f"Your free trial ends {_when(trial_left)}. Pick a plan to keep "
                "going — everything Occupella has synced stays exactly as it is."
            ),
            action="Choose a plan",
        )

    return None


def _days(n: int) -> str:
    """⚠ '1 days' is the tell that nobody read the string they shipped."""
    return "1 more day" if n == 1 else f"{n} more days"


def _when(n: int) -> str:
    if n <= 0:
        return "today"
    if n == 1:
        return "tomorrow"
    return f"in {n} days"


__all__ = ["PAYMENT_GRACE_DAYS", "BillingNotice", "Severity", "notice_for"]
