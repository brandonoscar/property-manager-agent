"""The campaign fields the customer must NOT be asked to write.

Three of the registration form's fields were free text, and they are the three
carriers reject over — the form's own hint said so: *"Reviewers read this
closely — it is the most common reason a registration is rejected."* Asking a
property manager to author an opt-in description is asking them to guess at a
carrier convention they have no reason to know, and a wrong guess costs the
campaign vetting fee plus a fortnight.

So the answers are generated here instead. What makes that honest rather than
convenient is that **the code enforces every claim they make**:

* the campaign description says we send four kinds of transactional message,
  and ``comms/sms.py`` is templates-only with "deliberately NO free-form
  message path anywhere in this module";
* the sample messages ARE those templates, rendered — not a paraphrase of
  them, so they cannot drift from what is actually sent (gotcha 25);
* the opt-in text says consent is recorded before any message and that STOP
  ends them, and ``send_sms`` fails closed on the consent ledger while
  ``comms/consent_keywords`` mirrors every STOP.

⚠ **The one clause we cannot generate is the one only they know.** "Residents
provide their number on the lease/application and are told they may receive
texts" is a fact about THEIR paperwork, before Occupella touches anything. We
can neither enforce nor verify it, so it is a CHECKBOX they tick rather than a
sentence we write on their behalf — filing that as a default would attest to
the carrier registry, in their name, that a document we have never read says
something. The checkbox is impossible to word badly and still a real
attestation.

⚠ **`business_vertical` is fixed, and the reason is not "it is ours".** Every
business that buys Occupella is a property management company, so REAL_ESTATE
is true of the entire customer base by construction. A customer editing it can
only make it wrong.
"""

from __future__ import annotations

from helixis.comms.sms import TEMPLATES

#: Every Occupella customer is a property manager. Not a preference.
BUSINESS_VERTICAL = "REAL_ESTATE"

#: Transactional property-management messaging.
#:
#: ⚠ `LOW_VOLUME` rather than `MIXED` — a PRICE decision, not a content one.
#: Both cover the four registered shapes without claiming a marketing campaign
#: we do not run. The difference is what TCR charges per campaign, and under
#: white-label that is a fee per CUSTOMER, every month, forever:
#:
#:     MIXED (Standard)        $46 one-time + $10/month
#:     LOW_VOLUME              $4.50 one-time + $1.50/month
#:
#: At 50 customers that is $425/month and $2,075 of one-time fees for a
#: throughput ceiling no property manager reaches — the low-volume tier caps
#: at a few messages per second and up to 5 campaigns, and this product sends
#: templated notices, not blasts. Per-customer costs are the ones that do NOT
#: amortise as you grow, which is what makes the gap worth closing before the
#: first real registration rather than after.
#:
#: ⚠ THE ENUM VALUE IS UNPROBED. twilio.com is egress-blocked from the dev
#: container (gotcha 75) and this came from the founder's research spec, not a
#: live call — the same shape as gotcha 79, where an SDK class name read as a
#: REST path and 404'd the entire chain. A wrong value here fails at the
#: CAMPAIGN, which is step 8, AFTER the brand fee at step 6 (gotcha 77).
#: `scripts/a2p_mock_run.py --tier mock` reaches the campaign for free and is
#: what settles it. Run it before the first paying registration.
USE_CASE = "LOW_VOLUME"

CAMPAIGN_DESCRIPTION = (
    "Property management notifications sent to residents, applicants and "
    "vendors who have provided their mobile number and consented: maintenance "
    "requests and scheduling, rent reminders, appointment confirmations, and "
    "account notices about their property. Recipients reply STOP to opt out at "
    "any time."
)

#: The half of the opt-in description the software guarantees. The customer's
#: own consent basis is prepended from their attestation — see ``opt_in_text``.
_OPT_IN_ENFORCED = (
    "Consent is recorded in the property manager's Occupella account before any "
    "message is sent, and no message can be sent to a number without a consent "
    "record on file. Every message identifies the sender and includes opt-out "
    "instructions; replying STOP ends messages immediately and is honoured "
    "across every property that manager operates. Opt-in is never a condition "
    "of renting."
)

#: What the customer ticks. Written as the FACT they are confirming rather than
#: as an instruction, so the checkbox label and the registered text say the
#: same thing — a carrier reading one and a customer reading the other must not
#: come away with different beliefs.
CONSENT_ATTESTATION = (
    "Our lease, rental application, or vendor agreement collects mobile "
    "numbers and tells people they may receive text messages about their "
    "property."
)

_OPT_IN_CUSTOMER_BASIS = (
    "Residents, applicants and vendors provide their mobile number on the "
    "property manager's lease, rental application, or vendor agreement, which "
    "states that they may receive text messages about their property."
)


def opt_in_text(*, attested: bool) -> str:
    """The MessageFlow we register, or ``""`` when the customer has not
    attested.

    ⚠ Returning empty is deliberate and load-bearing: without the consent basis
    there is no lawful campaign to register, and submitting the enforced half
    alone would describe a program with no opt-in — which is error 30909 and,
    worse, a claim we cannot stand behind. ``a2p.validate`` treats the empty
    string as a missing required field, so the form blocks instead.
    """
    if not attested:
        return ""
    return f"{_OPT_IN_CUSTOMER_BASIS} {_OPT_IN_ENFORCED}"


#: Placeholder values used only to render a registered template into a readable
#: EXAMPLE. Never sent — `send_sms` renders with the real record's values.
_SAMPLE_PARAMS: dict[str, str] = {
    "first_name": "Alex",
    "amount": "$1,450",
    "property_name": "200 E Colfax Ave",
    "due_date": "March 1",
    "status_update": "A technician is scheduled for Tuesday 9am-12pm.",
    "purpose": "unit showing",
    "date_time": "Saturday at 2:00 PM",
    "notice": "Your lease renewal offer is ready to review.",
}

#: Which templates to register, and in what order. The two most representative
#: lead, because carriers read the first samples most closely.
SAMPLE_TEMPLATE_IDS = ("maintenance_update", "rent_reminder", "appointment_confirmation")


def sample_messages(business_name: str = "") -> list[str]:
    """Example messages, RENDERED FROM THE LIVE TEMPLATES.

    ⚠ Derived, never a stored copy. Carriers check that sent messages match the
    registered samples, so a hand-written sample that drifts from
    ``sms.TEMPLATES`` is a campaign violation discovered by a carrier rather
    than by us. Generating them means the two cannot disagree (gotcha 25).

    ``business_name`` replaces the template's sender prefix so the samples name
    the company whose brand is being registered — the same reason the trust
    pages are per-customer.
    """
    out: list[str] = []
    for template_id in SAMPLE_TEMPLATE_IDS:
        template = TEMPLATES[template_id]
        params = {k: v for k, v in _SAMPLE_PARAMS.items() if "{" + k + "}" in template}
        rendered = template.format(**params)
        if business_name.strip():
            rendered = rendered.replace("Occupella:", f"{business_name.strip()}:", 1)
        out.append(rendered)
    return out


__all__ = [
    "BUSINESS_VERTICAL",
    "CAMPAIGN_DESCRIPTION",
    "CONSENT_ATTESTATION",
    "SAMPLE_TEMPLATE_IDS",
    "USE_CASE",
    "opt_in_text",
    "sample_messages",
]
