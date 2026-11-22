"""Leasing is a paid capability, and the check lives where it cannot be skipped.

Two plans do not carry the Leasing pipeline, for two different reasons
(founder calls, 2026-09-04):

- **Starter ($50)** — it is the reason the cheapest rung exists, and it is the
  honest line to draw, because Leasing costs real per-customer money (an A2P
  campaign and a phone number) that $50 does not cover at a measured 27%
  margin.
- **Trial** — arithmetic. A2P carrier approval takes ten to fifteen days and
  the trial is fourteen, so a trialist would get a section that cannot send.

Three properties, and each one has a silent failure mode:

1. **A plan without ``FEATURE_CRM`` is refused, with 402.** Not 403: the
   caller is exactly who they say they are and the answer is still no. A 403
   reads as "your role is wrong" and would send an admin to Team &
   Permissions to fix something that is not broken.

2. **An unreadable billing row ALLOWS.** Every other gate around Leasing
   fails closed, because they guard a carrier obligation. This one guards
   revenue, so it takes ``gate.py``'s rule 1 — a database blip that blacks
   out a section a Pro customer pays for costs far more than the sessions an
   outage lets through.

3. **The check is INSIDE ``_primary_company_id``.** Sixteen authenticated
   routes resolve a company that way, and a separate ``_require_crm_plan(...)``
   beside each of them is the per-call-site fan-out gotchas 25 and 81 both
   record: it holds until the seventeenth route forgets it, and a forgotten
   capability check fails open and silently. A behavioural test cannot see
   that (gotcha 62), so the last test here walks the module's AST.
"""

from __future__ import annotations

import ast
import inspect
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from helixis.api.routes import crm as crm_routes
from helixis.api.routes.crm import crm_plan_upgrade_detail
from helixis.billing.gate import BillingState
from helixis.billing.plans import FEATURE_CRM, PAID_PLANS, PLANS, cheapest_plan_with
from helixis.db.repositories.leads import LeadMetrics
from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
USER_ID = "00000000-0000-0000-0000-000000000001"


def _token() -> str:
    return jwt.encode(
        {
            "sub": USER_ID,
            "email": "brandon@example.com",
            "role": "authenticated",
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": TEST_ISSUER,
        },
        TEST_JWT_SECRET,
        algorithm="HS256",
    )


def _settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
        crm={"enabled": True},
    )


def _state(plan_key: str) -> BillingState:
    return BillingState(
        plan=PLANS[plan_key],
        status="active",
        seats=1,
        turns_used=0,
        turns_allowed=PLANS[plan_key].turns_included,
        trial_ends_at=None,
        period_end=datetime(2026, 10, 1, tzinfo=UTC),
        past_due=False,
    )


def _get_overview(state_return: object) -> int:
    """Status only — the status-code assertions below read this."""
    return _overview_response(state_return).status_code


def _overview_response(state_return: object):  # noqa: ANN202 — httpx.Response
    """Hit a real authenticated Leasing route with `load_state` pinned.

    ⚠ Returns the whole response rather than the status, because the REFUSAL'S
    WORDING is a property worth pinning and was the one this file did not
    cover — which is exactly how it came to name a plan the caller was not on
    for eighteen days with nothing failing.
    """
    settings = _settings()
    with (
        # Both, and neither is optional: `helixis.api.auth` reads the MODULE
        # resolver for the JWT secret (missing it 503s before any route runs,
        # which reads exactly like the gate refusing), while the route reads
        # `app.state.settings` for the CRM flag.
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
        patch("helixis.api.routes.crm.load_state", AsyncMock(return_value=state_return)),
        patch("helixis.api.routes.crm.get_pool", MagicMock(return_value=MagicMock())),
        # An EMPTY but well-formed pipeline. The repo is mocked because this
        # file measures the gate, not the query — but the shapes have to be
        # real, or a downstream 500 would read as the gate refusing and every
        # allow-case here would pass for the wrong reason.
        patch(
            "helixis.api.routes.crm.leads_repo.count_leads_by_stage",
            AsyncMock(return_value={}),
        ),
        patch(
            "helixis.api.routes.crm.leads_repo.recent_leads_by_stage",
            AsyncMock(return_value=[]),
        ),
        patch(
            "helixis.api.routes.crm.leads_repo.lead_metrics",
            AsyncMock(
                return_value=LeadMetrics(
                    new_this_month=0,
                    new_prev_month=0,
                    replies_this_month=0,
                    active=0,
                    leased=0,
                    total=0,
                )
            ),
        ),
        patch("helixis.api.routes.crm.leads_repo.count_cold_leads", AsyncMock(return_value=0)),
        patch(
            "helixis.api.auth._resolve_company_ids",
            side_effect=lambda _uid: [COMPANY_ID],
        ),
    ):
        app = create_app()
        app.state.settings = settings
        with TestClient(app) as client:
            res = client.get(
                "/api/v1/crm/overview",
                headers={"Authorization": f"Bearer {_token()}"},
            )
    return res


