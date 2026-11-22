"""Changing a plan figure must be loud, and must name every other place it lives.

`plans.py` is the source of truth for what a customer is charged. The figures
in it are ALSO written out by hand in three other places, one of which is in a
different repository and cannot import it:

    frontend/src/components/BillingPanel.tsx   — FIXED 2026-09-02, now served
                                                  by GET /billing/status
    helixis-onboarding  src/Legal.tsx          — the billing TERMS

⚠ **CORRECTED AGAIN, 2026-09-04 (second pass).** Earlier today this header
said `Landing.tsx` "has never carried a price — measured, not assumed". That
was measured against `main`, and the figures live on the working BRANCH:
commit `684a88d feat(legal,landing): real billing terms and a price on the
page` put $50 / $199 / $500 on the public pricing cards, and a fresh
`grep -c` finds three. So the correction was itself wrong, in the direction
that matters — a checklist telling the next reader a file is clean sends them
to edit nothing and report done, which is worse than omitting the file. Both
onboarding files are load-bearing now; neither is a note.

The reason this file exists at all is that the numbers demonstrably move: the
Pro ceiling shipped at **2,500 on 2026-09-02** — above break-even — and was
cut to 1,200 hours later. `plans.py`'s own docstring invites exactly that
("Re-derive these before changing a price"), so the drift is invited on one
side and silent on the other three.

⚠ WHAT THIS GUARD DOES AND DOES NOT DO. It cannot see the onboarding repo —
that is a separate checkout, absent in CI, and a test that skipped when it was
missing would report a pass for a check that never ran (the LED-013
skip-floor). So it does NOT verify those two files agree. What it does is make
a price change FAIL, with the other three locations named in the failure text,
so the person changing it cannot not-know. That is a checklist with teeth
rather than a comment, which is the distinction gotcha 48 turns on: a comment
naming the other copies cannot fail.

The stronger fix is a conformance probe against the DEPLOYED pricing page —
`conformance/checks.py` already knows `DEFAULT_WIZARD_ORIGIN`, and it would
test what customers actually see rather than what the source says. It is
deliberately not shipped in launch week: the landing page is a Vite SPA, so
the figures live in the JS bundle rather than the HTML (gotcha 3's technique),
and a new network check that files GitHub issues on a deploy blip is the wrong
thing to add two days before a launch.

⚠ A stale price on a marketing page is a bad look. A stale price in
`Legal.tsx` is the billing TERMS quoting a number Stripe does not charge,
which is a different kind of problem — that is why it is named separately
below rather than lumped in with the landing page.
"""

from __future__ import annotations

from pathlib import Path

from helixis.billing.plans import PAID_PLANS, PLANS, TRIAL_DAYS, TRIAL_TURNS

#: The figures every customer-facing surface states, as of 2026-09-04.
#: Changing one means changing it in `plans.py` AND in the places named in
#: `_OTHER_COPIES` — the failure message walks you through it.
_PUBLISHED = {
    "trial_days": 14,
    "trial_turns": 150,
    "starter_price_usd": 50,
    "starter_turns_pooled": 150,
    "pro_price_usd": 199,
    "pro_turns_per_seat": 400,
    "scale_price_usd": 500,
    "scale_turns_pooled": 1200,
}

_OTHER_COPIES = """
Every place these numbers are written out by hand:

  1. frontend/src/components/BillingPanel.tsx
     Already fixed — it renders whatever GET /billing/status serves, so it
     needs NO edit. Confirm that is still true rather than assuming it.

  2. helixis-onboarding  src/Legal.tsx     (the billing TERMS)
     A SEPARATE REPO. It cannot import plans.py and this test cannot see it.
     THE TERMS PAGE — a figure there that Stripe does not charge is a
     consumer-protection problem, not a stale marketing number.
     ⚠ Until 2026-09-04 it said access was "currently free" and that pricing
     would be announced "before any billing begins". That went false the
     moment enforcement was switched on, so it now states the three plans.

  3. helixis-onboarding  src/Landing.tsx     (the public PRICING CARDS)
     A SEPARATE REPO, invisible to this test, and it DOES carry the figures:
     four cards spelling out Free / $50 / $199 / $500 with each allowance in
     words. Landed in `684a88d`; `grep -c '\\$199\\|\\$50\\|\\$500'` finds three.
     ⚠ It also states what each plan INCLUDES, which is now a second thing
     that can go stale: Starter's card read "every feature" until the Leasing
     pipeline became a Pro capability, and a pricing page that overstates the
     cheap tier is a refund rather than a conversion. If you move a capability
     between plans, this file and Legal.tsx both need the edit.

  4. This file. Update `_PUBLISHED` last, once the others are done, so the
     edit is a deliberate acknowledgement rather than the way you silence
     the failure.

And before changing a PRICE at all: re-derive it against the measured cost
per turn (plans.py's docstring, scripts/cost_drift_check.py). A price set
against a cost that has since moved is gotcha 48's expired justification
pointed at revenue.
"""


