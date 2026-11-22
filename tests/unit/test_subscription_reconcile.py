"""The customer paid, the webhook was lost, and nothing asked Stripe again.

Every fact this product holds about a subscription arrives by webhook. Lose
one and the row keeps what it had, forever, in whichever direction hurts:

* a lost ``subscription.created`` leaves a PAYING customer on the trial
  allowance, refused with ``turns_exhausted`` while their card is charged;
* a lost ``subscription.deleted`` serves the paid plan to somebody who
  cancelled.

⚠ ``seats_cycle`` cannot cover either, and its own docstring says why: it
sweeps "only companies with a subscription", which is correct for repairing a
seat QUANTITY and excludes by construction the company missing its
subscription id. That is gotcha 62's finding — *a sweep whose population query
silently excludes the companies that need it* — so the population here is
different on purpose, and one test pins that difference rather than the SQL.

⚠ The assertions that matter most are the ones about doing NOTHING. A
reconciler that reads a failed Stripe call as "they cancelled" turns one
outage into a fleet-wide downgrade, which is strictly worse than the lost
webhook it exists to repair.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.billing.subscription_reconcile import (
    MAX_COMPANIES_PER_SWEEP,
    _current_subscription,
    _drifted,
    run_subscription_reconcile_cycle,
)

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "helixis"
WORKER = SRC / "webhooks" / "worker.py"
MODULE = SRC / "billing" / "subscription_reconcile.py"

COMPANY = "5ca1ab1e-0000-4000-8000-000000000004"
CUSTOMER = "cus_live"


def _settings(key: str = "sk_test_x") -> Any:
    s = MagicMock()
    s.billing.stripe_secret_key = key
    return s


def _row(**over: Any) -> dict[str, Any]:
    base = {
        "company_id": COMPANY,
        "stripe_customer_id": CUSTOMER,
        "stripe_subscription_id": None,
        "status": None,
        "plan": "trial",
    }
    return {**base, **over}


def _sub(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "sub_live",
        "status": "active",
        "customer": CUSTOMER,
        "items": {"data": [{"price": {"id": "price_starter"}, "quantity": 1}]},
    }
    return {**base, **over}


def _pool(rows: list[dict[str, Any]]) -> Any:
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=rows)
    return pool


def _run(rows: list[dict[str, Any]], subs: list[dict[str, Any]], **kw: Any) -> Any:
    """Drive the real cycle over a fake Stripe and a fake pool.

    Returns ``(repaired, applied_calls)`` where ``applied_calls`` is every
    ``_apply_event`` invocation — the thing that actually writes the row.
    """
    applied: list[tuple[Any, ...]] = []

    async def fake_apply(settings: Any, etype: str, obj: dict[str, Any], created: Any = None):
        applied.append((etype, obj, created))

    client = MagicMock()
    client.list_subscriptions = AsyncMock(return_value=subs)

    with (
        patch("helixis.billing.subscription_reconcile.StripeClient", return_value=client),
        patch("helixis.api.routes.billing._apply_event", new=fake_apply),
        patch("helixis.api.routes.billing._plan_for_price", new=kw.get("plan_for_price", _plan)),
    ):
        import asyncio

        repaired = asyncio.run(
            run_subscription_reconcile_cycle(_pool(rows), kw.get("settings") or _settings())
        )
    return repaired, applied


def _plan(_settings_arg: Any, price_id: str | None) -> str | None:
    return {"price_starter": "starter", "price_scale": "scale"}.get(price_id or "")


# ── the repair ───────────────────────────────────────────────────────


def test_a_lost_created_webhook_is_repaired_from_stripe() -> None:
    """The headline case: we hold the customer id (checkout writes it
    synchronously) and no subscription id, because the webhook never came."""
    repaired, applied = _run([_row()], [_sub()])

    assert repaired == 1
    assert len(applied) == 1
    etype, obj, created = applied[0]
    assert etype == "customer.subscription.updated"
    assert obj["id"] == "sub_live"
    assert created is not None, "the ordering guard needs a stamp or the repair is refused"


def test_a_lost_cancellation_is_repaired_too() -> None:
    """⚠ Same subscription ID, different status — the case an id-only check
    cannot see. Half this module's reason to exist would be structurally
    unreachable without the status comparison."""
    repaired, applied = _run(
        [_row(stripe_subscription_id="sub_live", status="active", plan="starter")],
        [_sub(status="canceled")],
    )

    assert repaired == 1
    assert applied[0][1]["status"] == "canceled"


def test_a_lost_plan_change_is_repaired() -> None:
    """Same id, same active status, a different price. Charged for one plan,
    allowanced on another."""
    repaired, _ = _run(
        [_row(stripe_subscription_id="sub_live", status="active", plan="starter")],
        [_sub(items={"data": [{"price": {"id": "price_scale"}, "quantity": 1}]})],
    )
    assert repaired == 1


def test_the_company_id_is_ours_and_not_the_objects() -> None:
    """⚠ ``_apply_event`` matches on ``company_id OR stripe_customer_id``, and
    a subscription created outside checkout (the Stripe dashboard, an import,
    a support action) carries no metadata. Passing the object's absent one
    would narrow the repair to exactly the rows least likely to need it."""
    _, applied = _run([_row()], [_sub(metadata={})])
    assert applied[0][1]["metadata"]["company_id"] == COMPANY


# ── the assertions about doing nothing ───────────────────────────────


def test_an_up_to_date_company_is_not_rewritten() -> None:
    """Idempotent by construction: a healthy fleet writes no rows."""
    repaired, applied = _run(
        [_row(stripe_subscription_id="sub_live", status="active", plan="starter")],
        [_sub()],
    )
    assert (repaired, applied) == (0, [])


def test_an_empty_stripe_answer_changes_nothing(caplog: Any) -> None:
    """⚠ THE ONE THAT MATTERS MOST. ``list_subscriptions`` returns ``[]`` for
    "no subscriptions" AND for "the call failed" — indistinguishable by
    design. Reading it as a cancellation would turn one Stripe outage into a
    fleet-wide downgrade, which is worse than the lost webhook being repaired.

    This is also the abandoned-checkout shape: a customer id with no
    subscription is a normal, permanent state, not a defect.

    ⚠ **Asserting `(0, [])` alone is not enough, and a mutation is what showed
    it.** The per-company handler swallows exceptions, so a version that
    CRASHED on the empty list would produce the identical outcome — declined
    correctly and blew up are the same tuple. The log assertion is what
    separates them: this path must decline knowingly, not survive by accident.
    """
    import logging

    with caplog.at_level(logging.ERROR, logger="helixis.billing.subscription_reconcile"):
        repaired, applied = _run([_row()], [])

    assert (repaired, applied) == (0, [])
    assert not [r for r in caplog.records if r.exc_info], (
        "the empty answer was survived rather than handled — an exception was "
        "swallowed, so the decline is accidental"
    )


def test_no_stripe_key_does_nothing_and_reads_no_rows() -> None:
    """The normal state of a deployment that has not switched billing on. It
    must not even query — a sweep that costs a fleet-wide read to decide it
    has nothing to do is a sweep somebody turns off."""
    pool = _pool([_row()])
    import asyncio

    assert asyncio.run(run_subscription_reconcile_cycle(pool, _settings(key=""))) == 0
    pool.fetch.assert_not_awaited()


def test_an_unreadable_population_does_not_raise() -> None:
    """The worker's other cycles must not die because a billing repair did."""
    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=RuntimeError("pool gone"))
    import asyncio

    assert asyncio.run(run_subscription_reconcile_cycle(pool, _settings())) == 0


