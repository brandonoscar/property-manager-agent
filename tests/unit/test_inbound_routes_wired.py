"""Both inbound routes must actually CALL the shared halves.

Gotcha 62's rule: a behavioural test that drives a helper directly cannot see
that nothing calls it — ``comms_triage`` was complete, unit-tested, and wired
to nothing for months. Every fix in this change is a helper plus a call site,
so each call site is asserted on the AST.

⚠ Static on purpose, and NOT a substitute for the behavioural tests beside it
(``test_crm_routes.py``, ``test_sms_inbound.py``). These answer "is it still
wired", which is the question a passing behavioural suite cannot answer when
the fixture happens to make the missing branch invisible.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROUTES = {
    "crm": Path("src/helixis/api/routes/crm.py"),
    "twilio": Path("src/helixis/api/routes/twilio.py"),
}


def _called_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_both_inbound_routes_apply_consent_keywords(route: str) -> None:
    """A Twilio number has ONE inbound URL. Whichever route the console names
    has to honour STOP — a missed opt-out is a TCPA violation, while a missed
    lead is an inconvenience."""
    assert "apply_consent_keywords" in _called_names(_ROUTES[route]), (
        f"{route} no longer mirrors STOP/START into the consent ledger. If this "
        "route is the one the Twilio console points at, opt-outs are silently "
        "ignored."
    )


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_both_inbound_routes_ingest_leads(route: str) -> None:
    """The other half of the same problem: pointing the number at the consent
    route used to mean no lead ever arrived."""
    assert "handle_inbound_lead_sms" in _called_names(_ROUTES[route]), (
        f"{route} no longer ingests leads. If this route is the one the console "
        "points at, Leasing receives nothing."
    )


@pytest.mark.parametrize("route", sorted(_ROUTES))
def test_both_inbound_routes_resolve_the_company_from_the_number(route: str) -> None:
    assert "resolve_company_for_number" in _called_names(_ROUTES[route]), (
        f"{route} is no longer resolving the company from the receiving number "
        "— a global default files every deployment's inbound into one company"
    )


def test_the_crm_inbound_route_has_no_signature_bypass() -> None:
    """``skip_signature_check`` is TESTING ONLY and must not reach a route that
    creates records or revokes consent. It legitimately gates ``/sms/simulate``
    and the voice simulators, so this counts USES rather than banning the name.

    The bar is the count at the time of the fix: four simulate-style gates and
    zero on the real inbound. A fifth use needs a deliberate look, which is the
    point of pinning a number rather than a boolean.
    """
    src = _ROUTES["crm"].read_text(encoding="utf-8")
    tree = ast.parse(src)
    uses = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "skip_signature_check"
    ]
    assert len(uses) <= 4, (
        f"skip_signature_check now has {len(uses)} call sites (was 4, all on the "
        "keyless simulate routes). A new one on a real webhook lets an unsigned "
        "request forge leads or consent revocations."
    )

    # And specifically: the real inbound handler must not consult it.
    inbound = next(
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "inbound_sms"
    )
    assert not [
        n
        for n in ast.walk(inbound)
        if isinstance(n, ast.Attribute) and n.attr == "skip_signature_check"
    ], "the real /crm/sms/inbound route consults skip_signature_check again"


def test_the_crm_reply_supports_a_messaging_service() -> None:
    """A Messaging Service is a number POOL — the thing that lets a new number
    be added in the Twilio console instead of a Render env var per number.
    ``comms/sms.py`` supported it; this lane hard-required ``from_number`` and
    always posted ``From``, so a pool-only deployment failed every reply."""
    src = _ROUTES["crm"].read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "send_sms"
    ]
    assert calls, "no twilio_client.send_sms call found in the CRM route"
    assert any(any(kw.arg == "messaging_service_sid" for kw in call.keywords) for call in calls), (
        "the CRM reply no longer passes messaging_service_sid — a "
        "Messaging-Service-only deployment cannot send"
    )