def test_the_trial_shape_matches_what_the_public_pages_state() -> None:
    assert (TRIAL_DAYS, TRIAL_TURNS) == (
        _PUBLISHED["trial_days"],
        _PUBLISHED["trial_turns"],
    ), f"the trial changed to {TRIAL_DAYS} days / {TRIAL_TURNS} questions.\n{_OTHER_COPIES}"


def test_the_paid_prices_match_what_the_public_pages_state() -> None:
    starter, pro, scale = PLANS["starter"], PLANS["pro"], PLANS["scale"]
    actual = {
        "starter_price_usd": starter.price_usd,
        "starter_turns_pooled": starter.turns_included,
        "pro_price_usd": pro.price_usd,
        "pro_turns_per_seat": pro.turns_included,
        "scale_price_usd": scale.price_usd,
        "scale_turns_pooled": scale.turns_included,
    }
    drifted = {k: (v, _PUBLISHED[k]) for k, v in actual.items() if v != _PUBLISHED[k]}
    assert not drifted, (
        "a plan figure changed and the public pages still say the old one:\n  "
        + "\n  ".join(f"{k}: plans.py={new}, pages say {old}" for k, (new, old) in drifted.items())
        + "\n"
        + _OTHER_COPIES
    )


def test_the_top_ceiling_stays_below_break_even() -> None:
    """Not a copy check — the reason the Pro number moved in the first place.

    It shipped at 2,500 on 2026-09-02, ABOVE break-even, and was cut hours
    later. At a cap past break-even a customer who uses everything they paid
    for costs more than they pay, so the plan is profitable only because most
    people do not finish it — a utilisation BET dressed as a guarantee. This
    reads the measured cost from `plans.py` rather than restating it, so
    re-measuring cost automatically re-checks the ceiling.
    """
    from helixis.billing.plans import MEASURED_MEAN_TURN_USD

    top = PLANS["scale"]
    cost_at_full_use = top.turns_included * MEASURED_MEAN_TURN_USD
    assert cost_at_full_use < top.price_usd, (
        f"{top.label}'s ceiling of {top.turns_included} turns costs "
        f"${cost_at_full_use:,.2f} at the measured mean of "
        f"${MEASURED_MEAN_TURN_USD}/turn, against a ${top.price_usd} price. "
        "A customer who uses what they bought loses money for us. Either lower the "
        "ceiling or lower the cost per turn — raising the price is the third option and "
        "needs the public pages changed too."
    )


def test_the_ladder_still_gives_a_reason_to_move_up() -> None:
    """The other constraint the top ceiling has to satisfy, and they oppose.

    Margin alone pushes the ceiling DOWN; the upgrade path pushes it UP. Two
    Pro seats already carry 800 turns for $398, so a Scale cap below that makes
    Scale strictly worse than buying seats and nobody moves. Pinning only the
    margin half would let a future 'safe' trim quietly kill the ladder.

    ⚠ Re-pointed 2026-09-04 when the ladder became Starter $50 / Pro $199 /
    Scale $500. The property is about the PER-SEAT plan and the flat plan above
    it, which used to be starter/pro and is now pro/scale — so the names moved
    and the rule did not. Resolved by shape rather than by name, so the next
    rename cannot leave this silently comparing the wrong two rungs.
    """
    per_seat = next(PLANS[k] for k in PAID_PLANS if PLANS[k].per_seat)
    above = [
        PLANS[k]
        for k in PAID_PLANS
        if not PLANS[k].per_seat and PLANS[k].price_usd > per_seat.price_usd
    ]
    assert above, "no flat plan sits above the per-seat one — the ladder has no top rung"
    top = min(above, key=lambda p: p.price_usd)

    two_seats_turns = per_seat.turn_allowance(2)
    two_seats_price = per_seat.price_usd * 2
    assert top.turns_included > two_seats_turns, (
        f"{top.label} gives {top.turns_included} turns for ${top.price_usd}, but two "
        f"{per_seat.label} seats give {two_seats_turns} for ${two_seats_price}. "
        "Nobody upgrades into less."
    )
    assert top.price_usd < per_seat.price_usd * 3, (
        f"{top.label} costs as much as three {per_seat.label} seats, so the comparison "
        "that makes it the better deal no longer holds"
    )


