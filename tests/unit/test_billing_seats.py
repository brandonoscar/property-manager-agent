"""Seats derive from membership, and Stripe is told when membership changes.

⚠ THE DEFECT THIS PINS. ``helixis_billing.seats`` had exactly one writer — the
Stripe subscription webhook — copying a quantity that originated as a number
the BROWSER sent at checkout and was never revisited. Inviting or removing
somebody changed nothing, so a five-person Pro team stayed on one seat: billed
for one, and (because Pro's allowance is per seat) allowanced for one.

The tests are grouped by the way each half fails, because they fail
differently: arithmetic silently bills a wrong number, and wiring silently
bills nothing at all.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.billing.plans import PLANS, Plan
from helixis.billing.seats import billable_seats, count_members, sync_company_seats, target_seats

_ROUTES = pathlib.Path(__file__).resolve().parents[2] / "src/helixis/api/routes"


class _FakePool:
    """Answers the two queries this module makes, by looking at the SQL.

    Keyed on the statement rather than call order so a reordering inside
    ``sync_company_seats`` does not silently hand back the wrong row — the
    failure mode a positional fake produces is a passing test measuring the
    wrong thing.
    """

    def __init__(self, *, members: int, plan: str | None, subscription: str | None) -> None:
        self._members = members
        self._plan = plan
        self._subscription = subscription

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if "count(*)" in sql:
            return {"n": self._members}
        if "select plan" in sql:
            return None if self._plan is None else {"plan": self._plan}
        if "stripe_subscription_id" in sql:
            return {"stripe_subscription_id": self._subscription}
        raise AssertionError(f"unexpected query: {sql}")


def _settings() -> Any:
    s = MagicMock()
    s.billing.stripe_secret_key = "sk_test_x"
    return s


# ── the arithmetic ───────────────────────────────────────────────────


def test_a_flat_plan_is_always_one_seat() -> None:
    """Starter and Scale are per-ACCOUNT and the pricing page says so —
    Scale's own highlight is "the allowance is pooled, not charged per
    person". Multiplying either by headcount makes that sentence false and
    overcharges every team on a flat plan."""
    for key in ("starter", "scale"):
        assert billable_seats(PLANS[key], 12) == 1, key


def test_a_per_seat_plan_tracks_headcount() -> None:
    assert billable_seats(PLANS["pro"], 5) == 5


def test_an_empty_company_still_bills_one_seat() -> None:
    """A zero-member count is a bug or a race, not a free account. Billing 0
    of a per-seat plan is a $0 invoice for a live subscription."""
    assert billable_seats(PLANS["pro"], 0) == 1


def test_the_ceiling_is_the_plans_own_not_one_global_number() -> None:
    """The trial's ceiling has to be tighter than a paid plan's, because trial
    seats multiply model spend against zero revenue."""
    assert billable_seats(PLANS["trial"], 999) == PLANS["trial"].max_seats
    assert billable_seats(PLANS["pro"], 999) == PLANS["pro"].max_seats
    assert PLANS["trial"].max_seats < PLANS["pro"].max_seats


@pytest.mark.parametrize("key", sorted(PLANS))
@pytest.mark.parametrize("members", [0, 1, 3, 5, 50, 999])
def test_what_we_bill_and_what_we_allow_never_disagree(key: str, members: int) -> None:
    """The one that matters, across every plan and every headcount.

    ``billable_seats`` decides the INVOICE and ``Plan.turn_allowance`` decides
    the ALLOWANCE, and they clamp independently. If they ever drift, a customer
    pays for seats they were not given (or is given seats nobody paid for) and
    both numbers still look individually reasonable — the kind of disagreement
    nobody notices until a support ticket.
    """
    plan = PLANS[key]
    billed = billable_seats(plan, members)
    expected = plan.turns_included * billed if plan.per_seat else plan.turns_included
    assert plan.turn_allowance(members) == expected


def test_the_trial_multiplies_spend_and_the_ceiling_bounds_it() -> None:
    """A guard on the DECISION, not just the code (gotcha 48's shape).

    The trial being per-seat is a deliberate cost: seats multiply model spend
    with no revenue behind them. If someone raises `max_seats` without
    re-deriving that cost, this fails and names the number.
    """
    trial = PLANS["trial"]
    assert trial.per_seat is True, "the trial is per-seat by founder call, 2026-09-04"
    worst_case_turns = trial.turn_allowance(999)
    assert worst_case_turns <= 750, (
        f"the trial can now hand out {worst_case_turns} free turns per company. "
        "At the measured mean that is real money with no revenue behind it — "
        "re-derive the cost before raising Plan.max_seats on the trial."
    )


# ── declining rather than guessing ───────────────────────────────────


@pytest.mark.asyncio
async def test_no_billing_row_declines_instead_of_assuming_one_seat() -> None:
    pool = _FakePool(members=4, plan=None, subscription="sub_1")
    assert await target_seats(pool, "c1") is None


@pytest.mark.asyncio
async def test_an_unknown_plan_declines_rather_than_falling_back_to_trial() -> None:
    """``plan_for`` folds an unknown key onto trial — right for a GATE, where
    least privilege is safe, and wrong here: trial is flat, so the fallback
    would resolve to quantity 1 and silently UNDER-bill a paying customer while
    the sync looked clean. The safe direction for an allowance and for an
    invoice are opposites."""
    pool = _FakePool(members=9, plan="enterprise_from_a_newer_deploy", subscription="sub_1")
    assert await target_seats(pool, "c1") is None


@pytest.mark.asyncio
async def test_a_company_with_no_subscription_is_not_an_error() -> None:
    """Every trial company takes this path on every invite. Checkout reads the
    live count when it happens, so the first bill is already right."""
    pool = _FakePool(members=3, plan="pro", subscription=None)
    assert await sync_company_seats(pool, _settings(), "c1", reason="invite") is None


@pytest.mark.asyncio
async def test_a_stripe_failure_never_raises_into_the_request(monkeypatch) -> None:
    """A team edit must not fail because a payment processor is slow."""
    client = MagicMock()
    client.update_subscription_quantity = AsyncMock(return_value=None)
    monkeypatch.setattr("helixis.billing.seats.StripeClient", lambda _key: client)
    pool = _FakePool(members=4, plan="pro", subscription="sub_1")
    assert await sync_company_seats(pool, _settings(), "c1", reason="invite") is None


@pytest.mark.asyncio
async def test_the_quantity_pushed_is_the_live_headcount(monkeypatch) -> None:
    client = MagicMock()
    client.update_subscription_quantity = AsyncMock(return_value={"id": "sub_1"})
    monkeypatch.setattr("helixis.billing.seats.StripeClient", lambda _key: client)
    pool = _FakePool(members=6, plan="pro", subscription="sub_1")

    await sync_company_seats(pool, _settings(), "c1", reason="invite")

    client.update_subscription_quantity.assert_awaited_once_with("sub_1", quantity=6)


@pytest.mark.asyncio
async def test_the_member_count_is_scoped_to_one_company() -> None:
    """gotcha 47. An unscoped count here bills somebody for the deployment."""
    seen: list[tuple[str, tuple[Any, ...]]] = []

    class _Recording(_FakePool):
        async def fetchrow(self, sql: str, *args: Any) -> Any:
            seen.append((sql, args))
            return await super().fetchrow(sql, *args)

    await count_members(_Recording(members=2, plan="pro", subscription="s"), "company-abc")
    sql, args = seen[0]
    assert "company_id = $1" in sql
    assert args == ("company-abc",)


# ── the wiring, which no behavioural test can see ────────────────────


def _calls_in(path: pathlib.Path, func: str) -> list[str]:
    """Names of the functions whose body contains a call to ``func``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        for c in ast.walk(node):
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == func:
                out.append(node.name)
                break
    return out


def test_both_membership_mutations_tell_stripe() -> None:
    """A behavioural test of the helper cannot see a caller that ignores it —
    gotcha 62, and the same shape as gotcha 69's four missed mutations, where
    the resolver was thoroughly tested and nothing pinned that the ROUTES used
    it. Removal matters most: nobody complains about being under-billed, so an
    unsynced removal can sit for months."""
    callers = set(_calls_in(_ROUTES / "company.py", "sync_company_seats"))
    assert "invite_member" in callers, "inviting somebody does not tell Stripe"
    # The route function is `remove_company_member`; `members_repo.remove_member`
    # is the repo call it wraps. Naming the wrong one is how this guard passes
    # against an unwired route, so it asserts the ROUTE.
    assert "remove_company_member" in callers, "removing somebody does not credit the seat back"


def test_checkout_derives_the_quantity_and_cannot_be_told_it() -> None:
    """The browser used to send `seats` and the bill was whatever it claimed.

    Removing the FIELD is the fix, not validating it — same move as the A2P
    form's server-filled values. This asserts both halves: the model no longer
    accepts it, and the route computes it.
    """
    src = (_ROUTES / "billing.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    checkout_in = next(
        n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "CheckoutIn"
    )
    fields = {
        t.target.id
        for t in checkout_in.body
        if isinstance(t, ast.AnnAssign) and isinstance(t.target, ast.Name)
    }
    assert "seats" not in fields, (
        "CheckoutIn accepts a client seat count again — the browser must not be "
        "able to set the quantity it is billed for."
    )
    derivers = _calls_in(_ROUTES / "billing.py", "billable_seats")
    assert derivers, "no route derives a seat count from membership any more"
    counters = _calls_in(_ROUTES / "billing.py", "count_members")
    assert set(derivers) & set(counters), (
        "billable_seats is called, but not in the same function that counts "
        "members — the quantity is being derived from something else."
    )


# ── the table the TypeScript client mirrors ─────────────────────────────


@pytest.mark.parametrize(
    ("per_seat", "max_seats", "members", "want"),
    [
        (False, 50, 0, 1),
        (False, 50, 9, 1),  # a flat plan is ONE seat however big the team is
        (True, 50, 0, 1),  # never zero — there is always at least one person
        (True, 50, 1, 1),
        (True, 50, 7, 7),
        (True, 5, 9, 5),  # clamped at the plan's own ceiling
        (True, 50, 999, 50),
    ],
)
def test_the_clamp_table_the_client_mirrors(
    per_seat: bool, max_seats: int, members: int, want: int
) -> None:
    """⚠ THE SAME TABLE IS DRIVEN IN TYPESCRIPT by
    ``frontend/src/components/InviteUsersDialog.test.tsx`` →
    ``describe('billableSeats mirrors billing/seats.py')``.

    The invite dialog quotes a per-seat price to an admin BEFORE they send, so
    it has to apply this clamp client-side — and TypeScript cannot import this
    module. That leaves a second copy of an arithmetic rule about money, which
    is the shape ``PaidPlanKey`` in ``billing.ts`` already records getting
    wrong.

    Neither side is the authority alone. Both are pinned against this one
    written table and each file names the other, so a change to either clamp
    fails a test rather than showing a customer one number and invoicing
    another. The VALUES the client multiplies by (``price_usd``, ``max_seats``,
    ``per_seat``) all still come from the server — only the arithmetic is
    duplicated, deliberately and visibly.

    ⚠ Uses a synthesised Plan rather than a shipped one on purpose: pinning
    this against `PLANS["pro"]` would make the table move whenever packaging
    does, and the thing under test is the FORMULA, not today's prices.
    """
    plan = Plan(
        "probe",
        "Probe",
        price_usd=1,
        per_seat=per_seat,
        turns_included=1,
        max_seats=max_seats,
    )
    assert billable_seats(plan, members) == want
