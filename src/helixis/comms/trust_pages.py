"""The per-customer SMS privacy policy and terms pages a campaign points at.

Since 2026-06-30 ``POST /v1/Services/{MG}/Compliance/Usa2p`` **rejects** a
campaign that carries no ``PrivacyPolicyUrl`` and ``TermsAndConditionsUrl``
(error 30933), and Twilio FETCHES both URLs as part of the check — so they
must be publicly reachable HTTPS pages, not placeholders.

**The customer never writes these.** Every sentence below is a constant; the
only thing that varies is the business name, which they already gave us on the
registration form. That is the whole point: authoring a compliant privacy
policy is the single biggest source of rejections (error 30908), and a
property manager has no reason to know what carriers look for. Removing the
field removes the failure.

⚠ **One page per customer, never one shared page.** The brand being registered
is the CUSTOMER's business, so a policy naming Occupella describes the wrong
company and reads as a mismatch to the reviewer. It is also the difference
between a normal ISV pattern and the shared-URL shape that looks like a spam
farm across a customer base.

⚠ **The required strings are constants, not prose choices.** ``REQUIRED_PHRASES``
is what the pre-submission validator asserts, and it is sourced from the same
module that renders the page — so the check and the content cannot drift into
disagreeing (gotcha 28: read both sides). Rewording a sentence that carries a
required phrase means updating one place, and the test fails until it is.

⚠ **The no-sell sentence is load-bearing.** Error 30908 fires when the policy
omits it or says opt-in data IS shared. It is the most common rejection in the
2025-2026 window, and it costs the campaign vetting fee to discover.
"""

from __future__ import annotations

import html
import re

# The two documents this module serves. Used by the routes and by the URL
# builders, so a new page cannot be added without a URL for it.
PRIVACY = "privacy"
TERMS = "terms"

#: Substrings every rendered privacy page MUST contain, in the reviewer's own
#: terms. The validator asserts these against the LIVE fetched page rather than
#: against the template, so a broken route or a truncated render fails the
#: check instead of passing on intent.
#:
#: ⚠ Checked case-insensitively but stored in the exact rendered casing — a
#: phrase here that is not in the template is a test failure, by design.
REQUIRED_PHRASES: tuple[str, ...] = (
    # The 30908 sentence. Nothing else on the page matters as much.
    "does not sell, share, or otherwise provide",
    "message frequency varies",
    "message and data rates may apply",
    "reply stop",
    "help for help",
)


def page_path(company_id: str, document: str) -> str:
    """The path half of a trust-page URL. One definition, used by the route
    and the URL builder, so a rename cannot move one without the other."""
    return f"/c/{company_id}/{document}"