# ── the packaging decision ───────────────────────────────────────────


def test_starter_does_not_carry_leasing() -> None:
    """The line the whole tier exists to draw. If this flips, Starter stops
    having an upgrade reason and the $50 rung is just a smaller Pro."""
    assert not PLANS["starter"].includes(FEATURE_CRM)


def test_every_dearer_plan_does() -> None:
    for key in ("pro", "scale"):
        assert PLANS[key].includes(FEATURE_CRM), f"{key} lost Leasing"


def test_the_trial_does_not_carry_it_either() -> None:
    """⚠ REVERSED 2026-09-04, and the reason is arithmetic, not packaging.

    Texting a lead needs A2P 10DLC carrier approval, which takes ten to
    fifteen days. The trial is fourteen. So a trialist granted Leasing gets a
    section that cannot send anything before the trial ends — a setup
    checklist they never finish, which is a worse first impression than not
    offering it. /features and /pricing describe Leasing instead.

    An earlier version of this file asserted the opposite for a DISCOVERY
    reason (nobody should fail to find the half of the product they would not
    guess at). That reasoning was right about discovery and wrong about
    delivery.
    """
    assert not PLANS["trial"].includes(FEATURE_CRM)


# ── the route ────────────────────────────────────────────────────────


@pytest.mark.parametrize("plan_key", ["starter", "trial"])
def test_a_plan_without_leasing_is_refused_with_402(plan_key: str) -> None:
    """Both no-Leasing plans, not just the paid one.

    ⚠ The trial is here because it is the plan a stranger arrives on: if the
    packaging decision above ever drifts back, the visible symptom is a trial
    customer reaching a Leasing section they cannot send from — and this is
    the assertion that catches it at the route rather than at the card.
    """
    assert _get_overview(_state(plan_key)) == 402


def test_a_pro_company_is_let_through() -> None:
    """The other half. A gate that refuses everyone passes the test above."""
    assert _get_overview(_state("pro")) == 200


def test_an_unreadable_billing_row_is_let_through() -> None:
    """`gate.py` rule 1, pointed at a section. `load_state` returns None on a
    read failure AND on a missing row; both must open the pipeline."""
    assert _get_overview(None) == 200


@pytest.mark.parametrize("plan_key", ["pro", "scale"])
def test_no_plan_that_carries_leasing_is_refused(plan_key: str) -> None:
    assert _get_overview(_state(plan_key)) == 200


# ── what the refusal SAYS ────────────────────────────────────────────
#
# The gate was correct and its sentence was not. From 2026-09-04, when the
# founder call took FEATURE_CRM off the trial, every trialist who reached a
# Leasing route was told "isn't part of the Starter plan" — a plan they had
# never bought. It had one call site, no test asserted a word of it, and a
# string cannot fail (gotcha 48).


@pytest.mark.parametrize(
    ("plan_key", "must_not_name"),
    [("trial", "Starter"), ("starter", "Free trial")],
)
def test_the_refusal_names_the_plan_the_caller_is_actually_on(
    plan_key: str, must_not_name: str
) -> None:
    """Both directions, because a one-sided check passes against the defect.

    ⚠ The NEGATIVE is the one that matters and the positive alone would not
    have caught this: the broken constant named Starter, so a Starter caller
    read correctly while every trialist read a lie. Asserting only "the label
    appears" is satisfied by a message that names every plan in the catalogue
    (gotchas 38/51 — test the thing it must spare as well as the thing it must
    catch).
    """
    res = _overview_response(_state(plan_key))
    assert res.status_code == 402
    detail = res.json()["detail"]
    assert PLANS[plan_key].label in detail, f"402 does not name the caller's plan: {detail!r}"
    assert must_not_name not in detail, f"402 names a plan the caller is not on: {detail!r}"


