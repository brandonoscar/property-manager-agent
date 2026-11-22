"""Guards for the per-customer SMS policy pages a campaign registration cites.

Since 2026-06-30 a campaign without ``PrivacyPolicyUrl`` +
``TermsAndConditionsUrl`` is hard-rejected (30933), and Twilio FETCHES both —
so three separate things have to hold together and each fails silently on its
own:

  1. the payload carries the fields,
  2. the URL points at a path this app actually serves,
  3. the page at that path contains the sentences a reviewer looks for.

Testing any one alone passes while the chain is broken, which is gotcha 28's
both-sides rule applied to a URL: the builder and the route are read together
here, and the required phrases are asserted against the RENDERED page rather
than against the template's intent.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from helixis.api.routes import trust as trust_route
from helixis.comms import a2p, trust_pages

BUSINESS = "Blue Ridge Properties LLC"
COMPANY = "5ca1ab1e-0000-4000-8000-000000000002"
BASE = "https://api.occupella.com"


# ── the content the reviewer is actually checking ─────────────────────


def test_the_privacy_page_carries_every_required_phrase() -> None:
    """⚠ THE ONE THAT MATTERS. Error 30908 fires when the policy omits the
    no-sell sentence, and it is the most common rejection in this window —
    discovered only after the campaign vetting fee is spent."""
    assert trust_pages.missing_phrases(trust_pages.render_privacy(BUSINESS)) == []


def test_the_page_names_the_customer_not_occupella() -> None:
    """The brand being registered is the CUSTOMER's business. A policy naming
    Occupella describes a company the reviewer is not looking at — and one
    shared page across every customer is the snowshoe-spam shape."""
    page = trust_pages.render_privacy(BUSINESS)
    assert BUSINESS in page
    assert "Occupella" not in page


def test_both_documents_render_and_differ() -> None:
    privacy = trust_pages.render(trust_pages.PRIVACY, BUSINESS)
    terms = trust_pages.render(trust_pages.TERMS, BUSINESS)
    assert BUSINESS in terms
    assert privacy != terms, "terms and privacy must not be the same document"


def test_an_unknown_document_raises_rather_than_rendering_blank() -> None:
    """A blank page fetched by a carrier is a CONTENT rejection — it sends the
    next reader to the wording instead of to the routing."""
    with pytest.raises(ValueError):
        trust_pages.render("cookies", BUSINESS)


def test_a_business_name_cannot_inject_markup() -> None:
    """The name comes from a customer-filled form and lands in HTML."""
    page = trust_pages.render_privacy("<script>alert(1)</script>")
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_the_phrase_check_can_actually_fail() -> None:
    """A validator that returns [] for everything is worse than none — it
    would report a broken or truncated fetch as compliant."""
    assert trust_pages.missing_phrases("nothing compliant here") == list(
        trust_pages.REQUIRED_PHRASES
    )


# ── the URL, read against the route that serves it ────────────────────


def test_the_url_points_at_a_path_this_app_serves() -> None:
    """⚠ BOTH SIDES (gotcha 28). The builder and the route must agree, or the
    campaign cites a 404 — which a carrier reads as a missing policy, exactly
    the same rejection as sending no URL at all.

    The route's path is read from the LIVE decorator rather than restated, so
    renaming one half fails here instead of in production.
    """
    served = {r.path for r in trust_route.router.routes if getattr(r, "path", "").startswith("/c/")}
    assert served, "the trust router serves no /c/ route"
    template = next(iter(served))

    for document in (trust_pages.PRIVACY, trust_pages.TERMS):
        built = trust_pages.page_url(BASE, COMPANY, document)
        assert built.startswith(BASE)
        # Substitute the route's own path params and require an exact match.
        expected = template.replace("{company_id}", COMPANY).replace("{document}", document)
        assert built == f"{BASE}{expected}"


def test_a_trailing_slash_does_not_double_up() -> None:
    """The URL is fetched by Twilio; `//c/...` is a different path."""
    assert trust_pages.page_url(BASE + "/", COMPANY, trust_pages.PRIVACY) == (
        f"{BASE}/c/{COMPANY}/{trust_pages.PRIVACY}"
    )


def test_no_base_url_yields_no_url_rather_than_a_relative_one() -> None:
    """A relative path would be accepted by our own payload builder and then
    fetched by Twilio as garbage. Empty makes `campaign_payload` refuse."""
    assert trust_pages.page_url("", COMPANY, trust_pages.PRIVACY) == ""


def test_the_route_is_registered_without_the_api_prefix() -> None:
    """⚠ Carriers fetch these unauthenticated and with no /api/v1 knowledge.
    An AST guard on the call site, because a behavioural test of the router
    cannot see how main.py mounts it (gotcha 62)."""
    tree = ast.parse(
        Path(inspect.getsourcefile(trust_route) or "")
        .parent.parent.parent.joinpath("main.py")
        .read_text(encoding="utf-8")
    )
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "include_router"
            and node.args
            and isinstance(node.args[0], ast.Attribute)
            and isinstance(node.args[0].value, ast.Name)
            and node.args[0].value.id == "trust"
        ):
            assert not node.keywords, (
                "the trust router must mount at the ROOT — a prefix puts the "
                "policy pages somewhere no carrier will look for them"
            )
            return
    raise AssertionError("main.py no longer registers the trust router")


# ── the campaign payload: the P0 itself ───────────────────────────────


def _details() -> a2p.BusinessDetails:
    return a2p.BusinessDetails(
        legal_business_name=BUSINESS,
        business_type="LLC",
        business_vertical="REAL_ESTATE",
        street="200 E Colfax Ave",
        city="Denver",
        region="CO",
        postal_code="80203",
        rep_first_name="Aaron",
        rep_last_name="Tillman",
        rep_email="aaron@example.com",
        rep_phone="+13035550100",
        rep_job_position="Director",
        campaign_description="Property management notifications.",
        opt_in_description="Residents provide their number on the lease.",
        sample_messages=["Blue Ridge: rent is due. Reply STOP to opt out."],
    )


def test_the_campaign_payload_carries_both_policy_urls() -> None:
    """The whole point: since 2026-06-30 a campaign without these is a hard
    400 (30933), AFTER the brand fee has already been charged."""
    payload = a2p.campaign_payload(
        _details(),
        brand_sid="BN1",
        privacy_policy_url=f"{BASE}/c/{COMPANY}/privacy",
        terms_url=f"{BASE}/c/{COMPANY}/terms",
    )
    assert payload["PrivacyPolicyUrl"] == f"{BASE}/c/{COMPANY}/privacy"
    assert payload["TermsAndConditionsUrl"] == f"{BASE}/c/{COMPANY}/terms"


@pytest.mark.parametrize(
    ("privacy", "terms"),
    [("", f"{BASE}/t"), (f"{BASE}/p", ""), ("", ""), ("   ", "   ")],
)
def test_a_missing_policy_url_refuses_rather_than_submitting(privacy: str, terms: str) -> None:
    """⚠ Refusing beats sending a body we KNOW will 400. The brand is
    registered several steps earlier and its fee is already spent, so the
    campaign step is the expensive place to discover a missing field."""
    with pytest.raises(a2p.A2PError) as exc:
        a2p.campaign_payload(
            _details(), brand_sid="BN1", privacy_policy_url=privacy, terms_url=terms
        )
    assert exc.value.step == "campaign"


def test_the_pipeline_builds_the_urls_from_the_reachable_base() -> None:
    """⚠ The pipeline must use the SAME base URL Twilio already reaches for
    the inbound webhooks. A marketing domain that only proxies /api would 404
    here, which a carrier reads as a missing policy (gotcha 62: read the call
    site, since a behavioural test of the builder cannot see which base the
    caller hands it)."""
    from helixis.comms import a2p_pipeline

    src = inspect.getsource(a2p_pipeline.submit_campaign)
    tree = ast.parse(src.lstrip())
    bases = {
        ast.unparse(node.args[0])
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "page_url"
        and node.args
    }
    assert bases == {"creds.webhook_base_url"}, (
        f"submit_campaign builds policy URLs from {bases or 'nothing'} — it must use the "
        "webhook base URL, which is the domain that actually serves the pages"
    )
