"""The Stripe webhook decides who has paid, and nothing ran it until now.

`grep -rln "_apply_event" tests/` found exactly one file before this one —
`test_tenant_scoping_corpus.py`, which reads the SQL as TEXT and never executes
it. So the four statements that move a company between `trialing`, `active`,
`past_due` and `canceled` had no behavioural coverage at all, two days before a
paid launch. That is gotcha 71's shape (`run_agent_streaming` had no test that
ran it) pointed at revenue instead of at chat.

**The defect these tests were written to catch.** Every handler keys on a
STRIPE id, so "no such row" is reachable rather than impossible — and
`pool.execute` reports a zero-row update as an ordinary success. The
subscription handler set `stripe_subscription_id` and never
`stripe_customer_id`, while both invoice handlers key on `stripe_customer_id`
ALONE. So any subscription that reached us matching on `company_id` metadata —
one created from the Stripe dashboard, from a payment link, or from a checkout
whose `create_customer` succeeded before the upsert failed — left that column
NULL, after which:

* `invoice.paid` matched zero rows, so `period_start` never rolled and the
  customer burned ONE allowance and stayed locked out with `turns_exhausted`
  while paying every month;
* `invoice.payment_failed` matched zero rows;
* `/billing/portal` returned 409, so they could not repair it themselves.

Fourth instance of the `update … where` that lands on nothing (gotchas 73, 76,
81), and the first one on the money path.

⚠ These drive `_apply_event` DIRECTLY rather than posting to the route. The
route's job is signature verification and swallowing, which is separately
worth testing; putting both in one test would mean a signature helper change
could turn a billing-state assertion red for an unrelated reason. What has to
be pinned here is which row each event lands on and what it writes.

⚠ The fake pool returns asyncpg's real command tag (`"UPDATE 1"` / `"UPDATE
0"`), not a MagicMock. A fake that answers whatever it is handed proves
nothing about a wire contract (gotcha 79), and the zero-row warning reads that
exact string — a MagicMock would make the diagnostic untestable while looking
covered.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import pytest

from helixis.api.routes import billing


class FakePool:
    """Records every statement and reports how many rows it 'touched'.

    `rows_touched` is the knob the whole file turns: it is the difference
    between a handler that applied and one that silently landed on nothing,
    and it is invisible to the caller in production.
    """

    def __init__(self, rows_touched: int = 1) -> None:
        self.rows_touched = rows_touched
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        return f"UPDATE {self.rows_touched}"

    # The handlers under test only execute; fetchrow is here so a future
    # handler that reads cannot silently get a MagicMock instead of a row.
    async def fetchrow(self, sql: str, *args: Any) -> None:  # pragma: no cover
        self.calls.append((sql, args))
        return None

    @property
    def only_sql(self) -> str:
        assert len(self.calls) == 1, f"expected one statement, got {len(self.calls)}"
        return " ".join(self.calls[0][0].split())

    @property
    def only_args(self) -> tuple[Any, ...]:
        assert len(self.calls) == 1, f"expected one statement, got {len(self.calls)}"
        return self.calls[0][1]


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> FakePool:
    fake = FakePool()
    monkeypatch.setattr(billing, "get_pool", lambda: fake)
    return fake


@pytest.fixture
def settings() -> Any:
    """Real settings, so `_plan_for_price` runs its true price lookup.

    An unconfigured deployment maps no price id to a plan, which is the
    `plan = coalesce(NULL, plan)` case the SQL is written for — the correct
    default for these tests, since none of them is about plan resolution.
    """
    return billing._settings()


SUBSCRIPTION = {
    "id": "sub_123",
    "customer": "cus_abc",
    "status": "active",
    "current_period_start": 1_756_000_000,
    "current_period_end": 1_758_000_000,
    "metadata": {"company_id": "5ca1ab1e-0000-4000-8000-000000000002"},
    "items": {"data": [{"quantity": 3, "price": {"id": "price_x"}}]},
}


# --------------------------------------------------------------------------
# The regression. This is the whole reason the file exists.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscription_event_records_the_stripe_customer_id(
    pool: FakePool, settings: Any
) -> None:
    """Without this the two invoice handlers can never match the row again.

    The subscription event is the FIRST one that carries the customer id, and
    for a subscription that did not come from our own checkout it is the only
    one that will ever reach us while the row is still findable — every later
    invoice looks the company up BY that column.
    """
    await billing._apply_event(settings, "customer.subscription.created", SUBSCRIPTION)

    sql = pool.only_sql
    assert "stripe_customer_id = coalesce($8, stripe_customer_id)" in sql, (
        "the subscription handler no longer learns the Stripe customer id.\n\n"
        "Both invoice handlers key on `stripe_customer_id` ALONE, so a row that "
        "never receives it is unreachable by every later event: `invoice.paid` "
        "stops rolling the metering window and the customer is locked out with "
        "`turns_exhausted` while paying every month, and /billing/portal 409s so "
        "they cannot fix it themselves. A subscription created from the Stripe "
        "dashboard, from a payment link, or by a checkout whose create_customer "
        "succeeded before the upsert failed matches on `company_id` metadata "
        "only — this assignment is what repairs it."
    )
    assert pool.only_args[7] == "cus_abc", "arg $8 must be the Stripe customer id"


@pytest.mark.asyncio
async def test_the_customer_id_is_coalesced_so_a_missing_one_cannot_erase_it(
    pool: FakePool, settings: Any
) -> None:
    """A bare assignment would blank the column on any object without it.

    That is strictly worse than the bug being fixed: it takes a row that WAS
    reachable and makes it unreachable, so the repair has to be additive.
    """
    await billing._apply_event(settings, "customer.subscription.updated", SUBSCRIPTION)
    assert re.search(
        r"stripe_customer_id\s*=\s*coalesce\(\$8,\s*stripe_customer_id\)", pool.only_sql
    ), (
        "stripe_customer_id must be COALESCEd, never assigned bare — a Stripe "
        "object arriving without `customer` would otherwise erase the id we "
        "already hold and unreach a row that was previously fine"
    )


@pytest.mark.asyncio
async def test_learning_the_customer_id_cannot_change_which_row_matched(
    pool: FakePool, settings: Any
) -> None:
    """The new assignment is in SET, and SET is evaluated against the OLD row.

    Worth pinning rather than assuming: if the column were ever moved into the
    WHERE, or the statement rewritten as a CTE that reads its own output, the
    handler could start matching rows it just wrote — and this test is the only
    thing standing between that and a cross-account write.
    """
    await billing._apply_event(settings, "customer.subscription.created", SUBSCRIPTION)
    where = pool.only_sql.split(" where ", 1)[1]
    assert "stripe_customer_id = coalesce" not in where, (
        "the customer-id assignment leaked into the WHERE clause — matching must "
        "stay on the pre-update row"
    )
    assert where.strip() == "company_id = $1 or stripe_customer_id = $8", (
        f"the subscription match keys changed to: {where.strip()!r}. That decides "
        "which company's subscription this event rewrites, so a change here needs "
        "to be deliberate."
    )


# --------------------------------------------------------------------------
# Zero-row visibility — the class, not just this instance.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("etype", "obj"),
    [
        ("customer.subscription.created", SUBSCRIPTION),
        ("customer.subscription.deleted", {"id": "sub_123", "customer": "cus_abc"}),
        ("invoice.payment_failed", {"customer": "cus_abc"}),
        ("invoice.paid", {"customer": "cus_abc", "lines": {"data": [{"period": {}}]}}),
    ],
)
@pytest.mark.asyncio
async def test_every_handler_warns_when_it_lands_on_nothing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    settings: Any,
    etype: str,
    obj: dict[str, Any],
) -> None:
    """A zero-row update is indistinguishable from a successful one otherwise.

    Parametrised across ALL FOUR handlers on purpose: the specific defect this
    file was written for is fixed, but the shape recurs (gotchas 73, 76, 81 are
    the same sentence three times), so the guard has to cover the class rather
    than the instance that happened to be found.
    """
    fake = FakePool(rows_touched=0)
    monkeypatch.setattr(billing, "get_pool", lambda: fake)

    with caplog.at_level(logging.WARNING, logger=billing.logger.name):
        await billing._apply_event(settings, etype, obj)

    assert any("matched no billing row" in r.message for r in caplog.records), (
        f"{etype} updated zero rows and said nothing. On this path that means a "
        "company's subscription state is silently stale forever — the exact "
        "failure mode gotchas 73/76/81 record, here deciding whether somebody "
        "who paid can use the product."
    )
    assert any("cus_abc" in r.getMessage() for r in caplog.records), (
        "the warning must name the key it looked the row up by, or it says an "
        "event failed without saying which customer to repair"
    )


@pytest.mark.asyncio
async def test_a_handler_that_applied_stays_quiet(
    caplog: pytest.LogCaptureFixture, pool: FakePool, settings: Any
) -> None:
    """The other direction, and it is what keeps the warning worth reading.

    A diagnostic that fires on healthy events is one people filter out, after
    which the real occurrence is invisible (gotcha 52's reasoning about guards
    that get switched off).
    """
    with caplog.at_level(logging.WARNING, logger=billing.logger.name):
        await billing._apply_event(settings, "invoice.paid", {"customer": "cus_abc"})
    assert not [r for r in caplog.records if "matched no billing row" in r.message], (
        "a successful update emitted the zero-row warning"
    )


def test_the_zero_row_check_never_raises_on_an_unparseable_tag() -> None:
    """It runs inside a handler whose exceptions are swallowed and acknowledged.

    So a diagnostic that raised would be invisible AND would skip the rest of
    the handler — breaking the thing it was added to observe. A fake pool
    returning a MagicMock must be ignored, not read as zero rows, or every
    test in every other file that touches billing emits a false alarm.
    """
    from unittest.mock import MagicMock

    for tag in (None, MagicMock(), "", "INSERT 0 1", "UPDATE", "UPDATE nope"):
        billing._warn_if_nothing_matched("invoice.paid", tag, "customer", "cus_abc")


# --------------------------------------------------------------------------
# What each event actually writes.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cancelled_subscription_falls_back_to_the_smallest_plan(
    pool: FakePool, settings: Any
) -> None:
    """`canceled` + `trial`, never `canceled` + whatever they were paying for.

    The gate reads status first, so the plan only matters if the row is later
    revived — and reviving it onto `pro` would hand out the largest allowance
    we sell to an account that stopped paying.
    """
    await billing._apply_event(
        settings, "customer.subscription.deleted", {"id": "sub_123", "customer": "cus_abc"}
    )
    sql = pool.only_sql
    assert "status = 'canceled'" in sql and "plan = 'trial'" in sql, (
        "a deleted subscription must land on the SMALLEST plan we sell; the gate "
        "reads status first, but a row later revived onto its old paid plan would "
        "hand out that allowance for free"
    )
    assert "stripe_subscription_id = null" in sql


@pytest.mark.asyncio
async def test_a_paid_invoice_rolls_the_metering_window(pool: FakePool, settings: Any) -> None:
    """This is what makes the turn allowance reset when they are actually billed.

    `billing/gate.py` counts turns since `period_start`, so an invoice that does
    not move it leaves a paying customer on an allowance they already spent.
    """
    await billing._apply_event(
        settings,
        "invoice.paid",
        {"customer": "cus_abc", "lines": {"data": [{"period": {"start": 1, "end": 2}}]}},
    )
    sql = pool.only_sql
    assert "period_start = coalesce(to_timestamp($2), period_start)" in sql
    assert "period_end = coalesce(to_timestamp($3), period_end)" in sql
    assert "past_due_since = null" in sql, "a paid invoice must end the dunning window"
    # ⚠ Positional, and asserted by SLICE rather than as the whole tail. The
    # handler also binds an event-ordering stamp ($4, migration 0093), which is
    # `None` here because this event carries no `created` — pinning the entire
    # remainder made the test fail on an argument it was never about.
    assert pool.only_args[1:3] == (1, 2), "the billing period must come from the invoice line"


@pytest.mark.asyncio
async def test_a_failed_payment_does_not_cut_service(pool: FakePool, settings: Any) -> None:
    """`past_due` is in `_SERVING_STATUSES` deliberately (gate.py rule 2).

    A declined card is the customer's bank, not their intent, and Stripe retries
    for days before it gives up. So this handler marks the row and must not
    touch plan or subscription id — `customer.subscription.deleted` is what
    actually stops service.
    """
    from helixis.billing.gate import _SERVING_STATUSES

    await billing._apply_event(settings, "invoice.payment_failed", {"customer": "cus_abc"})
    sql = pool.only_sql
    assert "status = 'past_due'" in sql
    assert "plan" not in sql, (
        "a failed payment must not change the plan — Stripe retries for days and "
        "the customer keeps the tier they bought while it does"
    )
    assert "past_due" in _SERVING_STATUSES, (
        "gate.py stopped serving past_due companies, so a single declined card "
        "now cuts service the hour it fails. That is a deliberate product "
        "decision, not a refactor — if it is intended, this test states the rule "
        "that changed."
    )


@pytest.mark.asyncio
async def test_an_unhandled_event_touches_nothing(pool: FakePool, settings: Any) -> None:
    """Stripe sends dozens of types we do not model.

    Each must be a no-op rather than an error: the route acknowledges everything
    once the signature verifies, because a non-2xx makes Stripe retry for days
    and eventually disable the endpoint.
    """
    await billing._apply_event(settings, "customer.discount.created", {"customer": "cus_abc"})
    assert pool.calls == [], "an unmodelled event wrote to the billing table"


# ---------------------------------------------------------------------------
# An unstorable status used to discard the WHOLE event
# ---------------------------------------------------------------------------
#
# `helixis_billing_status_known` listed five values (0087) and Stripe documents
# eight. `status = $4` was unconditional, so `incomplete_expired`, `unpaid` and
# `paused` each raised 23514 — which `stripe_webhook` swallows, correctly. The
# cost was never the status column: this statement carries the subscription id,
# the customer id, the plan, the seats, the metering window and the ordering
# stamp, and one rejected column discarded all of them.
#
# Migration 0097 widens the constraint; these pin the Python half, which is what
# has to hold for a status Stripe invents AFTER that file.


@pytest.mark.asyncio
async def test_a_status_stripe_invents_later_does_not_discard_the_event(
    pool: FakePool, settings: Any
) -> None:
    """The load-bearing one. An unrecognised status must cost the STATUS and
    nothing else — the metering window in particular, because a company whose
    `period_start` stops rolling burns one allowance and is then locked out
    with `turns_exhausted` while paying every month."""
    await billing._apply_event(
        settings,
        "customer.subscription.updated",
        {**SUBSCRIPTION, "status": "some_status_from_2027"},
    )
    sql, args = pool.only_sql, pool.only_args
    assert args[3] is None, (
        "an unrecognised status must be passed as NULL. Passing the raw string "
        "raises 23514 and the swallow takes the rest of the event with it; "
        "clamping it to a guess either serves the product free or locks out a "
        "payer."
    )
    assert "status = coalesce($4, status)" in sql, (
        "the column must keep its previous value when $4 is NULL — a bare "
        "`status = $4` writes NULL into a NOT NULL column and fails the whole "
        "statement in a different way"
    )
    # The rest of the event still lands, which is the entire point.
    assert args[1] == "sub_123"
    assert args[4] == SUBSCRIPTION["items"]["data"][0]["quantity"]
    assert args[5] == SUBSCRIPTION["current_period_start"]
    assert args[6] == SUBSCRIPTION["current_period_end"]
    assert args[7] == "cus_abc"


@pytest.mark.asyncio
async def test_an_unrecognised_status_is_reported_at_error(
    pool: FakePool, settings: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Keeping the old value is the safe direction and it is still WRONG state
    that will not self-correct, so it has to reach somebody. ERROR rather than
    warning for `_warn_if_nothing_matched`'s reason: Sentry events at ERROR and
    keeps WARNING as a breadcrumb, and this failure is silent by nature."""
    with caplog.at_level(logging.ERROR, logger=billing.logger.name):
        await billing._apply_event(
            settings,
            "customer.subscription.updated",
            {**SUBSCRIPTION, "status": "some_status_from_2027"},
        )
    assert any("some_status_from_2027" in r.getMessage() for r in caplog.records), (
        "the log must name the status that arrived — without it the operator "
        "knows an event failed and not which value to add"
    )


