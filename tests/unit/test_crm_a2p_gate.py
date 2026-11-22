"""The CRM lead-reply lane bypassed the A2P dark flag.

``comms/sms.py::send_sms`` refuses to send while ``twilio.enabled`` is false
— that flag means "carriers have not approved the 10DLC campaign", and it is
the whole reason outbound SMS ships dark. The CRM's ``POST /crm/leads/{id}/
reply`` checked ``twilio.configured`` ONLY.

⚠ Those are not the same question. ``configured`` asks whether credentials
exist; ``enabled`` asks whether we are ALLOWED to send. So the moment Twilio
secrets landed in Render, the CRM would have begun texting real leads from
the registered number before the campaign was approved — the exact violation
the flag exists to prevent, from a lane nobody thought of as "the SMS
feature".

The settings docstring is why it was missed: it scopes ``enabled`` to
"``comms/sms.py`` — templated rent reminders / maintenance updates", which
reads as module-local. A2P registration is a property of the NUMBER, and both
lanes send from the same ``from_number``.

Gotcha 25's rule, applied to a compliance flag rather than a data field: the
rule lives once (``outbound_channel_block``) and every layer that sends reads
it. These tests read BOTH sides — the predicate and the route — because a
predicate nothing calls is the failure mode this file exists to prevent
(gotcha 62).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

from helixis.comms.sms import outbound_channel_block


def _twilio(**over: object) -> SimpleNamespace:
    base = {
        "enabled": True,
        "account_sid": "ACxxxx",
        "auth_token": "tok",
        "from_number": "+18005551212",
        "messaging_service_sid": "",
    }
    base.update(over)
    return SimpleNamespace(**base)


# ── the predicate ────────────────────────────────────────────────────


def test_an_approved_configured_channel_may_send() -> None:
    assert outbound_channel_block(_twilio()) is None


def test_a_dark_channel_is_blocked_even_when_fully_configured() -> None:
    """The defect in one line: credentials present, approval absent. Every
    field Twilio needs is set and it still must not send."""
    assert outbound_channel_block(_twilio(enabled=False)) == "disabled"


def test_an_approved_but_unconfigured_channel_is_blocked() -> None:
    assert outbound_channel_block(_twilio(account_sid="")) == "not_configured"
    assert outbound_channel_block(_twilio(auth_token="")) == "not_configured"


def test_a_messaging_service_counts_as_a_sender() -> None:
    """Both directions. Over-blocking a valid Messaging-Service deployment
    would be a fresh bug wearing a compliance costume."""
    approved = _twilio(from_number="", messaging_service_sid="MG123")
    assert outbound_channel_block(approved) is None


def test_dark_wins_over_unconfigured() -> None:
    """Ordering is deliberate: a dark channel reports ``disabled`` even when
    it is also unconfigured, because "finish setting up Twilio" is the wrong
    instruction to give someone whose real blocker is carrier approval."""
    assert outbound_channel_block(_twilio(enabled=False, account_sid="")) == "disabled"


# ── the route actually calls it ──────────────────────────────────────


def test_the_crm_reply_route_consults_the_shared_predicate() -> None:
    """A predicate nothing calls is the ``comms_triage`` failure (gotcha 62):
    complete, unit-tested, and wired to nothing. Behavioural coverage of the
    route lives in ``test_crm_routes.py``; this asserts the CALL SITE exists,
    which a behavioural test cannot see going missing if the fixture happens
    to be approved.
    """
    src = Path("src/helixis/api/routes/crm.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    # ⚠ The predicate MOVED, it did not go away. ``outbound_block_for_company``
    # wraps ``outbound_channel_block`` and adds this company's own campaign,
    # because A2P approval is per company while ``twilio.enabled`` is one flag
    # for the deployment. Asserting the old name here would now fail on the
    # stronger check — so the name is updated and the bound is not: a route
    # calling neither is still the gotcha-67 regression.
    assert "outbound_block_for_company" in called, (
        "the CRM reply route no longer consults the channel gate — it is back "
        "to checking `configured` only, which lets it text leads before the "
        "A2P campaign is approved"
    )
    assert "outbound_channel_block" not in called, (
        "the CRM lane dropped to the ACCOUNT-level predicate alone, which "
        "cannot see that this company's own campaign is still pending"
    )


def test_send_sms_and_the_crm_share_one_definition() -> None:
    """``send_sms`` must not re-derive the channel rule inline. Two copies is
    how the CRM came to disagree with it in the first place — and the copy
    that drifts is always the one nobody is looking at."""
    src = inspect.getsource(__import__("helixis.comms.sms", fromlist=["x"]).send_sms)
    assert "outbound_block_for_company(" in src, (
        "send_sms stopped using the shared predicate — the two outbound lanes "
        "can now drift on whether the channel may send at all"
    )
