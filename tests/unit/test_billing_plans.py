"""Every purchasable plan is buyable, priced above cost, and named nowhere else.

Three properties, and each one exists because its failure is SILENT.

1. **A plan without a Stripe price id is unbuyable.** `/billing/checkout`
   refuses with a 503 rather than sending Stripe a blank price, which is the
   right runtime behaviour and a terrible way to find out — the customer picks
   a plan, clicks buy, and gets "that plan is not available yet". Reading BOTH
   sides (gotcha 28) turns that into a failing test at the moment the plan is
   added.

2. **A plan whose cap costs more than it charges loses money on every customer
   who uses what they paid for.** `plans.py` records the margin in prose, and
   prose cannot fail (gotcha 48). This re-derives it from
   `MEASURED_MEAN_TURN_USD` so a cap raised without re-pricing fails here.

⚠ **NOT pinned here, deliberately: the statement ORDER inside 0090.** A
mutation reversing its two updates is a real and severe defect — the second
would catch the rows the first just renamed, putting today's $199 customers on
the $500 allowance — and it survives this file. A test asserting the order of
an applied migration guards an artifact that can never legitimately change, so
it would read as coverage while defending nothing. What actually guarded it was
the count-gate (predicted 1 row, matched 1) and the by-effect verification
after applying (gotcha 59). A FUTURE colliding rename needs its own count-gate,
not a test pointed at this file.

3. **The checkout route must not NAME a plan.** It used to carry
   `price_starter if key == "starter" else price_pro`, so a third plan would
   have fallen to the `else` and silently charged Pro's price — gotcha 25's
   fan-out shape pointed at somebody's card. An AST walk is the only thing
   that survives `ruff format` reflowing the expression (gotcha 40).
"""

from __future__ import annotations

import ast
import pathlib
import re

from helixis.billing.plans import (
    ALL_FEATURES,
    MEASURED_MEAN_TURN_USD,
    PAID_PLANS,
    PLANS,
    plan_for,
)
from helixis.settings import BillingSettings

_ROUTE = pathlib.Path("src/helixis/api/routes/billing.py")
_CLIENT = pathlib.Path("frontend/src/services/billing.ts")

# Per-customer monthly cost that does NOT amortise under white-label: Stripe's
# 2.9% + $0.30 on the charge, the A2P campaign's $1.50/mo, and a number at
# ~$1.15/mo. Deliberately a FLOOR — anything omitted makes the test more
# permissive, never less, so a plan passing here is not passing on a
# flattering estimate.
_FIXED_COST_FLOOR_USD = 1.50 + 1.15


def _stripe_fee(price_usd: int) -> float:
    return price_usd * 0.029 + 0.30


def test_every_purchasable_plan_has_a_price_id_field() -> None:
    """The map and the ladder name the same plans, in both directions.

    A plan in `PAID_PLANS` with no entry here is unbuyable; an entry here for
    a plan nobody sells is a dead setting that reads as coverage.
    """
    price_ids = BillingSettings().price_ids
    assert set(price_ids) == set(PAID_PLANS), (
        f"BillingSettings.price_ids covers {sorted(price_ids)} but PAID_PLANS is "
        f"{sorted(PAID_PLANS)} — a plan with no price id 503s at checkout, and a "
        "price id with no plan is a setting nothing reads."
    )


def test_every_purchasable_plan_is_profitable_at_full_use() -> None:
    """Priced against the measurement, not against the prose beside it.

    The bar is 100% UTILISATION: a plan that only earns because most people do
    not finish their allowance is a utilisation bet, and `plans.py`'s own Pro
    comment records the few hours it shipped that way.
    """
    for key in PAID_PLANS:
        plan = PLANS[key]
        # Flat plans pool their allowance; per-seat plans earn per seat AND
        # spend per seat, so one seat is the correct unit for both.
        model_cost = plan.turns_included * MEASURED_MEAN_TURN_USD
        overhead = _stripe_fee(plan.price_usd) + _FIXED_COST_FLOOR_USD
        margin = plan.price_usd - model_cost - overhead
        assert margin > 0, (
            f"{plan.key} costs ${model_cost + overhead:.2f} at full use against "
            f"${plan.price_usd} of revenue — a customer who uses everything they "
            f"paid for loses ${-margin:.2f}. Re-derive the cap against "
            f"MEASURED_MEAN_TURN_USD (${MEASURED_MEAN_TURN_USD}) before shipping."
        )


