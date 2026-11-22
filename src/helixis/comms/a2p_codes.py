"""A2P 10DLC campaign rejection-code catalog + remediation decision logic.

Twilio's campaign rejection codes are a THREE-GENERATION system in 2026
(deep research, 2026-08-19; source: Twilio help article "A2P 10DLC Campaign
Registration Rejected", help.twilio.com/articles/15778026827291):

* LEGACY (pre-2026): the ~25-code 309xx set, split resubmittable vs
  appeal-only.
* MARCH 23, 2026 granular set (30910-30932, 30940-30964, 30971, 30972,
  30995): replaces broad codes with specific reasons; the Disallowed/SHAFT/
  High-Risk bands (30940-30964) are APPEAL-ONLY.
* SEPTEMBER 15, 2026 unified-policy set (30532-30567): shared across
  Toll-Free Verification and A2P; all resubmittable. Forward-looking at
  catalog time — entries carry generation="2026-09-15" so a pre-launch code
  sighting is detectable.

Operational rules encoded here (see ``next_step``):

* Resubmittable failures → EDIT-IN-PLACE (Console "Edit Campaign"; free,
  unlimited). NEVER delete/recreate for a field-level failure — that burns a
  new $15 vetting fee.
* Delete/recreate is required ONLY when the use_case itself is wrong, or the
  failure is actually at the BRAND (a brand can't be edited with a campaign
  attached).
* Appeal-only codes (Disallowed/SHAFT/High-Risk) must route to a HUMAN
  (Twilio Support appeal, subject "10DLC Campaign Appeal for (business)").
  Auto-resubmitting these wastes money and risks account flags.
* An unknown code ≥ 30500 means THIS CATALOG IS STALE — update it from the
  help article before acting (the research's explicit trigger).

This module is pure data + pure functions: no I/O, no Twilio client. Today
Occupella runs ONE manually-registered campaign, so the immediate consumer
is a human diagnosing a rejection with ``classify``/``next_step`` from a
REPL or script; the per-PMC ISV registration flow (future) branches on the
same catalog programmatically.

Fee constants (``FEES``) ride along so the pricing work starts from the
corrected 2025-08-01 numbers instead of the stale baseline.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Generation(StrEnum):
    LEGACY = "legacy"
    MARCH_2026 = "2026-03-23"
    SEPT_2026 = "2026-09-15"


class NextStep(StrEnum):
    EDIT_IN_PLACE = "edit_in_place"  # free, unlimited resubmissions
    APPEAL = "appeal"  # human appeal via Twilio Support — never auto-resubmit
    FIX_BRAND = "fix_brand"  # brand-level: delete campaign → fix brand → recreate
    UNKNOWN_CODE = "unknown_code"  # not in catalog — catalog may be stale


@dataclass(frozen=True)
class A2pCode:
    code: int
    generation: Generation
    category: str
    summary: str
    resubmittable: bool
    remediation: str


def _c(
    code: int,
    generation: Generation,
    category: str,
    summary: str,
    resubmittable: bool,
    remediation: str,
) -> A2pCode:
    return A2pCode(code, generation, category, summary, resubmittable, remediation)


_L = Generation.LEGACY
_M = Generation.MARCH_2026
_S = Generation.SEPT_2026

# ── LEGACY resubmittable ─────────────────────────────────────────────
_LEGACY_RESUBMITTABLE: list[A2pCode] = [
    _c(
        30881,
        _L,
        "brand",
        "Support email invalid or public-domain",
        True,
        "Use an email on the business's own domain.",
    ),
    _c(
        30886,
        _L,
        "description",
        "Campaign description unclear / mismatched",
        True,
        "Describe the real purpose; state ISV direct offering if applicable.",
    ),
    _c(
        30887,
        _L,
        "opt-out",
        "Opt-out workflow unclear or keywords missing",
        True,
        "Add opt-out keywords + acknowledgment message carrying the brand name.",
    ),
    _c(30888, _L, "age-gate", "Age gate missing", True, "Add a robust age gate."),
    _c(
        30889,
        _L,
        "samples",
        "Embedded-phone flag set but no phone in samples",
        True,
        "Align the flag with the samples.",
    ),
    _c(
        30890,
        _L,
        "help",
        "HELP reply lacks brand name/contact",
        True,
        "Add brand + phone/email to the HELP response.",
    ),
    _c(
        30891,
        _L,
        "website",
        "Website non-functioning / pre-launch not indicated",
        True,
        "Provide a working site or state pre-launch explicitly.",
    ),
    _c(
        30892,
        _L,
        "samples",
        "Public URL shortener or non-secure URL in samples",
        True,
        "Drop bit.ly-class shorteners; use a branded HTTPS domain.",
    ),
    _c(
        30893,
        _L,
        "samples",
        "Samples missing/unclear or mismatched to use case",
        True,
        "Accurate samples; bracket templated fields; include the business name.",
    ),
    _c(
        30894,
        _L,
        "brand",
        "Campaign not associated with the brand behind it",
        True,
        "Verify brand info; state ISV direct offering.",
    ),
    _c(
        30895,
        _L,
        "attributes",
        "Direct-lending content attribute missing",
        True,
        "Set the direct-lending flag or fix the description.",
    ),
    _c(
        30896,
        _L,
        "opt-in",
        "Opt-in insufficient or shared with third parties",
        True,
        "Meet CTIA opt-in; host screenshots if behind login; no third-party sharing.",
    ),
    _c(
        30903,
        _L,
        "brand",
        "Sole-prop brand doesn't meet Sole Prop criteria",
        True,
        "Re-register the brand as Standard.",
    ),
    _c(
        30907,
        _L,
        "website",
        "Website doesn't match campaign/brand",
        True,
        "Align the website with the brand + campaign.",
    ),
    _c(
        30908,
        _L,
        "privacy",
        "No compliant privacy policy found",
        True,
        "Link a compliant policy (incl. the no-third-party-sharing clause).",
    ),
    _c(
        30909,
        _L,
        "opt-in",
        "Call-to-action could not be verified",
        True,
        "Provide the complete CTA and every opt-in method.",
    ),
]

# ── LEGACY appeal-only ───────────────────────────────────────────────
_LEGACY_APPEAL: list[A2pCode] = [
    _c(30882, _L, "terms", "Terms & Conditions not met", False, "Appeal via Support."),
    _c(30883, _L, "shaft", "SHAFT content violation", False, "Appeal via Support."),
    _c(30884, _L, "spam", "Spam/phishing", False, "Appeal via Support."),
    _c(
        30885,
        _L,
        "high-risk",
        "High risk (fraud/deceptive; 3rd-party data sharing)",
        False,
        "Appeal via Support; fix the privacy policy's sharing language first.",
    ),
    _c(30897, _L, "disallowed", "Disallowed content category", False, "Appeal via Support."),
    _c(30898, _L, "brand", "Excessive EIN (max ~50 brands/EIN)", False, "Appeal via Support."),
]

# ── MARCH 23, 2026 — granular resubmittable ──────────────────────────
_MARCH_RESUBMITTABLE: list[A2pCode] = [
    _c(30910, _M, "use-case", "Non-English fields", True, "Submit fields in English."),
    _c(
        30911,
        _M,
        "use-case",
        "Copy-paste duplicate fields/samples",
        True,
        "Write distinct description/samples.",
    ),
    _c(30912, _M, "use-case", "P2P traffic, not A2P", True, "Reframe as A2P or don't register."),
    _c(
        30913,
        _M,
        "consent",
        "Marketing consent not separate from transactional",
        True,
        "Separate campaigns + separate explicit marketing consent.",
    ),
    _c(30914, _M, "identity", "Sole-prop content mismatches name", True, "Align identity."),
    _c(
        30915,
        _M,
        "identity",
        "Sole prop registered but LLC/Inc name used",
        True,
        "Register as Standard/LVS under the entity.",
    ),
    _c(30916, _M, "use-case", "Lead-gen vs lead-nurture mismatch", True, "Fix the use case."),
    _c(
        30917,
        _M,
        "consent",
        "Multiple opt-in methods not all described",
        True,
        "Describe every opt-in method in message_flow.",
    ),
    _c(30918, _M, "identity", "DBA doesn't match legal name", True, "Reconcile DBA/legal name."),
    _c(
        30919,
        _M,
        "website",
        "Insufficient business info / messaging disclosure on site",
        True,
        "Add business info + SMS program disclosure to the site.",
    ),
    _c(30920, _M, "website", "Site is just a form", True, "Ship a real site."),
    _c(
        30921,
        _M,
        "website",
        "Site requires login",
        True,
        "Host opt-in screenshots publicly for the reviewer.",
    ),
    _c(
        30922,
        _M,
        "website",
        "Site under construction / non-standard URL",
        True,
        "Finish the site or state pre-launch.",
    ),
    _c(
        30923,
        _M,
        "consent",
        "Consent bundled into mandatory terms",
        True,
        "Unbundle: separate OPTIONAL SMS checkbox.",
    ),
    _c(
        30924,
        _M,
        "consent",
        "Consent language missing required disclosures",
        True,
        "Add message type, frequency, 'message and data rates may apply', STOP.",
    ),
    _c(
        30925,
        _M,
        "consent",
        "Opt-in checkbox missing or pre-selected",
        True,
        "Separate UNCHECKED checkbox.",
    ),
    _c(
        30926,
        _M,
        "identity",
        "Campaign references multiple companies",
        True,
        "One company per campaign.",
    ),
    _c(
        30927,
        _M,
        "identity",
        "Opt-in evidence shows a different company",
        True,
        "Evidence must carry the registering company.",
    ),
    _c(
        30928,
        _M,
        "use-case",
        "Influencer/public-figure not a valid use case",
        True,
        "Pick a supported use case.",
    ),
    _c(
        30929,
        _M,
        "use-case",
        "Emergency alerts not permitted",
        True,
        "Remove emergency-alert framing.",
    ),
    _c(
        30930,
        _M,
        "brand",
        "Brand hit the 100-campaign limit",
        True,
        "Consolidate or use another brand.",
    ),
    _c(
        30931,
        _M,
        "consent",
        "Form mechanics prevent declining consent",
        True,
        "The SMS checkbox must be genuinely optional — service works without it.",
    ),
    _c(
        30932,
        _M,
        "consent",
        "Privacy policy shares opt-in data with third parties",
        True,
        "Add the no-third-party-sharing clause for mobile info/opt-in data.",
    ),
    _c(
        30971,
        _M,
        "identity",
        "Contact email on a personal domain",
        True,
        "Use the official business domain (tightened Sept 15).",
    ),
    _c(
        30972,
        _M,
        "identity",
        "Contact is not an authorized representative",
        True,
        "Use an authorized rep as contact.",
    ),
    _c(
        30995,
        _M,
        "registration",
        "Messaging Service phone-number limit reached",
        True,
        "Contact Twilio Support.",
    ),
]

# ── MARCH 23, 2026 — appeal-only bands ───────────────────────────────
_MARCH_APPEAL_SUMMARIES: dict[int, tuple[str, str]] = {
    30940: ("disallowed", "Cannabis/CBD"),
    30941: ("disallowed", "Prescription/controlled substances"),
    30942: ("disallowed", "Loan marketing"),
    30943: ("disallowed", "Third-party debt collection"),
    30944: ("disallowed", "Gambling"),
    30945: ("disallowed", "Sweepstakes"),
    30946: ("disallowed", "Stock alerts"),
    30947: ("disallowed", "Crypto"),
    30948: ("disallowed", "High-risk investment"),
    30949: ("disallowed", "Debt reduction"),
    30950: ("disallowed", "Credit repair"),
    30951: ("disallowed", "Third-party lead-gen / MLM"),
    30952: ("disallowed", "Not federally compliant"),
    30953: ("shaft", "Sex"),
    30954: ("shaft", "Hate/violence"),
    30955: ("shaft", "Alcohol"),
    30956: ("shaft", "Alcohol without 21+ age gate"),
    30957: ("shaft", "Firearms/fireworks/explosives"),
    30958: ("shaft", "Tobacco/vape"),
    30959: ("high-risk", "Fraudulent/misleading"),
    30960: ("high-risk", "Known phishing"),
    30961: ("high-risk", "High-risk domain reputation"),
    30962: ("high-risk", "Deceptive marketing"),
    30963: ("high-risk", "Public URL shorteners"),
    30964: ("high-risk", "HTTP (not HTTPS) URLs"),
}
_MARCH_APPEAL: list[A2pCode] = [
    _c(
        code,
        _M,
        cat,
        summary,
        False,
        "Appeal via Twilio Support ('10DLC Campaign Appeal for (business)') — never auto-resubmit.",
    )
    for code, (cat, summary) in _MARCH_APPEAL_SUMMARIES.items()
]

# ── SEPTEMBER 15, 2026 — unified policy (all resubmittable) ──────────
_SEPT_SUMMARIES: dict[int, tuple[str, str, str]] = {
    30532: ("use-case", "Personal/P2P use not supported", "Register a business use case."),
    30533: ("use-case", "Personal/P2P use not supported", "Register a business use case."),
    30534: ("use-case", "Testing use case not accepted", "Register the real use case."),
    30536: (
        "privacy",
        "Privacy policy URL missing/inaccessible",
        "Publish the policy at a public HTTPS URL.",
    ),
    30537: ("identity", "Business model mismatch", "Align campaign with the business model."),
    30539: (
        "opt-out",
        "Opt-out instructions/keywords missing or noncompliant",
        "Use the verbatim 'Reply STOP to cancel' phrasing.",
    ),
    30540: (
        "consent",
        "Double opt-in confirmation message required",
        "Confirmation must carry brand, program name, rates line, HELP + STOP.",
    ),
    30541: ("political", "Political: FEC registration required", "Provide FEC details."),
    30542: ("political", "Political: Campaign Verify token required", "Provide the token."),
    30543: ("consent", "Live opt-in link not provided", "Link the live opt-in flow."),
    30544: ("consent", "CTA must state purpose", "State what the messages are."),
    30545: ("consent", "CTA must state frequency", "State message frequency."),
    30547: ("consent", "CTA must state opt-out", "State how to opt out."),
    30549: (
        "consent",
        "No Terms-of-Service link in the opt-in flow",
        "Link the ToS adjacent to the opt-in.",
    ),
    30550: (
        "consent",
        "No privacy-policy link in the opt-in flow",
        "Link the privacy policy adjacent to the opt-in.",
    ),
    30551: ("consent", "Consent doesn't specify 'SMS'/'text'", "Name the channel explicitly."),
    30552: (
        "consent",
        "Opt-in not customer-facing (internal CRM checkbox rejected)",
        "Collect consent on a customer-facing surface.",
    ),
    30555: (
        "consent",
        "First message without documented prior consent",
        "Produce visual proof of the complete opt-in sequence.",
    ),
    30556: ("consent", "SMS consent bundled with other channels", "Unbundle per channel."),
    30557: ("terms", "ToS missing business/program name", "Add it."),
    30558: ("terms", "ToS missing message frequency", "Add it."),
    30559: ("terms", "ToS missing product description", "Add it."),
    30560: ("terms", "ToS missing customer-support contact", "Add it."),
    30561: ("terms", "ToS missing opt-out instructions", "Add it."),
    30562: ("terms", "ToS missing 'message and data rates may apply'", "Add it."),
    30563: ("terms", "ToS missing carrier-liability clause", "Add it."),
    30564: ("terms", "ToS/privacy links not near the opt-in", "Place links adjacent."),
    30565: ("disallowed", "Rx/GLP-1 content must be informational only", "Adjust content."),
    30566: ("disallowed", "Academic fraud", "Not registrable; appeal if misclassified."),
    30567: ("disallowed", "Phishing simulation", "Not registrable; appeal if misclassified."),
}
_SEPT: list[A2pCode] = [
    _c(code, _S, cat, summary, True, remediation)
    for code, (cat, summary, remediation) in _SEPT_SUMMARIES.items()
]

CATALOG: dict[int, A2pCode] = {
    e.code: e
    for e in (
        *_LEGACY_RESUBMITTABLE,
        *_LEGACY_APPEAL,
        *_MARCH_RESUBMITTABLE,
        *_MARCH_APPEAL,
        *_SEPT,
    )
}


def classify(code: int) -> A2pCode | None:
    """The catalog entry for a rejection code, or ``None`` when unknown."""
    return CATALOG.get(code)


def next_step(code: int, *, wrong_use_case: bool = False, brand_level: bool = False) -> NextStep:
    """The remediation route for a rejection.

    ``wrong_use_case``/``brand_level`` are the two operator-supplied facts the
    code alone can't tell you (the research's §1G decision tree): a wrong
    use_case can't be edited in place, and a brand problem requires
    delete-campaign → fix-brand → recreate. Everything else known and
    resubmittable is a FREE edit-in-place; appeal-only codes go to a human.
    """
    if brand_level:
        return NextStep.FIX_BRAND
    entry = classify(code)
    if entry is None:
        return NextStep.UNKNOWN_CODE
    if not entry.resubmittable:
        return NextStep.APPEAL
    if wrong_use_case:
        return NextStep.FIX_BRAND  # delete/recreate path — use_case has no edit
    return NextStep.EDIT_IN_PLACE


def catalog_is_stale_for(code: int) -> bool:
    """True when a code in Twilio's A2P/TFV numbering space isn't in this
    catalog — the research's explicit update trigger ("if you ever see a code
    ≥30532 you don't have, your mapping is stale")."""
    return 30500 <= code <= 31999 and code not in CATALOG


# ── Fee constants (verified 2026-08-19; reconcile against Twilio's live
#    pricing page before invoicing — figures move) ────────────────────
FEES: dict[str, float] = {
    # One-time, per brand (Twilio-billed figures).
    "brand_standard": 46.00,  # includes secondary vetting
    "brand_low_volume_standard": 4.50,
    "brand_sole_prop": 4.50,
    "secondary_vetting": 41.50,
    "authentication_plus": 12.50,  # mandatory 2FA email verification, 2025-08-07
    # Per campaign.
    "campaign_vetting_one_time": 15.00,  # burned again on delete/recreate
    "campaign_monthly_min": 1.50,
    "campaign_monthly_max": 10.00,
    # Per-message context (USD/segment, pass-through, volatile).
    "tmobile_mo_fee": 0.0025,  # 2026-01-19, US Cellular matched
}

# The LVS-default policy (research Stage 3): typical PMCs register as
# Low-Volume Standard (skips the $41.50 secondary vetting); flip to full
# Standard only when volume approaches the T-Mobile Basic daily cap.
LVS_DAILY_SEGMENT_FLIP_THRESHOLD = 2_000  # T-Mobile Basic-tier daily cap