@pytest.mark.asyncio
async def test_a_missing_status_is_not_defaulted_to_active(pool: FakePool, settings: Any) -> None:
    """The previous code read `str(obj.get("status") or "active")`, so a
    malformed or truncated event became a SERVING subscription — the one
    direction that hands out the product for free."""
    payload = {k: v for k, v in SUBSCRIPTION.items() if k != "status"}
    await billing._apply_event(settings, "customer.subscription.updated", payload)
    assert pool.only_args[3] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["incomplete_expired", "unpaid", "paused"])
async def test_the_three_statuses_the_constraint_used_to_reject_now_store(
    pool: FakePool, settings: Any, status: str
) -> None:
    """These are the ones that were actually broken, named individually so a
    re-narrowing of the vocabulary fails here as well as in the corpus guard.

    `unpaid` is the worst: the row kept `past_due`, which IS in
    `_SERVING_STATUSES`, so the product was served free and indefinitely to a
    customer Stripe had stopped billing.
    """
    await billing._apply_event(
        settings, "customer.subscription.updated", {**SUBSCRIPTION, "status": status}
    )
    assert pool.only_args[3] == status


@pytest.mark.asyncio
async def test_an_unrecognised_status_does_not_erase_the_dunning_window(
    pool: FakePool, settings: Any
) -> None:
    """`past_due_since` is keyed on the EFFECTIVE status, not on $4.

    A bare `status` in a SET expression reads the PRE-UPDATE row, so
    `coalesce($4, status)` is the value actually being stored. Keyed on $4
    alone, an unrecognised status (NULL) would take the `else null` arm and
    silently erase when collection started — for a company whose status is
    being left alone precisely because we could not read it.
    """
    await billing._apply_event(
        settings, "customer.subscription.updated", {**SUBSCRIPTION, "status": "unpaid"}
    )
    sql = pool.only_sql
    assert "case when coalesce($4, status) = 'past_due'" in sql, (
        "the dunning window must be decided by the status being STORED, not by "
        "the parameter, or an unreadable status erases past_due_since"
    )