def test_the_cheapest_cap_does_not_read_as_a_downgrade_from_the_trial() -> None:
    """The cheapest paid plan must not offer LESS than the free trial.

    Not a margin property — a pricing-page one. The trial's turns are once and
    a plan's recur, so a smaller paid number is defensible on the arithmetic
    and still reads as "I paid and got less" at the moment of the decision.
    `plans.py` names 100 turns as the higher-margin alternative; this test is
    what makes choosing it a deliberate act rather than a quiet one.
    """
    cheapest = min((PLANS[k] for k in PAID_PLANS), key=lambda p: p.price_usd)
    trial = PLANS["trial"]
    assert cheapest.turns_included >= trial.turns_included, (
        f"{cheapest.key} includes {cheapest.turns_included} turns against the "
        f"trial's {trial.turns_included}. That is a real option — it earns more "
        "margin — but it is a product decision, so change this test in the same "
        "commit and say why."
    )


def test_the_stored_plan_keys_are_frozen_without_a_migration() -> None:
    """A key rename does not raise — it silently downgrades customers.

    `helixis_billing.plan` stores the KEY, and `plan_for` falls back to TRIAL
    on anything it does not recognise — deliberately, so an unknown row cannot
    buy the largest ceiling we sell. The cost of that correct decision is that
    renaming a key is invisible: every customer on the old name quietly drops
    to a 14-day trial allowance at their next turn, and the only symptom is
    somebody being told they are out of questions.

    The ladder DID move on 2026-09-04 (Starter $50 / Pro $199 / Scale $500,
    every label shifted down a rung), and that is exactly why `0090` exists —
    a key rename is a data migration, never an edit to this dict alone. ⚠ Its
    two updates run HIGHEST RUNG FIRST because the old and new names collide;
    reversed, today's $199 customers land on the $500 allowance.
    """
    assert set(PLANS) == {"trial", "starter", "pro", "scale"}, (
        "A plan key changed. Labels are display-only and safe to edit; keys are "
        "stored in helixis_billing.plan and resolve through plan_for, which "
        "falls back to TRIAL on an unknown key — so an edit here downgrades "
        "every customer on that plan with no error. Ship a migration that "
        "rewrites the stored rows (see 0090), or change the label instead."
    )


def test_the_database_accepts_every_plan_key_python_can_write() -> None:
    """The CHECK constraint is a SECOND source of truth for the plan keys.

    ⚠ This is the money bug. `helixis_billing_plan_known` allowed exactly
    ('trial','starter','pro') and nothing connected it to `PLANS`. A plan added
    in Python passes every unit test, renders in the panel, and reaches Stripe
    fine — then the webhook writes `plan = coalesce($3, plan)` with the new
    key, the constraint REJECTS it, and the whole
    `customer.subscription.created` handler fails. **The card is charged and
    the plan never activates**, surfacing as a webhook error rather than as
    anything that looks like pricing.

    That is exactly what `solo` would have done had it shipped, hours before
    this test existed. Gotcha 28's rule — a string crossing a producer/consumer
    boundary needs a test that reads BOTH sides — with Postgres as the
    consumer.

    Reads the whole migration CORPUS, not just 0090: an applied migration
    cannot be edited, so the newest definition always wins and a later file is
    where the next widening will land (gotcha 82's lesson).
    """
    pattern = re.compile(
        r"helixis_billing_plan_known\s*\n?\s*check\s*\((.*?)\)\s*;",
        re.IGNORECASE | re.DOTALL,
    )
    newest: str | None = None
    for path in sorted(pathlib.Path("supabase/migrations").glob("*.sql")):
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            newest = match.group(1)

    assert newest is not None, (
        "No CHECK constraint on helixis_billing.plan found anywhere in the "
        "migration corpus. Either it was renamed — re-point this scan — or the "
        "database no longer bounds the column, which lets a typo'd plan key "
        "persist as a permanent unknown that plan_for reads as trial."
    )

    allowed = set(re.findall(r"'([a-z_]+)'", newest))
    missing = set(PLANS) - allowed
    assert not missing, (
        f"PLANS defines {sorted(missing)} but the newest helixis_billing.plan "
        f"constraint only allows {sorted(allowed)}. The webhook writes the plan "
        "KEY, so a purchase of that plan charges the customer and then fails on "
        "the constraint — add a migration widening the check in the same change."
    )


