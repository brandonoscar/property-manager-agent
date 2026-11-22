"""A delayed Stripe retry must not hand a cancelled customer their plan back.

Every handler in `_apply_event` is an unconditional UPDATE, so the ORDER events
arrive in is the order they take effect — and Stripe's order is not ours. It
retries a failed delivery with backoff for up to three days, which is plenty of
time for the customer to cancel in between:

    subscription.updated  ──(our endpoint 500s, queued for retry)──┐
    subscription.deleted  ──(applied)── row: canceled / trial      │
    subscription.updated  ←────────────(retry lands an hour later)─┘
                                        row: active / pro, forever

Nothing anywhere reports that. `_warn_if_nothing_matched` checks that a row was
matched, and one was. The tests below are the only thing standing between that
sequence and a free paid account.

⚠ Read `_is_delayed_retry`'s asymmetry before changing anything here: every
uncertain answer is "proceed". The defect is rare and a false positive is a
paying customer whose signup event was silently dropped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.api.routes import billing as billing_routes

CUSTOMER = "cus_live"
# The stamp already on the row: a `subscription.deleted` applied at 12:00.
APPLIED_AT = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
NEWER = APPLIED_AT.timestamp() + 60
OLDER = APPLIED_AT.timestamp() - 3600


def _pool(newest: Any = APPLIED_AT) -> MagicMock:
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=newest)
    pool.execute = AsyncMock(return_value="UPDATE 1")
    return pool


@pytest.mark.asyncio
async def test_an_event_older_than_the_last_one_applied_is_refused() -> None:
    """THE DEFECT. A `subscription.updated` created before the `deleted` that
    has already been applied is a delayed retry, not news."""
    stale = await billing_routes._is_delayed_retry(_pool(), {"customer": CUSTOMER}, OLDER)
    assert stale is True, (
        "an event older than one already applied was treated as current — a "
        "queued subscription.updated will resurrect a cancelled plan"
    )


@pytest.mark.asyncio
async def test_a_newer_event_still_applies() -> None:
    """The other direction, and the one that matters more often: ordinary
    forward progress must not be mistaken for a retry."""
    assert await billing_routes._is_delayed_retry(_pool(), {"customer": CUSTOMER}, NEWER) is False


@pytest.mark.asyncio
async def test_two_events_in_the_same_second_both_apply() -> None:
    """⚠ Stripe emits `invoice.paid` and `customer.subscription.updated` in the
    SAME second off one charge. Refusing on equality drops whichever arrives
    second, and if that is the invoice the metering window never rolls — the
    costliest miss the handler names."""
    assert (
        await billing_routes._is_delayed_retry(
            _pool(), {"customer": CUSTOMER}, APPLIED_AT.timestamp()
        )
        is False
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("obj", "created", "why"),
    [
        ({}, NEWER, "no customer on the event — nothing to look up"),
        ({"customer": CUSTOMER}, None, "no timestamp — nothing to compare"),
        ({"customer": CUSTOMER}, "not-a-number", "unparseable timestamp"),
    ],
)
async def test_every_unanswerable_question_proceeds(
    obj: dict[str, Any], created: Any, why: str
) -> None:
    """⚠ FAIL OPEN, deliberately. A guard that cannot see must not refuse: the
    thing it would drop is the event that records a signup or rolls a metering
    window, and locking out a paying customer is worse than the rare
    resurrection this exists to prevent."""
    assert await billing_routes._is_delayed_retry(_pool(), obj, created) is False, why


@pytest.mark.asyncio
async def test_a_row_that_has_never_seen_an_event_proceeds() -> None:
    """NULL is "nothing applied yet", never "older than everything". Every
    company the 0088 trigger creates starts here, so reading NULL as stale
    would refuse the first subscription event a new customer ever produces."""
    assert (
        await billing_routes._is_delayed_retry(_pool(newest=None), {"customer": CUSTOMER}, OLDER)
        is False
    )


@pytest.mark.asyncio
async def test_a_read_that_raises_proceeds() -> None:
    """Same asymmetry: a database blip must not silently discard billing
    events. It logs and lets the event through."""
    pool = _pool()
    pool.fetchval = AsyncMock(side_effect=RuntimeError("pool is gone"))
    assert await billing_routes._is_delayed_retry(pool, {"customer": CUSTOMER}, OLDER) is False


@pytest.mark.asyncio
async def test_a_stale_event_writes_NOTHING(monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: N802
    """The guard is only worth having if `_apply_event` actually returns before
    its UPDATE. ⚠ Pairs with the unit tests above rather than replacing them —
    gotcha 62: a test that drives a helper directly cannot see a caller that
    ignores it."""
    pool = _pool()
    monkeypatch.setattr(billing_routes, "get_pool", lambda: pool)
    await billing_routes._apply_event(
        MagicMock(),
        "customer.subscription.updated",
        {"customer": CUSTOMER, "id": "sub_1", "status": "active"},
        OLDER,
    )
    assert not pool.execute.await_args_list, (
        "a delayed retry still wrote to helixis_billing — the guard is computed and then ignored"
    )


@pytest.mark.asyncio
async def test_every_handler_records_the_stamp_it_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ The guard compares against a column SOMETHING has to write. A handler
    that applies an event without stamping the row leaves the ordering check
    permanently blind for that customer — the column stays NULL, which reads as
    ALLOW, and the resurrection is back with a test that passes."""
    pool = _pool(newest=None)
    monkeypatch.setattr(billing_routes, "get_pool", lambda: pool)

    events = {
        "customer.subscription.created": {"customer": CUSTOMER, "id": "sub_1"},
        "customer.subscription.updated": {"customer": CUSTOMER, "id": "sub_1"},
        "customer.subscription.deleted": {"customer": CUSTOMER, "id": "sub_1"},
        "invoice.payment_failed": {"customer": CUSTOMER},
        "invoice.paid": {"customer": CUSTOMER, "lines": {"data": []}},
        "invoice.payment_succeeded": {"customer": CUSTOMER, "lines": {"data": []}},
    }
    for etype, obj in events.items():
        pool.execute.reset_mock()
        await billing_routes._apply_event(MagicMock(), etype, obj, NEWER)
        assert pool.execute.await_args_list, f"{etype} wrote nothing at all"
        sql = pool.execute.await_args_list[0].args[0]
        assert "last_stripe_event_at" in sql, (
            f"{etype} applies an event without recording when it happened — the "
            "ordering guard has nothing to compare the next event against"
        )
        # The stamp must actually be among the bound arguments, not merely
        # named in the SQL: `coalesce(to_timestamp($n), ...)` with nothing
        # bound to it would write NULL on every event.
        assert NEWER in pool.execute.await_args_list[0].args[1:], (
            f"{etype} names the column but never binds the timestamp"
        )