def test_one_companys_failure_does_not_stop_the_sweep() -> None:
    """A fleet-wide sweep that aborts on the first bad row repairs nobody
    after it, and the rows are ordered stalest-first — so the companies most
    likely to need it are the ones behind the failure."""
    applied: list[Any] = []

    async def fake_apply(settings: Any, etype: str, obj: dict[str, Any], created: Any = None):
        applied.append(obj["id"])

    client = MagicMock()
    client.list_subscriptions = AsyncMock(
        side_effect=[RuntimeError("stripe down"), [_sub(id="sub_second")]]
    )
    import asyncio

    with (
        patch("helixis.billing.subscription_reconcile.StripeClient", return_value=client),
        patch("helixis.api.routes.billing._apply_event", new=fake_apply),
        patch("helixis.api.routes.billing._plan_for_price", new=_plan),
    ):
        repaired = asyncio.run(
            run_subscription_reconcile_cycle(
                _pool([_row(), _row(stripe_customer_id="cus_two")]), _settings()
            )
        )

    assert repaired == 1
    assert applied == ["sub_second"]


# ── which subscription decides ───────────────────────────────────────


def test_a_serving_subscription_beats_a_cancelled_one() -> None:
    """A re-subscribe leaves the customer holding both. Letting Stripe's list
    order decide somebody's plan is the kind of coin flip that reads as a
    billing bug months later."""
    chosen = _current_subscription(
        [_sub(id="sub_old", status="canceled"), _sub(id="sub_new", status="active")]
    )
    assert chosen is not None and chosen["id"] == "sub_new"


def test_past_due_counts_as_serving() -> None:
    """⚠ ``past_due`` is the dunning window, which ``gate.py`` keeps serving
    through on purpose. Excluding it would let a stale CANCELLED subscription
    win over the live delinquent one and cut off a customer whose card is
    merely late."""
    chosen = _current_subscription(
        [_sub(id="sub_dead", status="canceled"), _sub(id="sub_late", status="past_due")]
    )
    assert chosen is not None and chosen["id"] == "sub_late"


def test_an_unrecognised_price_is_not_drift() -> None:
    """``_apply_event`` COALESCEs a ``None`` plan into "leave it alone", so
    reporting drift on a price we cannot map would re-apply forever, every
    sweep, for a subscription nothing can change."""
    assert (
        _drifted(
            _sub(items={"data": [{"price": {"id": "price_unknown"}}]}),
            ("sub_live", "active", "starter"),
            settings=_settings(),
            plan_for_price=_plan,
        )
        is False
    )