def test_no_plan_can_be_multiplied_into_dominating_a_dearer_one() -> None:
    """A per-seat plan bought N times must not beat a dearer plan on BOTH axes.

    ⚠ This is the hole a mutation found and review did not. Flipping Solo to
    `per_seat=True` keeps its per-seat margin intact — so the profitability
    test above passes — while three seats become **$150 for 450 turns**
    against Starter's **$199 for 400**. Cheaper AND more allowance: Starter is
    strictly dominated and nobody would ever buy it.

    A company holds exactly one plan, so the only arbitrage is SEATS. That is
    why the cheapest tier is flat: a flat plan cannot be multiplied, so the
    cap does the segmenting on its own. Margin and ladder are separate
    properties and a plan can satisfy one while breaking the other.
    """
    for seller in (PLANS[k] for k in PAID_PLANS):
        if not seller.per_seat:
            continue  # a flat plan cannot be scaled, so it cannot dominate
        for seats in range(1, 11):
            price = seller.price_usd * seats
            turns = seller.turn_allowance(seats)
            for rival in (PLANS[k] for k in PAID_PLANS):
                if rival.key == seller.key:
                    continue
                dominates = price < rival.price_usd and turns >= rival.turn_allowance(1)
                assert not dominates, (
                    f"{seats}x {seller.key} is ${price} for {turns} turns, which "
                    f"beats {rival.key} (${rival.price_usd} for "
                    f"{rival.turn_allowance(1)}) on price AND allowance — "
                    f"{rival.key} becomes unsellable. Keep the cheapest tier flat, "
                    "or re-cut the caps so each rung buys something."
                )


def test_an_unknown_plan_key_cannot_be_purchased() -> None:
    """`plan_for` falls back to TRIAL, and trial is not in `PAID_PLANS`.

    So the checkout guard rejects junk without needing its own validation —
    but only while trial stays unpurchasable, which is what this pins.
    """
    assert plan_for("enterprise-unlimited").key == "trial"
    assert "trial" not in PAID_PLANS


def test_checkout_resolves_the_price_id_by_lookup_and_never_by_plan_name() -> None:
    """AST, not a substring scan.

    The route's comments now QUOTE the old `== "starter"` branch in order to
    explain why it is gone, so a text search fires on the sentence documenting
    the guard — gotcha 40, and gotcha 59's SQL variant of it. A comment is
    never a `Compare` node, so walking the tree needs no comment handling.
    """
    tree = ast.parse(_ROUTE.read_text(encoding="utf-8"))
    checkout = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and "checkout" in n.name
    )

    plan_literals = {k for k in PAID_PLANS}
    for node in ast.walk(checkout):
        if isinstance(node, ast.Compare):
            for side in [node.left, *node.comparators]:
                if isinstance(side, ast.Constant) and side.value in plan_literals:
                    raise AssertionError(
                        f"checkout compares against the plan literal {side.value!r} "
                        f"(line {side.lineno}). A per-plan branch charges the NEXT "
                        "plan whatever the else-arm names — resolve through "
                        "settings.billing.price_ids instead."
                    )

    # And the positive half: it must actually reach the map. Asserting only the
    # absence would pass against a checkout that resolved no price at all.
    reaches_map = any(
        isinstance(n, ast.Attribute) and n.attr == "price_ids" for n in ast.walk(checkout)
    )
    assert reaches_map, "checkout no longer reads settings.billing.price_ids"


# ── the plan keys cross a language boundary (gotcha 28) ──────────────


def _ts_union(name: str) -> set[str]:
    """The string members of a `export type <name> = 'a' | 'b';` declaration.

    ⚠ Deliberately tolerant of what comes AFTER the quoted members —
    `PlanKey` is written as `'trial' | PaidPlanKey`, and a scan that demanded
    every alternative be a literal would report it as empty and pass
    vacuously. It returns the literals it finds and the caller says what they
    should be.
    """
    source = _CLIENT.read_text(encoding="utf-8")
    match = re.search(rf"export type {name}\s*=\s*([^;]+);", source)
    assert match, (
        f"`export type {name}` is gone from {_CLIENT}. Either it was renamed — "
        "re-point this scan — or the client stopped naming the plan keys at "
        "all, which means nothing holds it in step with PLANS."
    )
    return set(re.findall(r"'([a-z_]+)'", match.group(1)))


