"""Reading money off a Buildium record, once, for every layer that needs it.

There is exactly one rule here and three call sites that must share it: the
live agent tool, the mirror ingest, and the webhook handler. They did not
share it before, which is why fixing the agent's ``$0 unpaid bills`` answer
left the mirror — the path the hybrid policy actually prefers — still
reporting ``$0``.

The same module now owns the *sign* rule for ledger rows, for the same
reason: it was written down correctly in one module and never reached the
one that put a number on screen.
"""

from __future__ import annotations

from typing import Any

#: Every ledger row is signed by its EFFECT ON THE BALANCE, so a payment is
#: NEGATIVE. Measured on helixis-test 2026-09-10 with the control in the same
#: result set — which is what makes it evidence rather than a coincidence:
#: ``Payment`` 144/144 negative (-2572.50 … -100.00) against ``Charge``
#: 619/619 positive. Also ``Credit`` 20/20 and ``Applied Deposit`` 2/2
#: negative, ``Refund`` 3/3 positive.
PAYMENTS_ARE_NEGATIVE = True


def payments_received_sql(column: str = "t.total_amount") -> str:
    """SQL for *money actually received*, as a positive number.

    ⚠ **This is a function, not a constant, because both halves are fatal in
    OPPOSITE directions and a caller that hand-writes one usually forgets the
    other.**

    * **Negate, never ``ABS``.** A positive ``Payment`` row is money going
      back OUT (a reversal), and it must subtract. ``ABS`` would count a
      refund as income — the same arithmetic that makes a bad month look
      like a good one. ``reconciliation/analyst.py`` reached this conclusion
      first and its comment says so; this function exists so the next caller
      inherits it instead of re-deriving it.
    * **``COALESCE``, never a bare ``SUM``.** ``SUM`` over no rows is
      ``NULL``, not ``0`` — and ``NULL`` compared to anything is ``NULL``,
      which is falsy. A gate written without this silently stops firing on
      exactly the population it exists for.

    The failure this was extracted from: ``db/repositories/cockpit.py``
    summed ``Payment`` rows un-negated into a field named
    ``rent_collected_mtd`` and rendered it as "$X collected this month" in
    BOTH the property cockpit and the agent's own property snapshot — so a
    property that collected rent displayed a NEGATIVE figure. The frontend's
    own mock fixture (``services/properties.ts``) carries a positive number,
    which is the tell: the fixture stated the intent while the query did the
    opposite.
    """
    return f"COALESCE(-SUM({column}), 0)"


def bill_amount(bill: Any) -> float | None:
    """The money on one bill — ``TotalAmount`` when Buildium sends it, else
    the sum of ``Lines[].Amount``.

    ⚠ Production bills carry NO top-level ``TotalAmount``. Live-verified
    2026-08-21 across all 67 unpaid bills on account 695763: not one had the
    field. Reading only that field made every bill "unpriced", so the
    assistant reported **$0 total vendor exposure** against a real $26,000 —
    it correctly refused to invent a number, but the number was sitting in
    the line items. Same class as gotcha 24: a field Buildium does not put
    where the code assumed.

    Returns ``None`` only when neither source has a usable number, so callers
    can say "excludes N bills with no amount recorded" rather than quietly
    folding them in as zero. A caller that stores this must keep NULL
    meaning "unknown" — writing 0.0 would launder a missing amount into a
    real-looking figure.
    """
    total = getattr(bill, "total_amount", None)
    if isinstance(total, (int, float)):
        return float(total)
    lines = getattr(bill, "lines", None) or []
    line_amounts = [
        float(amount)
        for line in lines
        if isinstance(amount := getattr(line, "amount", None), (int, float))
    ]
    return sum(line_amounts) if line_amounts else None


def bill_is_paid(bill: Any) -> bool | None:
    """Whether a bill is paid — ``IsPaid`` when it says anything, else
    "does it have a ``PaidDate``".

    ⚠ ``IsPaid`` is present as a key on every production bill and **NULL on
    every one of them** (live-verified across all 131 mirrored rows,
    2026-08-21). Storing it verbatim left the mirror unable to tell paid from
    unpaid, and ``WHERE is_paid IS NOT TRUE`` then counted all 131 bills as
    outstanding when 32 of them were paid. ``PaidDate`` is the field that
    carries the answer: null = unpaid, set = paid.

    Fourth instance of the gotcha-25 class in one month, and the reason
    ``PaidDate`` had to be TYPED on ``Bill``: as an untyped extra it is stored
    under its original casing, so ``bill.paid_date`` silently read ``None``.
    """
    flag = getattr(bill, "is_paid", None)
    if isinstance(flag, bool):
        return flag
    return getattr(bill, "paid_date", None) is not None


__all__ = ["PAYMENTS_ARE_NEGATIVE", "bill_amount", "bill_is_paid", "payments_received_sql"]
