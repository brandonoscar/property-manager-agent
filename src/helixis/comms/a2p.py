"""A2P 10DLC registration run on the customer's behalf — the ISV pipeline.

White-label means the customer never opens a carrier account, never sees a
vendor name and never leaves Occupella. They fill in their business details in
the app; this module submits the registration against OUR messaging account
using the ISV ("secondary customer profile") flow.

The chain, each link returning a SID we must keep:

    1. CustomerProfile   — a secondary profile: their business under ours
    2. EndUser x2        — business info + the authorized representative
    3. SupportingDocument— the business address
    4. bundle the above onto the CustomerProfile, then submit it
    5. TrustProduct      — the A2P compliance container
    6. BrandRegistration — goes to The Campaign Registry (minutes)
    7. MessagingService  — the sender pool the campaign attaches to
    8. Usa2p campaign    — goes to TCR (10-15 DAYS, see below)
    9. buy a number, attach it to the service (carrier approval, days)

⚠ **THE TIMELINE IS THE PRODUCT CONSTRAINT AND CANNOT BE ENGINEERED AWAY.**
Brand vetting returns in minutes. CAMPAIGN vetting is currently 10-15 days,
and each phone number then needs separate carrier approval taking several more
days. No amount of automation shortens that. The value of submitting at signup
is that the clock starts THEN rather than whenever somebody remembers — and
that the customer is told the real number instead of discovering it.

⚠ **Resumable, never restarted.** Every step is skipped when its SID is
already stored. A brand costs money and TCR rejects duplicates, so a retry
after a failed campaign must re-use the existing brand rather than create a
second one. This is why the pipeline reads its own row rather than taking the
whole payload as an argument.

⚠ **Rate limit: one request per second** for brand and campaign registration,
per Twilio's ISV guide. The steps here are sequential and awaited, which
satisfies it; do not "optimise" this into a gather.

Sources for the flow and its field requirements:
  https://www.twilio.com/docs/messaging/compliance/a2p-10dlc/onboarding-isv-api
  https://www.twilio.com/docs/trust-hub/trusthub-rest-api/api-create-secondary-customer-profile
  https://www.twilio.com/docs/messaging/api/brand-registration-resource
  https://www.twilio.com/docs/messaging/services/api/us-a2p-campaign-resource
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_TRUSTHUB_BASE = "https://trusthub.twilio.com/v1"
_MESSAGING_BASE = "https://messaging.twilio.com/v1"

#: Twilio's policy SID for a secondary customer profile. A constant of their
#: platform, not a per-account value.
SECONDARY_CUSTOMER_PROFILE_POLICY = "RNdfbf3fae0e1107f8aded0e7cead80bf5"
#: Policy SID for the A2P messaging trust product.
A2P_TRUST_PRODUCT_POLICY = "RNb0d4771c2c98518d916a3d4cd70a8f8b"


class A2PError(RuntimeError):
    """A step failed. Carries the carrier's own wording where there is one.

    ⚠ ``reason`` is stored and shown VERBATIM. A paraphrased rejection is
    unactionable — "address could not be validated" is the most common one and
    tells the customer exactly what to fix, while "registration failed" tells
    them nothing and generates a support ticket.
    """

    def __init__(self, message: str, *, step: str, reason: str | None = None) -> None:
        super().__init__(message)
        self.step = step
        self.reason = reason


# ── the status machine ────────────────────────────────────────────────

#: Ordered. Each value means "everything before it is done".
STATUSES = (
    "draft",
    "submitted",
    "brand_pending",
    "brand_approved",
    "campaign_pending",
    "approved",
)
TERMINAL = ("approved", "failed")

#: The ONE status that means this company's carriers have cleared it to send.
#:
#: ⚠ Read it, never re-type it. Three modules gate on this value — the sweep
#: decides whether to buy a number, the outbound gate decides whether a text
#: may leave, and the status endpoint decides what the customer is told. A
#: fourth literal that drifts by one character silences every white-label
#: customer at once, silently, with no failing test (gotcha 46: the rule lives
#: once and every layer reads it).
APPROVED = STATUSES[-1]


def is_terminal(status: str) -> bool:
    return status in TERMINAL


def next_status(current: str) -> str | None:
    """The status that follows ``current``, or None at the end / on failure."""
    if current in TERMINAL or current not in STATUSES:
        return None
    idx = STATUSES.index(current)
    return STATUSES[idx + 1] if idx + 1 < len(STATUSES) else None


#: Twilio's brand/campaign status vocabulary -> ours. Anything unrecognised is
#: deliberately NOT mapped: inventing a state for an unknown carrier response
#: is how a pending registration starts reading as approved.
_BRAND_STATUS = {
    "PENDING": "brand_pending",
    "IN_REVIEW": "brand_pending",
    "APPROVED": "brand_approved",
    "FAILED": "failed",
}
_CAMPAIGN_STATUS = {
    "PENDING": "campaign_pending",
    "IN_PROGRESS": "campaign_pending",
    "VERIFIED": "approved",
    "APPROVED": "approved",
    "FAILED": "failed",
}


def map_brand_status(raw: str | None) -> str | None:
    return _BRAND_STATUS.get((raw or "").strip().upper())


def map_campaign_status(raw: str | None) -> str | None:
    return _CAMPAIGN_STATUS.get((raw or "").strip().upper())


# ── what the customer has to give us ──────────────────────────────────


@dataclass(frozen=True)
class BusinessDetails:
    """Exactly the fields TCR requires, and nothing we do not need.

    ⚠ ``ein`` is OPTIONAL and its absence is MEANINGFUL. A business with no EIN
    registers as a Sole Proprietor brand; one with an EIN must register
    Standard / Low-Volume Standard. TCR rejects the wrong kind, so an empty
    string here is not the same as "not supplied" and must not be normalised
    into one. Most property managers have an EIN.
    """

    legal_business_name: str
    business_type: str
    business_vertical: str
    street: str
    city: str
    region: str
    postal_code: str
    rep_first_name: str
    rep_last_name: str
    rep_email: str
    rep_phone: str
    rep_job_position: str
    campaign_description: str
    opt_in_description: str
    sample_messages: list[str] = field(default_factory=list)
    ein: str | None = None
    website: str | None = None
    iso_country: str = "US"
    # Default mirrors a2p_defaults.USE_CASE; a test pins the two together so a
    # dataclass default cannot quietly re-introduce the expensive tier.
    #
    # ⚠ It is a LITERAL rather than the constant because importing it here
    # cycles: a2p_defaults -> comms.sms (for TEMPLATES) -> comms.a2p (this
    # module, for the status vocabulary). Duplication with a drift guard is the
    # deliberate trade — do not "tidy" it into an import.
    use_case: str = "LOW_VOLUME"


#: Fields that must be non-empty before we spend a submission. Deliberately a
#: DATA list rather than a chain of ifs: the API route, the form and this
#: module all have to agree on it, and three copies drift.
REQUIRED_FIELDS = (
    "legal_business_name",
    "business_type",
    "business_vertical",
    "street",
    "city",
    "region",
    "postal_code",
    "rep_first_name",
    "rep_last_name",
    "rep_email",
    "rep_phone",
    "rep_job_position",
    "campaign_description",
    "opt_in_description",
)

#: What the customer's OWN form calls each field.
#:
#: ⚠ `validate` reports to a person looking at that form, and the old code
#: built its sentences with `name.replace('_', ' ').capitalize()` — so the
#: screen read "Rep job position is required", "Region is required", "Postal
#: code is required" for fields labelled Job title, State and ZIP. Generating
#: English from an identifier is a tell in both directions: it reads as
#: machine output, and it names something the reader cannot find.
FIELD_LABELS = {
    "legal_business_name": "Legal business name",
    "business_type": "Business structure",
    "street": "Street address",
    "city": "City",
    "region": "State",
    "postal_code": "ZIP",
    "rep_first_name": "First name",
    "rep_last_name": "Last name",
    "rep_email": "Email",
    "rep_phone": "Mobile number",
    "rep_job_position": "Job title",
}

#: Required fields the SERVER fills (`comms/a2p_defaults`), not the customer.
#:
#: ⚠ Empty here is OUR defect and never their omission, so it must not be
#: reported as a field for them to go and fill — that is the dead-end
#: checklist item the Leasing gate already learned to delete. It still blocks,
#: because submitting an incomplete registration costs a fee and a fortnight.
_SERVER_FILLED = ("business_vertical", "campaign_description")

#: TCR rejects a campaign whose samples do not match its stated use case, and
#: an empty sample list fails outright.
#:
#: ⚠ TWO, NOT ONE, AND THE NUMBER IS THE USE CASE'S. Raised 2026-09-04 after
#: the first live `--tier mock` run: the carrier refused the campaign with
#:
#:     Number of Message Samples < 2 for usecase: UseCase(LOW_VOLUME)
#:
#: while `validate()` had returned no problems on the same fixture. So the
#: local check and the wire disagreed about what "enough samples" means, and
#: the local one was the permissive side — it would have waved a one-sample
#: submission through to a carrier that rejects it, AFTER the brand fee.
#:
#: This floor is tied to `a2p_defaults.USE_CASE` being LOW_VOLUME. Changing
#: that back to MIXED (or to any other tier) means re-deriving this number
#: against that use case's own minimum rather than assuming it carries over —
#: `test_a2p_registration.py` pins the pair so the two cannot drift apart in
#: silence again.
MIN_SAMPLE_MESSAGES = 2
MAX_SAMPLE_MESSAGES = 5
#: Carrier limit on a single sample.
MAX_SAMPLE_LENGTH = 1024


def validate(details: BusinessDetails) -> list[str]:
    """Problems that would make the submission fail, in the customer's words.

    ⚠ Validated BEFORE submitting, because a rejected brand is not free: it
    costs a registration fee and a 10-15 day wait to discover a typo. This is
    the cheap half of a check whose expensive half takes a fortnight.
    """
    problems: list[str] = []
    ours_incomplete = False
    for name in REQUIRED_FIELDS:
        if str(getattr(details, name, "") or "").strip():
            continue
        if name == "opt_in_description":
            # ⚠ The ONE server-filled field that maps back to something the
            # customer can act on: it renders empty exactly when the consent
            # box is unticked, so it is their checkbox and not our bug.
            problems.append("Confirm that your lease or application mentions text messages.")
        elif name in _SERVER_FILLED:
            ours_incomplete = True
        else:
            problems.append(f"{FIELD_LABELS[name]} is required.")

    samples = [s for s in details.sample_messages if s and s.strip()]
    if len(samples) < MIN_SAMPLE_MESSAGES:
        # Samples are rendered from the live message templates, so a missing
        # one is ours too — never an instruction to go and write examples.
        ours_incomplete = True
    if ours_incomplete:
        problems.append(
            "We haven't finished preparing your compliance wording. Save the form "
            "again, and tell us if this stays."
        )
    if len(samples) > MAX_SAMPLE_MESSAGES:
        problems.append(f"At most {MAX_SAMPLE_MESSAGES} sample messages.")
    for s in samples:
        if len(s) > MAX_SAMPLE_LENGTH:
            problems.append(f"A sample message is over {MAX_SAMPLE_LENGTH} characters.")
            break

    if details.ein and not any(ch.isdigit() for ch in details.ein):
        problems.append("EIN should be your federal tax ID number.")
    if details.website and not details.website.startswith(("http://", "https://")):
        problems.append("Website should start with http:// or https://.")
    return problems


#: The ONLY values the carrier accepts for ``job_position``.
#:
#: ⚠ ``business_title`` is free text; ``job_position`` is this enum, and they
#: were being sent the same value. The API does NOT expose the list — ``GET
#: /EndUserTypes/authorized_representative_1`` reports ``job_position`` as a
#: bare ``String`` — so these were measured one at a time against the policy
#: evaluation on 2026-09-04, which is the only oracle there is:
#:
#:     Director · GM · VP · CEO · CFO · General Counsel · Other   -> accepted
#:     Owner · Manager · President · General Manager              -> "Job Level is invalid."
#:
#: ⚠ **"Owner" was the form's own placeholder.** A customer typing exactly what
#: the field suggested produced a rejected registration. Six green harness runs
#: missed it because the fixture says "Director" — the one value that works —
#: the same way a fixture hid the ``use_case`` default.
JOB_POSITIONS = ("Director", "GM", "VP", "CEO", "CFO", "General Counsel", "Other")


def job_position(raw: str | None) -> str:
    """Coerce a free-text job title onto the carrier's enum.

    ⚠ Falls back to ``"Other"`` rather than failing. "Owner" is a true and
    ordinary answer that the registry simply has no slot for, and rejecting a
    registration over a job title — while `business_title` carries the exact
    words anyway — would be our vocabulary problem charged to the customer.
    Matching is case-insensitive because the enum's own casing ("GM", "CEO")
    is not what anyone types.
    """
    wanted = (raw or "").strip().casefold()
    for allowed in JOB_POSITIONS:
        if allowed.casefold() == wanted:
            return allowed
    return "Other"


def brand_kind(details: BusinessDetails) -> str:
    """Which brand TCR expects for this business.

    ⚠ This is not a preference. Registering a business that HAS an EIN as a
    Sole Proprietor is rejected, and so is the reverse. The single question
    "do you have an EIN" decides it, which is why the form asks it plainly
    rather than asking the customer to choose a brand type they have never
    heard of.
    """
    return "STANDARD" if (details.ein or "").strip() else "SOLE_PROPRIETOR"


#: TCR brand kind -> the A2P messaging profile's ``company_type``.
#:
#: ⚠ Keyed on :func:`brand_kind`'s OUTPUT, deliberately, so there is exactly
#: one place that reads the business and decides what it is. Deriving this
#: from ``details.ein`` again would be a second answer to the same question,
#: and the two would drift the first time either changed (gotcha 28).
#: A ``KeyError`` here is correct: a new brand kind must state its company
#: type rather than inherit a plausible-looking default.
_COMPANY_TYPE_BY_BRAND_KIND = {
    # Both are privately held businesses as far as the registry is concerned.
    # The EIN distinction that separates them decides the BRAND, not this —
    # "public" is reserved for a listed company, which would also have to
    # supply stock_exchange and stock_ticker.
    "STANDARD": "private",
    "SOLE_PROPRIETOR": "private",
}


def company_type(details: BusinessDetails) -> str:
    """The A2P messaging profile's ``company_type``, derived from the brand.

    Required by the ``us_a2p_messaging_profile_information`` end user, whose
    absence made every A2P trust product noncompliant with the carrier's
    "The Company Type is invalid." until 2026-09-04.
    """
    return _COMPANY_TYPE_BY_BRAND_KIND[brand_kind(details)]


# ── request payloads ──────────────────────────────────────────────────
#
# Built as pure functions so the exact wire shape is unit-testable without a
# Twilio account. Getting a field name wrong here costs a rejected submission
# and a fortnight, so the shapes are pinned by tests rather than trusted.


def customer_profile_payload(details: BusinessDetails, *, email: str, callback: str) -> dict:
    return {
        "FriendlyName": details.legal_business_name,
        "Email": email,
        "PolicySid": SECONDARY_CUSTOMER_PROFILE_POLICY,
        # ⚠ Without a StatusCallback Twilio gives no `failure_reason` on
        # address validation -- the single most common rejection. The callback
        # is the ONLY way to learn why, so it is not optional here even though
        # the API treats it as such.
        "StatusCallback": callback,
    }


def business_end_user_payload(details: BusinessDetails) -> dict:
    attrs: dict[str, Any] = {
        "business_name": details.legal_business_name,
        "business_type": details.business_type,
        "business_industry": details.business_vertical,
        "business_registration_identifier": "EIN" if details.ein else "None",
        "business_identity": "direct_customer",
        "business_regions_of_operation": "USA_AND_CANADA",
    }
    if details.ein:
        attrs["business_registration_number"] = details.ein
    if details.website:
        attrs["website_url"] = details.website
    return {
        "FriendlyName": f"{details.legal_business_name} business info",
        "Type": "customer_profile_business_information",
        "Attributes": attrs,
    }


def a2p_messaging_profile_payload(details: BusinessDetails) -> dict:
    """The third end user, and the one this module went without entirely.

    ⚠ **It attaches to the TRUST PRODUCT bundle, not the customer profile.**
    Confirmed from the evaluation output rather than from resource naming: it
    is the A2P trust product's evaluation that reports
    ``us_a2p_messaging_profile_information`` / "US A2P Messaging Profile
    General Business Information" as its failing requirement.

    Fields are the live policy's, read from the API on 2026-09-04 rather than
    guessed — ``GET /EndUserTypes/us_a2p_messaging_profile_information`` lists
    exactly ``company_type``, ``stock_exchange``, ``stock_ticker`` and
    ``brand_contact_email``. The two stock fields are for a LISTED company and
    are omitted here; the evaluation passes them absent.
    """
    return {
        "FriendlyName": f"{details.legal_business_name} A2P profile",
        "Type": "us_a2p_messaging_profile_information",
        "Attributes": {
            "company_type": company_type(details),
            "brand_contact_email": details.rep_email,
        },
    }


def representative_end_user_payload(details: BusinessDetails) -> dict:
    return {
        "FriendlyName": f"{details.rep_first_name} {details.rep_last_name}",
        "Type": "authorized_representative_1",
        "Attributes": {
            "first_name": details.rep_first_name,
            "last_name": details.rep_last_name,
            "email": details.rep_email,
            "phone_number": details.rep_phone,
            # ⚠ DIFFERENT FIELDS with different rules: `business_title` is
            # free text the carrier does not validate; `job_position` is an
            # enum it rejects on ("Job Level is invalid.").
            # ⚠ They now usually carry the SAME string, because the form is a
            # <select> over `JOB_POSITIONS` — so the coercion is a no-op for
            # six of the seven options. It still has to exist: the API also
            # takes a stored value from before the select shipped, and any
            # future free-text caller. The one visible difference is that an
            # owner's record reads `business_title: "Other"`; cosmetic, on a
            # field nothing validates, and NOT worth guessing "Owner" back
            # into (gotcha 55 — decline rather than invent someone's title).
            "business_title": details.rep_job_position,
            "job_position": job_position(details.rep_job_position),
        },
    }


def address_payload(details: BusinessDetails) -> dict:
    return {
        "CustomerName": details.legal_business_name,
        "Street": details.street,
        "City": details.city,
        "Region": details.region,
        "PostalCode": details.postal_code,
        "IsoCountry": details.iso_country,
    }


def campaign_payload(
    details: BusinessDetails,
    *,
    brand_sid: str,
    privacy_policy_url: str,
    terms_url: str,
) -> dict:
    """The campaign registration body.

    ⚠ **Both policy URLs are REQUIRED and this refuses without them.** Since
    2026-06-30 Twilio hard-rejects a campaign carrying neither (error 30933)
    and FETCHES both as part of the check. Refusing here rather than sending a
    body we know will 400 is the difference between a caught mistake and a
    rejection discovered AFTER the brand fee has been charged — the brand is
    registered several steps earlier, so a campaign failure is the expensive
    end of the pipeline (see ``comms/trust_pages`` for what they point at).
    """
    samples = [s.strip() for s in details.sample_messages if s and s.strip()]
    if not (privacy_policy_url.strip() and terms_url.strip()):
        raise A2PError(
            "campaign needs a privacy policy URL and a terms URL",
            step="campaign",
            reason=(
                "Carriers reject a campaign without both, and Twilio fetches them — "
                "so a public webhook base URL must be configured before registering."
            ),
        )
    return {
        "BrandRegistrationSid": brand_sid,
        "Description": details.campaign_description,
        "MessageFlow": details.opt_in_description,
        "UsAppToPersonUsecase": details.use_case,
        # Every campaign we submit is transactional property-management
        # messaging: maintenance updates, rent reminders, lease follow-ups.
        # Declaring otherwise is what gets a campaign rejected.
        "HasEmbeddedLinks": True,
        "HasEmbeddedPhone": True,
        "MessageSamples": samples,
        "PrivacyPolicyUrl": privacy_policy_url.strip(),
        "TermsAndConditionsUrl": terms_url.strip(),
    }


__all__ = [
    "A2P_TRUST_PRODUCT_POLICY",
    "APPROVED",
    "MAX_SAMPLE_MESSAGES",
    "JOB_POSITIONS",
    "a2p_messaging_profile_payload",
    "company_type",
    "job_position",
    "MIN_SAMPLE_MESSAGES",
    "REQUIRED_FIELDS",
    "SECONDARY_CUSTOMER_PROFILE_POLICY",
    "STATUSES",
    "TERMINAL",
    "A2PError",
    "BusinessDetails",
    "address_payload",
    "brand_kind",
    "business_end_user_payload",
    "campaign_payload",
    "customer_profile_payload",
    "is_terminal",
    "map_brand_status",
    "map_campaign_status",
    "next_status",
    "representative_end_user_payload",
    "validate",
]
