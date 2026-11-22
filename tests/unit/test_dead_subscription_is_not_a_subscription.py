"""A stored subscription id is not a subscription that can still bill.

WHAT WAS WRONG
--------------
Four places asked ``bool(stripe_subscription_id)`` when the question they
needed answered was "could this subscription still charge somebody". For seven
of Stripe's eight statuses those agree. For the customer whose FIRST PAYMENT
EXPIRED they do not, and that customer is the one trying to start paying us:

* ``create_checkout`` refused with *"You already have a subscription — change
  your plan instead of starting a new one."*  They do not have one. The chat
  gate, in the same breath, was telling them *"That subscription was never
  completed — pick a plan again in Settings → Billing"*, which is the door
  checkout had just shut.
* ``BillingPanel`` decided between Checkout and switch on ``plan === 'trial'``.
  An ``incomplete_expired`` company still carries the plan it picked, so the
  panel headed the section "Change plan" and every button under it pushed a
  re-price at a subscription that never charged — surfacing as *"Could not
  change the plan with Stripe"*, blaming a vendor that is working fine.
* ``change_plan`` did the pushing. Its own comment already said a *"trial or
  LAPSED company"* belongs in checkout; the condition only ever caught the
  null id, which covers ``canceled`` (the delete handler nulls it) and not
  ``incomplete_expired`` (which keeps it).
* ``seat_change_quote`` quoted the invite dialog a proration onto a
  subscription that cannot be prorated.

So both exits were shut and the two error messages contradicted each other.
``incomplete_expired`` is a real persisting status here, not a theoretical one:
migration 0097 exists because the CHECK constraint could not store it, and
``gate.py`` has carried a dedicated branch for it since.

WHY THE OBVIOUS SHORTCUT IS WRONG
---------------------------------
``status not in gate._SERVING_STATUSES`` is the one-liner that looks right and
re-opens the double-subscription bug: ``incomplete`` is not *serving* — the
gate correctly pauses the product mid-3-D-Secure — but it is very much still
*billing*. ``test_serving_and_billing_are_different_questions`` pins that the
two sets genuinely disagree, so the shortcut is refuted by a test rather than
by a comment nobody reads.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from helixis.billing.gate import _SERVING_STATUSES, BillingState
from helixis.billing.plans import plan_for
from helixis.billing.stripe_status import (
    STRIPE_SUBSCRIPTION_STATUSES,
    TERMINAL_STATUSES,
    subscription_can_bill,
)

ROUTE = pathlib.Path(__file__).resolve().parents[2] / "src/helixis/api/routes/billing.py"
FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend/src"


# ── the predicate, both directions ───────────────────────────────────────


def test_every_terminal_status_is_one_stripe_actually_sends() -> None:
    """A typo here never matches, so the predicate would silently allow
    everything while reading as a careful allowlist."""
    assert TERMINAL_STATUSES
    assert TERMINAL_STATUSES <= STRIPE_SUBSCRIPTION_STATUSES


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
def test_a_terminal_subscription_cannot_bill(status: str) -> None:
    assert subscription_can_bill(status) is False


@pytest.mark.parametrize("status", sorted(STRIPE_SUBSCRIPTION_STATUSES - TERMINAL_STATUSES))
def test_every_other_stripe_status_can_still_bill(status: str) -> None:
    """⚠ The other direction, and it is the one that costs money.

    A predicate that answered False for everything would satisfy the test
    above perfectly and hand a paying customer a second subscription
    (gotchas 38/51: test the thing it must catch AND the thing it must spare).
    """
    assert subscription_can_bill(status) is True


@pytest.mark.parametrize("raw", [None, "", "   ", "something_stripe_invents_in_2027", 7, object()])
def test_an_unrecognised_status_is_assumed_to_still_bill(raw: object) -> None:
    """Unknown keeps today's behaviour exactly — refuse a second checkout.

    ``storable_status`` writes NULL for a status this file has not heard of and
    the column is COALESCEd, so a stale or absent value is expected rather than
    surprising. Both error directions are real, and this is the one where being
    wrong costs a support email instead of the customer's money.
    """
    assert subscription_can_bill(raw) is True


def test_serving_and_billing_are_different_questions() -> None:
    """The refutation of ``status not in _SERVING_STATUSES``.

    If these ever became the same set the shortcut would be harmless and this
    test would be noise — so it names the statuses rather than asserting
    inequality, and fails loudly with them if the vocabulary moves.
    """
    not_serving = STRIPE_SUBSCRIPTION_STATUSES - _SERVING_STATUSES
    assert not_serving != TERMINAL_STATUSES
    # Not serving, still billing: a second checkout for either is a double
    # subscription, which is what the 409 exists to prevent.
    assert {"incomplete", "unpaid", "paused"} <= not_serving - TERMINAL_STATUSES
    assert all(subscription_can_bill(s) for s in ("incomplete", "unpaid", "paused"))


# ── the state property the panel reads ───────────────────────────────────


def _state(*, status: str, has_subscription: bool) -> BillingState:
    return BillingState(
        plan=plan_for("pro"),
        status=status,
        seats=3,
        turns_used=0,
        turns_allowed=100,
        trial_ends_at=None,
        period_end=None,
        past_due=False,
        has_subscription=has_subscription,
    )


def test_a_live_subscription_can_be_changed() -> None:
    assert _state(status="active", has_subscription=True).can_change_plan is True


def test_a_dead_subscription_cannot_be_changed_even_though_its_id_is_stored() -> None:
    """The defect, stated as a property: id present, plan present, nothing to
    re-price."""
    assert _state(status="incomplete_expired", has_subscription=True).can_change_plan is False


def test_no_subscription_at_all_cannot_be_changed() -> None:
    assert _state(status="trialing", has_subscription=False).can_change_plan is False


def test_a_degraded_read_reports_nothing_to_change() -> None:
    """The dataclass default. A state built without the field sends the
    customer to Checkout, whose own 409 is the backstop — rather than offering
    a switch against an id we never read."""
    state = BillingState(
        plan=plan_for("pro"),
        status="active",
        seats=1,
        turns_used=0,
        turns_allowed=10,
        trial_ends_at=None,
        period_end=None,
        past_due=False,
    )
    assert state.can_change_plan is False


# ── the call sites, because a behavioural test cannot see one that ignores it ──


def _func(name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    tree = ast.parse(ROUTE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not in {ROUTE.name} — re-point this guard")


@pytest.mark.parametrize("func", ["create_checkout", "change_plan"])
def test_the_refusal_itself_asks_whether_the_subscription_can_bill(func: str) -> None:
    """⚠ NOT "the function calls the predicate somewhere".

    That is the check a dead ``_ = subscription_can_bill(row["status"])`` sitting
    beside an unchanged ``if row["stripe_subscription_id"]:`` satisfies
    perfectly, which is gotcha 94's finding and gotcha 128's, both inside the
    guard written for the defect. The CONDITION is what decides, so the
    condition is what gets read.

    ``create_portal`` is deliberately absent: a lapsed customer's invoices and
    payment history live in the portal, and ``gate.py`` sends both
    ``incomplete`` and ``unpaid`` there on purpose. "Nothing left to bill" and
    "nothing to show you" are different questions.
    """
    refusals = [
        ast.unparse(n.test)
        for n in ast.walk(_func(func))
        if isinstance(n, ast.If)
        and any(
            isinstance(inner, ast.Raise)
            and isinstance(inner.exc, ast.Call)
            and isinstance(inner.exc.func, ast.Name)
            and inner.exc.func.id == "HTTPException"
            for inner in ast.walk(n)
        )
    ]
    # ⚠ Anchored on "the branch that RAISES", not on a variable name. The first
    # cut required the string `stripe_subscription_id` in the condition and
    # failed against correct code, because `change_plan` binds the id to a local
    # first — a guard that fails on the right code is telling you the property
    # is wrong, not the code (gotchas 103/109/111/124/133).
    assert refusals, f"{func} has no HTTPException branch — re-point this guard"
    assert any("subscription_can_bill" in r for r in refusals), (
        f"{func} refuses on the stored id alone; a dead call elsewhere in the "
        "function satisfies a presence check but changes no behaviour"
    )


def test_the_seat_quote_passes_the_predicate_and_not_the_bare_id() -> None:
    """The one site that is an argument rather than a branch. A dead call
    satisfies a presence check here too, so the KEYWORD's value is read."""
    keywords = [
        kw
        for call in ast.walk(_func("seat_quote"))
        if isinstance(call, ast.Call)
        for kw in call.keywords
        if kw.arg == "has_subscription"
    ]
    assert len(keywords) == 1, "seat_quote should pass has_subscription exactly once"
    assert "subscription_can_bill" in ast.unparse(keywords[0].value)