def test_the_upgrade_target_is_derived_rather_than_typed() -> None:
    """The refusal points at the cheapest plan that actually carries Leasing.

    ⚠ `LeasingComplianceGate` had already worked this out on the client — "a
    client that wrote 'upgrade to Pro' itself would still say Pro" — and the
    server was the caller that rule never reached (gotchas 25/67). Pinned
    against `cheapest_plan_with` rather than the literal "Pro", so moving
    Leasing to another rung updates the message instead of falsifying it.
    """
    target = cheapest_plan_with(FEATURE_CRM)
    assert target is not None, "no purchasable plan carries Leasing"
    detail = _overview_response(_state("trial")).json()["detail"]
    assert target.label in detail


def test_the_cheapest_carrier_is_purchasable_and_in_ladder_order() -> None:
    """Two properties one lookup could get wrong in opposite directions.

    ⚠ It must skip the TRIAL: the trial is not purchasable, so naming it as an
    upgrade target would tell a Starter customer to move DOWN to get a
    capability. And it must take the FIRST carrier in ladder order, or the
    message sends somebody to Scale when Pro would do.
    """
    target = cheapest_plan_with(FEATURE_CRM)
    assert target is not None
    assert target.key in PAID_PLANS
    assert target.key != "trial"
    assert target.key == next(k for k in PAID_PLANS if PLANS[k].includes(FEATURE_CRM))


def test_a_capability_no_plan_sells_yields_no_upgrade_instruction() -> None:
    """An instruction naming an upgrade that does not exist is one the reader
    cannot carry out, which this repo records as worse than none (gotchas
    102/106/118/133). `None` is the honest answer, and the caller must render a
    sentence with no upgrade clause rather than crash on it.

    ⚠ The lookup has to be PATCHED, and a first version of this test did not
    do that: it asserted `cheapest_plan_with("a-capability-no-plan-carries") is
    None` and then called `crm_plan_upgrade_detail`, which resolves
    `FEATURE_CRM` internally and gets Pro — so the `None` branch was never
    reached and deleting it left every assertion green. The mutation battery is
    what found it (gotcha 58: the mutant plainly differed, so the gap was in
    the test). This supplies the one input the two implementations disagree on.
    """
    assert cheapest_plan_with("a-capability-no-plan-carries") is None
    with patch("helixis.api.routes.crm.cheapest_plan_with", return_value=None):
        detail = crm_plan_upgrade_detail(PLANS["trial"])
    assert PLANS["trial"].label in detail
    assert "Upgrade to" not in detail, f"names an upgrade that does not exist: {detail!r}"


# ── the wiring (gotcha 62 — a behavioural test cannot see this) ───────


def test_the_company_resolver_is_the_gate() -> None:
    """⚠ Reads BOTH halves: the resolver is async AND consults `load_state`.

    Asserting only that `load_state` is imported would pass against a module
    that imported it and never called it — which is precisely how a
    capability check ends up wired to nothing.
    """
    tree = ast.parse(Path(inspect.getfile(crm_routes)).read_text(encoding="utf-8"))
    resolver = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and n.name == "_primary_company_id"
    )
    assert isinstance(resolver, ast.AsyncFunctionDef), (
        "_primary_company_id went back to being sync, which means the plan "
        "lookup it holds was removed — every Leasing route is open again."
    )
    calls = {
        n.func.id
        for n in ast.walk(resolver)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "load_state" in calls, "the resolver no longer reads the billing row"


def test_no_route_reaches_around_the_resolver_for_a_company_id() -> None:
    """`user.company_ids[0]` anywhere else is a bypass.

    The gate is only unskippable while the resolver is the ONE way a route
    learns whose company it is. A route that indexes the token's claim
    directly gets a company id and no plan check, and nothing about it looks
    wrong — which is the shape this test exists to catch.
    """
    source = Path(inspect.getfile(crm_routes)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if fn.name == "_primary_company_id":
            continue  # the one legitimate reader
        for node in ast.walk(fn):
            if isinstance(node, ast.Attribute) and node.attr == "company_ids":
                offenders.append(f"{fn.name} (line {node.lineno})")
    assert not offenders, (
        "these read the caller's companies without passing through "
        f"_primary_company_id, so they skip the plan gate: {offenders}"
    )