def test_the_client_knows_every_plan_the_server_sells() -> None:
    """TypeScript cannot import `plans.py`, so the key set exists twice.

    ⚠ Drift here does NOT fail loudly, which is why it needs a test. A union
    narrower than the ladder makes `status.plan === plan.key` quietly
    impossible for the missing rung and lets `startCheckout` be typed against
    a key the server would reject — which is exactly what happened when
    `PlanOption.key` sat at `'starter' | 'pro'` while the API had begun
    serving `scale`. Nothing errored; the card simply never rendered right.
    """
    assert _ts_union("PaidPlanKey") == set(PAID_PLANS), (
        "frontend/src/services/billing.ts's PaidPlanKey and PAID_PLANS "
        "disagree. Add the key on BOTH sides — and check `startCheckout`, "
        "which is typed against it."
    )


def test_the_client_knows_the_trial_too() -> None:
    """`BillingStatus.plan` can be any STORED key, not just a purchasable
    one — a trial company reports 'trial' and must still typecheck."""
    assert _ts_union("PlanKey") == {"trial"}, (
        "PlanKey should read `'trial' | PaidPlanKey` — spelling the paid keys "
        "out again is a third copy of the same list."
    )


def test_the_client_knows_every_enforced_feature() -> None:
    assert _ts_union("PlanFeature") == set(ALL_FEATURES), (
        "PlanFeature and ALL_FEATURES disagree. The client uses this union to "
        "decide what a card claims and what the Leasing gate opens; the server "
        "uses the same keys to refuse the routes. They cannot be allowed to "
        "drift, or a card promises a section the API declines."
    )


_PANEL = pathlib.Path("frontend/src/components/BillingPanel.tsx")


def _render_order_is_derived(source: str) -> bool:
    """Does the plan card's feature list come FROM the label map?

    ⚠ Comments are stripped first. The code this guards now quotes the broken
    spelling (`PlanFeature[]`) in order to explain it, so a scan over raw text
    fires on the CORRECT file — the trap this repo keeps re-making (gotchas
    40/59/91/114/124).
    """
    stripped = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    stripped = re.sub(r"(?<!:)//[^\n]*", "", stripped)
    match = re.search(r"\bFEATURE_ORDER\s*(?::[^=]+)?=\s*([^;]+);", stripped)
    assert match, (
        "canary: no FEATURE_ORDER assignment in BillingPanel.tsx — it was "
        "renamed or the cards stopped listing capabilities, so this guard is "
        "measuring nothing. Re-point it (gotchas 49/100/101)."
    )
    return "FEATURE_LABEL" in match.group(1)


def test_the_plan_cards_list_every_capability_the_server_enforces() -> None:
    """The card's feature rows are derived from the label map, not re-typed.

    ⚠ MEASURED, because the two lists sit on adjacent lines and only one of
    them is enforced. Adding a member to `PlanFeature` makes tsc reject
    `FEATURE_LABEL` (`Record<PlanFeature, string>` is exhaustive — probed:
    TS2741). A separate `const FEATURE_ORDER: PlanFeature[] = ['crm']` is
    satisfied by ANY subset, so tsc stayed silent and the new capability
    rendered on no plan card at all — invisible in the one place a customer
    compares what they are buying.

    Latent rather than live: `ALL_FEATURES` is a single key today, so the
    hand-typed list happened to be complete (gotcha 135's correct-by-
    coincidence shape). A behavioural test cannot tell the two spellings apart
    for that reason, which is why this reads the source (gotcha 58).
    """
    assert _render_order_is_derived(_PANEL.read_text(encoding="utf-8")), (
        "BillingPanel's FEATURE_ORDER no longer comes from FEATURE_LABEL. A "
        "hand-typed list is satisfied by a subset, so the next capability the "
        "server enforces would be absent from every plan card with nothing "
        "failing. Derive it, or make the render exhaustive some other way."
    )
    # The corpus has one feature, so it cannot supply a failing case — hand it
    # one directly (gotchas 38/51/135: a guard that only ever sees the good
    # input passes just as well when it has stopped discriminating).
    assert not _render_order_is_derived(
        "const FEATURE_LABEL: Record<PlanFeature, string> = { crm: 'x' };\n"
        "const FEATURE_ORDER: PlanFeature[] = ['crm'];\n"
    ), "the guard accepts a hand-typed order — it would not have caught the defect it names"


# ── features are enforced capabilities, not bullets ──────────────────