def page_url(base_url: str, company_id: str, document: str) -> str:
    """The absolute URL a campaign registration points at.

    ⚠ ``base_url`` must be a domain that actually SERVES ``page_path`` — Twilio
    fetches it. Passing a marketing domain that only proxies ``/api`` yields a
    404 the carrier reads as a missing policy, which is the same rejection as
    sending no URL at all.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    return f"{base}{page_path(company_id, document)}"


_STYLE = (
    "max-width:42rem;margin:3rem auto;padding:0 1.25rem;"
    "font:16px/1.65 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1a1a1a"
)


def _document(title: str, business: str, body: str) -> str:
    safe = html.escape(business)
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)} — {safe}</title></head>"
        f'<body style="{_STYLE}"><h1>{html.escape(title)}</h1>'
        f"<p><strong>{safe}</strong></p>{body}</body></html>"
    )


def render_privacy(business_name: str) -> str:
    """The SMS privacy policy for one customer.

    ⚠ Scoped to TEXT MESSAGING deliberately. This is not the customer's
    general website privacy policy and must not claim to be — it describes the
    messaging program the campaign registers, which is the only thing the
    reviewer is checking and the only thing we can truthfully speak to.
    """
    safe = html.escape(business_name)
    body = (
        f"<p>{safe} sends text messages to residents, applicants and vendors about "
        "maintenance requests and scheduling, rent reminders, appointment confirmations, "
        "and account notices relating to their property.</p>"
        # ⚠ The 30908 sentence. Do not reword without updating REQUIRED_PHRASES.
        f"<h2>We do not sell or share your information</h2><p>{safe} <strong>does not sell, "
        "share, or otherwise provide</strong> mobile opt-in information, SMS consent, or "
        "phone number data to third parties or affiliates for their own marketing or "
        "promotional purposes. Consent to receive text messages is never shared.</p>"
        "<h2>How we use your number</h2><p>Your mobile number is used only to send the "
        "messages described above and to respond when you reply. Message frequency varies. "
        "Message and data rates may apply.</p>"
        "<h2>Opting out</h2><p>Reply <strong>STOP</strong> to any message to stop receiving "
        "them. Reply <strong>HELP</strong> for help. Opting out is honoured immediately and "
        "applies to every number on file for you.</p>"
        "<h2>How long we keep it</h2><p>Your number and consent record are kept while you "
        "have an active relationship with "
        f"{safe}, and afterwards only as long as required to evidence consent and opt-out "
        "under applicable law.</p>"
        f"<h2>Contact</h2><p>To ask about this policy, contact {safe} directly.</p>"
    )
    return _document("SMS Privacy Policy", business_name, body)


def render_terms(business_name: str) -> str:
    """The messaging terms for one customer."""
    safe = html.escape(business_name)
    body = (
        f"<p>These terms cover the text message program operated by {safe}.</p>"
        "<h2>What you will receive</h2><p>Maintenance updates and scheduling, rent "
        "reminders, appointment confirmations, and account notices about your property. "
        "Message frequency varies.</p>"
        "<h2>Cost</h2><p>There is no charge from "
        f"{safe} for these messages. Message and data rates may apply from your mobile "
        "carrier.</p>"
        "<h2>Opting out</h2><p>Reply <strong>STOP</strong> at any time to stop receiving "
        "messages. Reply <strong>HELP</strong> for help, or contact "
        f"{safe} directly.</p>"
        "<h2>Carriers</h2><p>Carriers are not liable for delayed or undelivered messages.</p>"
        f"<h2>Changes</h2><p>{safe} may update these terms; the current version is always "
        "available at this address.</p>"
    )
    return _document("SMS Terms and Conditions", business_name, body)


def render(document: str, business_name: str) -> str:
    """Render one document by name. Raises on an unknown document rather than
    serving an empty page — a blank policy fetched by a carrier is a rejection
    that reads as a content problem rather than a routing one."""
    if document == PRIVACY:
        return render_privacy(business_name)
    if document == TERMS:
        return render_terms(business_name)
    raise ValueError(f"unknown trust document {document!r} — known: {PRIVACY}, {TERMS}")


def visible_text(html_source: str) -> str:
    """The page as a REVIEWER reads it — tags stripped, whitespace collapsed.

    ⚠ Load-bearing, and the guard caught its absence. A required phrase is
    routinely split by markup (``Reply <strong>STOP</strong>``), so a raw
    substring scan of the HTML reports a perfectly compliant page as missing
    the sentence — and the reverse, a phrase that only exists across a tag
    boundary, would read as present to us and as absent to a human. Checking
    the RENDERED text is the only version of the question a carrier asks.
    """
    return " ".join(re.sub(r"<[^>]+>", " ", html_source).split())


def missing_phrases(rendered: str) -> list[str]:
    """Required phrases absent from a rendered (or fetched) privacy page.

    Used BOTH by the unit tests against the template and by the pre-submission
    validator against the live URL, so the two cannot disagree about what
    compliant means. Accepts raw HTML or plain text — tags are stripped first.
    """
    haystack = visible_text(rendered).lower()
    return [p for p in REQUIRED_PHRASES if p.lower() not in haystack]


__all__ = [
    "PRIVACY",
    "REQUIRED_PHRASES",
    "TERMS",
    "missing_phrases",
    "page_path",
    "page_url",
    "render",
    "render_privacy",
    "render_terms",
    "visible_text",
]