def test_the_status_response_sends_the_servers_own_answer() -> None:
    """The panel must not re-derive it. Same rule as `block`, `features` and
    `notice`, and the same reason — this one decides whether a customer is
    charged twice."""
    src = ROUTE.read_text(encoding="utf-8")
    assert "can_change_plan=state.can_change_plan" in src


# ── both sides of the wire ───────────────────────────────────────────────


def test_the_client_carries_the_field_and_defaults_it_closed() -> None:
    src = (FRONTEND / "services/billing.ts").read_text(encoding="utf-8")
    assert "canChangePlan: boolean;" in src
    # Absent on the wire (an older backend) must read as "nothing to switch",
    # which offers Checkout — whose 409 is a sentence the customer can act on.
    assert "canChangePlan: w.can_change_plan ?? false," in src


def test_the_panel_routes_the_button_on_the_servers_answer_not_on_the_plan() -> None:
    """⚠ `plan === 'trial'` is the defect, and it is still legitimately used in
    this file for the trial countdown and the per-person footer — those are
    about the PLAN. Only the three ACTION sites had to move, so this pins the
    decision itself rather than banning the word."""
    src = (FRONTEND / "components/BillingPanel.tsx").read_text(encoding="utf-8")
    assert "const canSwitchPlan = status.canChangePlan;" in src
    assert "if (!canSwitchPlan) {" in src
    # The heading has to agree with what the button does, or the label promises
    # one action and the click performs the other.
    assert "{canSwitchPlan ? 'Change plan' : 'Choose a plan'}" in src
    assert "{onTrial ? 'Choose a plan' : 'Change plan'}" not in src