# ── wiring ───────────────────────────────────────────────────────────


def _worker_calls() -> set[str]:
    tree = ast.parse(WORKER.read_text(encoding="utf-8"))
    return {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }


def test_the_worker_actually_runs_the_cycle() -> None:
    """⚠ This repo has shipped a complete, unit-tested, unreachable module
    three times — ``comms_triage``, ``notes/policy``, and the reconciliation
    call gotcha 62 caught deleted from a cycle with every unit test green.
    ``seats_cycle``'s own docstring says the call site is the deliverable."""
    calls = _worker_calls()
    assert "run_subscription_reconcile_cycle" in calls
    assert "run_seat_reconcile_cycle" in calls, (
        "the seat sweep is gone — these are two populations and this one does not replace it"
    )


def test_the_repair_goes_through_the_webhooks_own_handler() -> None:
    """⚠ A local copy of the eighty lines that decide what a subscription
    object means for our row would drift, and the direction it drifts in is
    somebody's plan and somebody's allowance. Pinned as an AST import because
    a function-local import is easy to quietly replace with a local
    reimplementation, and a behavioural test cannot tell the two apart."""
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "helixis.api.routes.billing":
            imported |= {a.name for a in node.names}

    assert {"_apply_event", "_plan_for_price"} <= imported, (
        "the reconciler stopped reusing the webhook's handler/mapper — a second "
        "answer to 'what does this subscription mean' is what this avoids"
    )


def test_the_population_is_customers_not_subscriptions() -> None:
    """The whole point, and the one thing a behavioural test with a fake pool
    cannot see: the fake returns whatever rows it is handed regardless of the
    SQL. Keying on ``stripe_subscription_id`` here would re-create gotcha 62 —
    excluding precisely the companies whose subscription id is what went
    missing."""
    from helixis.billing.subscription_reconcile import _POPULATION_SQL

    sql = " ".join(_POPULATION_SQL.split()).lower()
    where = sql.split("where", 1)[1].split("order by", 1)[0]
    assert "stripe_customer_id" in where
    assert "stripe_subscription_id" not in where, (
        "the population now filters on the column a lost webhook is what makes "
        "NULL — the companies needing repair are the ones excluded"
    )
    assert "nulls first" in sql, (
        "a fleet larger than the cap must be swept round-robin, with the rows "
        "that have never seen a Stripe event at the front"
    )


@pytest.mark.parametrize("n", [MAX_COMPANIES_PER_SWEEP])
def test_the_sweep_is_bounded(n: int) -> None:
    """One Stripe call per company; a fleet-wide sweep on a bad morning has to
    cost a bounded number of them."""
    assert 0 < n <= 200


# ── the WIRE, not the fake ───────────────────────────────────────────


def test_the_list_call_asks_stripe_for_cancelled_subscriptions_too() -> None:
    """⚠ Every test above fakes ``list_subscriptions``, so none of them can see
    what URL it builds — producer and consumer each correct, the request
    between them wrong. That is gotcha 28's both-sides rule with a transport as
    the second side, and it is exactly how a mutation to ``status=active``
    survived the first battery.

    ``status=all`` is load-bearing: a lost ``subscription.deleted`` leaves
    somebody on a paid plan they stopped paying for, and asking only for active
    ones makes that half of the repair structurally impossible — the reconciler
    would see nothing and change nothing, forever.
    """
    import asyncio

    import httpx

    from helixis.billing.stripe_client import StripeClient

    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json={"data": [{"id": "sub_gone", "status": "canceled"}]})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def fake_client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=transport, **kwargs)

    with patch("helixis.billing.stripe_client.httpx.AsyncClient", new=fake_client):
        subs = asyncio.run(StripeClient("sk_test_x").list_subscriptions(CUSTOMER))

    assert len(seen) == 1
    params = dict(seen[0].params)
    assert params.get("customer") == CUSTOMER
    assert params.get("status") == "all", (
        "the reconciler can no longer see a cancellation — a customer who quit "
        "keeps their paid plan and nothing will ever notice"
    )
    assert seen[0].path.endswith("/subscriptions")

    assert [s["id"] for s in subs] == ["sub_gone"], (
        "a cancelled subscription must come BACK, not just be asked for"
    )


def test_a_failed_stripe_call_reads_as_no_evidence() -> None:
    """``_get`` logs and returns None on a non-2xx, so the list collapses to
    ``[]`` — the same value as "this customer has none". That collapse is
    deliberate and is why the caller treats empty as *no evidence*; pinned here
    so a future edit cannot start distinguishing them and act on the
    difference."""
    import asyncio

    import httpx

    from helixis.billing.stripe_client import StripeClient

    transport = httpx.MockTransport(lambda _r: httpx.Response(500, text="stripe is down"))
    real_client = httpx.AsyncClient

    def fake_client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=transport, **kwargs)

    with patch("helixis.billing.stripe_client.httpx.AsyncClient", new=fake_client):
        assert asyncio.run(StripeClient("sk_test_x").list_subscriptions(CUSTOMER)) == []
