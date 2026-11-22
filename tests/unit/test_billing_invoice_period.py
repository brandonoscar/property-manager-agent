"""The metering window must roll from the SUBSCRIPTION line, not `lines.data[0]`.

⚠ THIS DEFECT BECAME REACHABLE THE DAY `change-plan` SHIPPED, which is why it
has tests now and did not before. An invoice's `lines` array is not just the
recurring charge — a mid-cycle plan change adds PRORATION lines, and a
proration's `period` is the slice being credited (the eleven days left in the
month), not the billing period.

`period_start` / `period_end` are what `gate.py` counts turns against. Write a
proration's slice into them and the customer who just upgraded gets their
allowance measured against a few days, or a `period_end` in the past.
"""

from __future__ import annotations

from typing import Any

from helixis.api.routes.billing import _billing_period_from_invoice

MONTH = {"start": 1_756_000_000, "end": 1_758_600_000}
PRORATION = {"start": 1_757_000_000, "end": 1_757_200_000}


def _invoice(*lines: dict[str, Any]) -> dict[str, Any]:
    return {"id": "in_1", "lines": {"data": list(lines)}}


def test_a_proration_first_does_not_become_the_billing_period() -> None:
    """THE DEFECT, in the shape a plan change actually produces: Stripe puts
    the proration first and the subscription line after it."""
    got = _billing_period_from_invoice(
        _invoice(
            {"type": "invoiceitem", "proration": True, "period": PRORATION},
            {"type": "subscription", "period": MONTH},
        )
    )
    assert got == MONTH, (
        "the metering window rolled from a proration line — the customer's "
        "turn allowance is now measured against a partial cycle"
    )


def test_the_2025_parent_shape_is_recognised() -> None:
    """⚠ Stripe MOVED this field. Newer API versions drop `type` in favour of
    `parent.type`, and an account pinned to one of those would otherwise fall
    straight back to `data[0]` — the bug, silently, on exactly the accounts
    created most recently."""
    got = _billing_period_from_invoice(
        _invoice(
            {"period": PRORATION},
            {"parent": {"type": "subscription_item_details"}, "period": MONTH},
        )
    )
    assert got == MONTH


def test_a_bare_subscription_id_is_the_third_tell() -> None:
    got = _billing_period_from_invoice(
        _invoice({"period": PRORATION}, {"subscription": "sub_1", "period": MONTH})
    )
    assert got == MONTH


def test_an_ordinary_renewal_is_unchanged() -> None:
    """One line, no ambiguity — the common case must behave exactly as before."""
    assert (
        _billing_period_from_invoice(_invoice({"type": "subscription", "period": MONTH})) == MONTH
    )


def test_an_unrecognisable_invoice_falls_back_rather_than_declining() -> None:
    """⚠ The asymmetry, and it is deliberate. This statement ROLLS THE METERING
    WINDOW; the handler's own comment calls a miss here "the costliest of the
    four" — a company it does not update keeps last period's `period_start` and
    exhausts one allowance permanently while paying every month. So a shape we
    cannot read behaves exactly as it did before: a correction where we can
    see, a no-op where we cannot."""
    assert _billing_period_from_invoice(_invoice({"period": PRORATION})) == PRORATION


def test_an_invoice_with_no_lines_returns_nothing_rather_than_raising() -> None:
    """`coalesce` in the SQL then leaves the stored window alone. Raising here
    would abort a handler that also clears dunning."""
    assert _billing_period_from_invoice({"lines": {"data": []}}) == {}
    assert _billing_period_from_invoice({}) == {}
