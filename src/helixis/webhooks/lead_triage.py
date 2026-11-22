"""Decide which inbound emails are **CRM leads**, and project one into a
lead row.

This is the deterministic core of CRM lead ingestion — the mirror of
:mod:`helixis.webhooks.email_triage`, but for the *inverse* population.
Where ``email_triage.should_surface`` keeps long emails **from a known
Buildium contact** (an existing tenant/owner/vendor), a lead is almost
always from **someone new** — a renter inquiring on a listing, an owner
shopping for a PM. So the gate here is: *not* an existing contact, and it
looks like an inquiry (came through a listing site, or the text reads like
one).

Everything here is a PURE function — no network, no DB, no LLM — so it's
fully unit-testable. The live seams (poll the leads inbox via Composio,
fetch the company's Buildium contacts to exclude, persist via
``repositories.leads.insert_lead``) call into these functions; they are
NOT implemented here. The whole path is gated by ``HELIXIS_CRM__ENABLED``.

Identity note (for the live slice, NOT done here): a lead is keyed on
email first, phone second. Listing sites often mask the renter's address
behind a relay (``jordan.r.a1b2@convo.zillow.com``) — that relay is what
lands in ``email`` and what a reply must thread back to.
"""

from __future__ import annotations

from helixis.db.repositories.leads import LeadRow
from helixis.webhooks.email_triage import (
    InboundEmail,
    is_known_contact,
    normalize_email_address,
)

# Listing-site / ILS sender domains → the CRM source they map to. A message
# from one of these is a lead by origin, regardless of wording. Extend as
# new syndication partners are added.
LISTING_SENDER_DOMAINS: dict[str, str] = {
    "zillow.com": "zillow",
    "convo.zillow.com": "zillow",
    "trulia.com": "zillow",
    "hotpads.com": "zillow",
    "apartments.com": "apartments",
    "apartmentlist.com": "apartments",
    "realtor.com": "apartments",
}

# Text signals that a free-form email is a rental/management inquiry. Used
# only when the sender domain isn't a known listing site. Deliberately
# lowercase substrings so a simple ``in`` test over the lowered text works.
INQUIRY_KEYWORDS: tuple[str, ...] = (
    "interested in",
    "still available",
    "is it available",
    "schedule a tour",
    "book a tour",
    "set up a showing",
    "apply",
    "application",
    "for rent",
    "leasing",
    "inquir",  # inquiry / inquire / enquiry
    "move-in",
    "move in",
    "looking to rent",
    "manage my propert",  # owner lead
    "property management",
)


def sender_domain(from_email: str | None) -> str:
    """Lowercased domain part of a sender address, or ``""``."""
    addr = normalize_email_address(from_email)
    if "@" not in addr:
        return ""
    return addr.rsplit("@", 1)[1]


def classify_source(from_email: str | None) -> str:
    """Map a sender to a CRM source. Known listing domains resolve to their
    partner; everything else defaults to ``"email"`` (a direct inbound). A
    web-form notifier would be classified upstream by the form poster, not
    here."""
    domain = sender_domain(from_email)
    if not domain:
        return "email"
    # Exact match, then parent-domain match (relay subdomains like
    # ``convo.zillow.com`` fall through to ``zillow.com``).
    if domain in LISTING_SENDER_DOMAINS:
        return LISTING_SENDER_DOMAINS[domain]
    for known, source in LISTING_SENDER_DOMAINS.items():
        if domain.endswith("." + known):
            return source
    return "email"


def looks_like_inquiry(email: InboundEmail) -> bool:
    """True when the subject/body reads like a rental or management inquiry."""
    haystack = f"{email.subject or ''}\n{email.body_text or ''}".lower()
    return any(kw in haystack for kw in INQUIRY_KEYWORDS)


def is_lead(email: InboundEmail, known_contact_emails: frozenset[str]) -> bool:
    """The gate: an email is a CRM lead when it is **not** from an existing
    Buildium contact AND (it came from a listing site OR it reads like an
    inquiry). Existing contacts route to the Inbox pipeline instead — a lead
    is by definition someone not yet in the book."""
    if is_known_contact(email.from_email, known_contact_emails):
        return False
    from_listing = classify_source(email.from_email) != "email"
    return from_listing or looks_like_inquiry(email)


def _display_name(email: InboundEmail) -> str:
    """Best-effort lead name — the sender's display name, else the address
    local-part title-cased, else empty."""
    name = (email.from_name or "").strip()
    if name:
        return name
    addr = normalize_email_address(email.from_email)
    if "@" in addr:
        local = addr.split("@", 1)[0]
        # "jordan.reyes" → "Jordan Reyes"; drop relay noise after a '+'.
        local = local.split("+", 1)[0].replace(".", " ").replace("_", " ").strip()
        if local:
            return local.title()
    return ""


def build_lead_from_sms(from_number: str, body: str, company_id: str) -> LeadRow:
    """Project an inbound SMS into a new-lead :class:`LeadRow` (source
    ``sms``, stage ``new``). The phone is the identity + dedup key; the
    first text becomes the note. Name is unknown at first contact (a phone
    number carries no display name), so it's left blank for the PM to fill
    or for a later reply to reveal."""
    text = (body or "").strip()
    return LeadRow(
        company_id=company_id,
        name="",
        kind="renter",
        stage="new",
        source="sms",
        email=None,
        phone=(from_number or "").strip() or None,
        unit_label=None,
        note=text[:280] or None,
        raw={"from_number": from_number, "first_message": text},
    )


def build_lead_from_call(from_number: str, company_id: str) -> LeadRow:
    """Project an inbound phone call into a new-lead :class:`LeadRow` (source
    ``sms`` — the phone channel; stage ``new``). Like an inbound text, a call
    carries only the number: no name (a number has no display name) and no
    unit. The note records that they called so the card isn't blank."""
    return LeadRow(
        company_id=company_id,
        name="",
        kind="renter",
        stage="new",
        source="sms",
        email=None,
        phone=(from_number or "").strip() or None,
        unit_label=None,
        note="Called in — no message yet.",
        raw={"from_number": from_number, "channel": "call"},
    )


def build_lead_from_email(email: InboundEmail, company_id: str) -> LeadRow:
    """Project an inbound email into a new-lead :class:`LeadRow` (stage
    ``new``). Kind defaults to ``renter`` — the common case for inbound
    listing inquiries; owner classification (from web-form context or the
    inquiry text) is a later refinement. The raw email is carried in
    ``raw`` for debugging + future re-parse."""
    subject = (email.subject or "").strip()
    first_line = (email.body_text or "").strip().splitlines()[0:1]
    note = subject or (first_line[0] if first_line else "")
    return LeadRow(
        company_id=company_id,
        name=_display_name(email),
        kind="renter",
        stage="new",
        source=classify_source(email.from_email),
        email=normalize_email_address(email.from_email) or None,
        phone=None,
        unit_label=None,
        note=note[:280] or None,
        raw={
            "message_id": email.message_id,
            "thread_id": email.thread_id,
            "subject": subject,
            "from_email": email.from_email,
            "from_name": email.from_name,
            "received_at": email.received_at,
            "provider": email.provider,
        },
    )