def test_no_plan_carries_a_feature_nothing_enforces() -> None:
    """A key outside `ALL_FEATURES` is a promise with no refusal behind it."""
    for key, plan in PLANS.items():
        unknown = plan.features - set(ALL_FEATURES)
        assert not unknown, (
            f"{key} claims {sorted(unknown)}, which no code enforces. Either "
            "add it to ALL_FEATURES and write the refusal, or move it to "
            "`highlights` where it is honestly just copy."
        )


def test_marketing_copy_never_restates_a_price_or_an_allowance() -> None:
    """The hand-copied price table, wearing a different hat.

    ⚠ `test_plan_figures_have_one_source` exists because these numbers move —
    the Pro ceiling shipped at 2,500 and was cut to 1,200 hours later. A
    bullet reading "1,200 questions a month" would have survived that edit
    silently, on the card, next to the correct figure rendered from
    `turns_included`. So the copy carries no digits at all: the client renders
    every number from the fields above it.
    """
    offenders: list[str] = []
    for key, plan in PLANS.items():
        for text in (plan.tagline, *plan.highlights):
            if any(ch.isdigit() for ch in text) or "$" in text:
                offenders.append(f"{key}: {text!r}")
    assert not offenders, (
        "plan copy is quoting figures instead of letting the card render "
        f"them from price_usd / turns_included: {offenders}"
    )


def test_every_purchasable_plan_says_who_it_is_for() -> None:
    """A card with no tagline is three prices and no reason to pick one —
    which is what the panel looked like before 2026-09-04."""
    for key in PAID_PLANS:
        plan = PLANS[key]
        assert plan.tagline.strip(), f"{key} has no tagline"
        assert plan.highlights, f"{key} lists nothing it includes"


def test_every_purchasable_plan_round_trips_through_the_webhook_map() -> None:
    """⚠ THE REVERSE DIRECTION, which the test above is structurally blind to.

    `test_every_purchasable_plan_has_a_price_id_field` compares `price_ids`
    against `PAID_PLANS` — both sides of the FORWARD map, the one checkout
    reads. The webhook reads the map BACKWARDS (`_plan_for_price`: which plan
    is this Stripe price?), and nothing compared that against anything.

    It was wrong. `_plan_for_price` was an if-chain covering starter and pro
    with no `scale` branch, so a Scale subscription resolved to None and the
    handler's `plan = coalesce($3, plan)` kept the row's PREVIOUS plan —
    `trial`. Stripe charges $500/month while the gate serves 150 requests and
    no Leasing. `BillingSettings.price_ids` had already recorded this same
    defect being fixed on the checkout side; the fix reached one layer of two
    (gotcha 25).

    Driving the real function with real settings is the point — asserting the
    if-chain's shape, or re-deriving the expected answer from `price_ids`,
    would only prove the function agrees with itself (gotcha 45).
    """
    from helixis.api.routes.billing import _plan_for_price
    from helixis.settings import Settings

    # Distinct, obviously-fake ids: reusing one across two plans would let a
    # map that returns the FIRST match pass while mis-attributing the second.
    settings = Settings(
        env="test",
        billing={f"price_{key}": f"price_fake_{key}" for key in PAID_PLANS},
    )
    for key in PAID_PLANS:
        assert _plan_for_price(settings, f"price_fake_{key}") == key, (
            f"the webhook cannot resolve {key}'s price id back to {key}. A "
            "subscription on that plan keeps whatever plan the row had before "
            "— the customer is charged for one plan and served another."
        )

    # An id we sell nothing at must stay unresolved rather than falling into
    # the nearest branch, which is how the forward map mis-charged before it
    # was rewritten.
    assert _plan_for_price(settings, "price_not_ours") is None
    assert _plan_for_price(settings, None) is None


def test_an_unconfigured_price_does_not_swallow_a_real_one() -> None:
    """⚠ Empty is not a match. Every `price_*` defaults to `""`, so on a
    deployment with only some plans wired, a comparison that did not skip
    blanks would resolve `price_id=""`… and worse, any FUTURE plan added with
    no id configured would collide with every other unconfigured plan and
    return whichever the dict happened to yield first.
    """
    from helixis.api.routes.billing import _plan_for_price
    from helixis.settings import Settings

    partial = Settings(env="test", billing={"price_pro": "price_real_pro"})
    assert _plan_for_price(partial, "price_real_pro") == "pro"
    assert _plan_for_price(partial, "") is None
