"""Pure lead-triage tests — the CRM ingestion gate + email→lead projection.

Mirrors ``test_email_triage.py``. Everything under test is a pure function,
so no mocks: build an ``InboundEmail``, assert the classification.
"""

from __future__ import annotations

from helixis.webhooks.email_triage import InboundEmail, normalize_contact_set
from helixis.webhooks.lead_triage import (
    build_lead_from_email,
    build_lead_from_sms,
    classify_source,
    is_lead,
    looks_like_inquiry,
    sender_domain,
)


def _email(
    *,
    from_email: str,
    subject: str = "",
    body: str = "",
    from_name: str = "",
    message_id: str = "m1",
) -> InboundEmail:
    return InboundEmail(
        message_id=message_id,
        from_email=from_email,
        subject=subject,
        body_text=body,
        received_at="2026-07-04T12:00:00Z",
        from_name=from_name,
    )


# ── source classification ────────────────────────────────────────────


def test_sender_domain_extracts_domain():
    assert sender_domain("Jordan <jordan@convo.zillow.com>") == "convo.zillow.com"
    assert sender_domain("garbage") == ""


def test_classify_source_known_listing_domains():
    assert classify_source("no-reply@zillow.com") == "zillow"
    assert classify_source("leads@apartments.com") == "apartments"


def test_classify_source_relay_subdomain_falls_through_to_parent():
    # A masked relay like convo.zillow.com maps to its parent partner.
    assert classify_source("jordan.r.a1b2@convo.zillow.com") == "zillow"


def test_classify_source_unknown_domain_is_email():
    assert classify_source("someone@gmail.com") == "email"


# ── inquiry detection ────────────────────────────────────────────────


def test_looks_like_inquiry_matches_keywords():
    assert looks_like_inquiry(_email(from_email="a@gmail.com", subject="Is it available?"))
    assert looks_like_inquiry(
        _email(from_email="a@gmail.com", body="Hi, I'm interested in the 2 bed for rent.")
    )


def test_looks_like_inquiry_false_for_unrelated_text():
    assert not looks_like_inquiry(
        _email(from_email="a@gmail.com", subject="Lunch?", body="Want to grab lunch tomorrow?")
    )


# ── the gate ─────────────────────────────────────────────────────────


def test_is_lead_true_for_listing_sender_even_without_keywords():
    email = _email(from_email="jordan@convo.zillow.com", subject="New contact")
    assert is_lead(email, frozenset())


def test_is_lead_true_for_unknown_sender_with_inquiry_text():
    email = _email(from_email="stranger@gmail.com", subject="Is the unit still available?")
    assert is_lead(email, frozenset())


def test_is_lead_false_for_known_buildium_contact():
    # An existing tenant/owner/vendor is NOT a lead, even if they ask an
    # inquiry-shaped question — they route to the Inbox pipeline instead.
    contacts = normalize_contact_set(["tenant@example.com"])
    email = _email(from_email="tenant@example.com", subject="Is the unit available for my friend?")
    assert not is_lead(email, contacts)


def test_is_lead_false_for_unknown_sender_without_inquiry_signal():
    email = _email(from_email="newsletter@marketing.com", subject="Weekly digest")
    assert not is_lead(email, frozenset())


# ── projection ───────────────────────────────────────────────────────


def test_build_lead_from_email_maps_fields():
    email = _email(
        from_email="jordan.r.a1b2@convo.zillow.com",
        from_name="Jordan Reyes",
        subject="Interested in Maple Court 4B",
        body="Is 4B still available? I could tour this weekend.",
        message_id="msg-42",
    )
    lead = build_lead_from_email(email, company_id="co-1")
    assert lead.company_id == "co-1"
    assert lead.name == "Jordan Reyes"
    assert lead.kind == "renter"
    assert lead.stage == "new"
    assert lead.source == "zillow"
    assert lead.email == "jordan.r.a1b2@convo.zillow.com"
    assert lead.note == "Interested in Maple Court 4B"
    assert lead.raw is not None
    assert lead.raw["message_id"] == "msg-42"


def test_build_lead_derives_name_from_address_when_no_display_name():
    email = _email(from_email="dana.whitfield@gmail.com", subject="Looking to rent")
    lead = build_lead_from_email(email, company_id="co-1")
    assert lead.name == "Dana Whitfield"


# ── SMS projection ───────────────────────────────────────────────────


def test_build_lead_from_sms_maps_phone_and_source():
    lead = build_lead_from_sms("+14155551234", "Hi, is the 2 bed still available?", "co-1")
    assert lead.company_id == "co-1"
    assert lead.kind == "renter"
    assert lead.stage == "new"
    assert lead.source == "sms"
    assert lead.phone == "+14155551234"
    assert lead.email is None
    assert lead.note == "Hi, is the 2 bed still available?"
    assert lead.raw is not None
    assert lead.raw["from_number"] == "+14155551234"


def test_build_lead_from_sms_handles_blank_body():
    lead = build_lead_from_sms("+14155551234", "", "co-1")
    assert lead.phone == "+14155551234"
    assert lead.note is None