def test_the_catalog_the_app_serves_is_the_same_table() -> None:
    """Ties this file to the one surface that is already fixed.

    `BillingPanel` renders whatever `/billing/status` serves, so it needs no
    edit on a price change — but that is only true while the endpoint keeps
    building its catalog from `PLANS`. If it ever grows its own literals,
    surface (1) in the list above silently rejoins the hand-copied set.
    """
    from helixis.api.routes.billing import _plan_catalog

    served = {p.key: (p.price_usd, p.turns_included, p.per_seat) for p in _plan_catalog()}
    assert served == {
        k: (PLANS[k].price_usd, PLANS[k].turns_included, PLANS[k].per_seat) for k in PAID_PLANS
    }, "the served catalog no longer matches PLANS — the app is now a fourth hand-copied surface"


# ── the metered unit's NAME, across the surfaces this repo can see ──────


def test_the_billing_vocabulary_holds_across_the_surfaces_this_repo_owns() -> None:
    """Two words, and each belongs in a different place.

    ⚠ The unit has moved three times — "questions" (too narrow once the answer
    to "what counts" had to include things that are not questions), "requests"
    (accurate and too small; reads like an HTTP call), "credits" (a countable
    number invites the buyer to divide it by thirty and decide it is not
    enough) — and each move touched four surfaces with nothing holding them
    together. So the vocabulary is pinned rather than remembered:

      · the METER is "usage", and it is what the refusal names. No number:
        a customer who is cut off needs to know what to do, not what they
        spent.
      · the UNIT is an "answer", and it appears where a real figure belongs —
        the in-app panel, and the billing terms in the onboarding repo.

    ⚠ `gate.py`'s sentence is the one that matters most. It is read at the
    moment somebody is locked out, and a retired noun there tells them they
    ran out of something they never bought.

    ⚠ This cannot see the onboarding repo — separate checkout, absent in CI,
    and skipping when it is missing would report a pass for a check that never
    ran (the LED-013 skip-floor). Pricing.tsx and Legal.tsx are named in the
    Pricing.tsx vocabulary comment instead. What is enforced is the pair this
    repo owns.
    """
    root = Path(__file__).resolve().parents[2]
    gate = (root / "src/helixis/billing/gate.py").read_text(encoding="utf-8")
    panel = (root / "frontend/src/components/BillingPanel.tsx").read_text(encoding="utf-8")

    refusals = [ln for ln in gate.splitlines() if "You've reached" in ln]
    assert len(refusals) == 2, f"expected both exhaustion messages, found {len(refusals)}"
    blurbs = [ln for ln in panel.splitlines() if "${turns}" in ln]
    assert len(blurbs) == 2, f"expected both plan blurbs, found {len(blurbs)}"

    # ⚠ The unit noun is not on the blurb LINES any more. `planBlurb` builds
    # `turns` from a pluralising helper — `count(p.turnsIncluded, 'answer')` —
    # so the word sits one line above, and a per-line substring check reported
    # a correct panel as having lost its vocabulary. The question this test is
    # actually asking is "does the plan blurb state the unit", which is a
    # property of the FUNCTION, not of a line inside it; scoping it to the
    # function survives that refactor and the next one.
    blurb_fn = panel.split("function planBlurb(", 1)
    assert len(blurb_fn) == 2, "planBlurb has been renamed — re-point this guard"
    blurb_fn = blurb_fn[1].split("\n}", 1)[0]

    for line in refusals:
        assert "usage" in line, (
            "the refusal a locked-out customer reads must name the METER, which is "
            f"'usage':\n  {line.strip()}"
        )
        assert "turns_allowed" not in line, (
            "the refusal must not quote a number — the pricing page deliberately "
            f"sells usage rather than a countable unit:\n  {line.strip()}"
        )

    # The singular stem, because the helper pluralises: "1 answer" / "N
    # answers" both reach the screen from one literal.
    assert "answer" in blurb_fn, (
        "the in-app panel states the UNIT, which is 'answer':\n  "
        + "\n  ".join(ln.strip() for ln in blurb_fn.splitlines() if ln.strip())
    )

    retired = ("credits", "requests", "questions")
    for line in refusals + blurbs + blurb_fn.splitlines():
        for word in retired:
            assert word not in line, (
                f"'{word}' is a retired name for the metered unit:\n  {line.strip()}\n"
                "Changing the vocabulary means changing it HERE, in BillingPanel.tsx, "
                "and in the onboarding repo's Pricing.tsx and Legal.tsx."
            )
