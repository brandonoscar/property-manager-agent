"""The campaign fields the customer no longer writes.

Three free-text fields — campaign description, opt-in description, sample
messages — were the ones carriers reject over, and the form said so in its own
hint. They are generated now. What has to hold:

* the generated text is TRUE of the software (every claim it makes is enforced
  somewhere in this repo, not aspirational);
* the samples are DERIVED from the live templates, so registered and sent
  cannot drift (gotcha 25);
* the server fills them regardless of what a client sends — a field a caller
  can override is a field that can be wrong;
* and the ONE fact only the customer knows still comes from the customer.
"""

from __future__ import annotations

import re

import pytest

from helixis.comms import a2p, a2p_defaults
from helixis.comms.sms import TEMPLATES

BUSINESS = "Blue Ridge Properties LLC"


# ── the samples are the templates, not a copy of them ─────────────────


def test_every_sample_is_a_live_template_rendered() -> None:
    """⚠ Carriers check that sent messages MATCH the registered samples. A
    hand-written sample that drifts from ``sms.TEMPLATES`` is a campaign
    violation found by a carrier rather than by us.

    ⚠ Asserting only that the id is registered is NOT this check, and mutation
    F4 proved it: swapping the body for a hardcoded string left that version
    green. What has to hold is that the OUTPUT carries the template's own
    literal text — every fixed chunk between the placeholders.
    """
    samples = a2p_defaults.sample_messages(BUSINESS)
    assert len(samples) == len(a2p_defaults.SAMPLE_TEMPLATE_IDS)
    for template_id, sample in zip(a2p_defaults.SAMPLE_TEMPLATE_IDS, samples, strict=True):
        assert template_id in TEMPLATES, f"{template_id} is not a registered template"
        template = TEMPLATES[template_id]
        # Split on the placeholders; every literal chunk must survive into the
        # rendered sample, so the sample cannot be prose that merely resembles
        # the template.
        literals = [chunk for chunk in re.split(r"\{[a-z_]+\}", template) if len(chunk.strip()) > 8]
        assert literals, f"{template_id} has no literal text to anchor on"
        for chunk in literals:
            anchor = chunk.strip()
            # The sender prefix is deliberately rewritten to the customer.
            anchor = anchor.replace("Occupella:", "").strip()
            assert anchor in sample, (
                f"{template_id}'s sample dropped its own wording {anchor!r} — "
                "registered and sent messages must not be able to drift"
            )


def test_the_samples_render_with_no_placeholders_left() -> None:
    """A literal `{first_name}` in a registered sample is an instant rejection
    and reads to a reviewer as an unfinished application."""
    for sample in a2p_defaults.sample_messages(BUSINESS):
        assert "{" not in sample and "}" not in sample, sample


def test_every_sample_carries_the_sender_and_an_opt_out() -> None:
    """Both are carrier requirements on the sample itself, and both come from
    the template — so this also pins that the templates keep them."""
    for sample in a2p_defaults.sample_messages(BUSINESS):
        assert BUSINESS in sample, f"sample does not identify the sender: {sample}"
        assert "STOP" in sample, f"sample has no opt-out: {sample}"


def test_the_samples_name_the_customer_not_occupella() -> None:
    """Same rule as the trust pages: the brand being registered is theirs, and
    a sample signed by a company the reviewer is not looking at is a
    mismatch."""
    for sample in a2p_defaults.sample_messages(BUSINESS):
        assert "Occupella" not in sample


def test_without_a_business_name_the_samples_still_render() -> None:
    """A draft saves before the name is filled in; the samples must not crash
    or emit a dangling prefix."""
    samples = a2p_defaults.sample_messages("")
    assert samples and all(s.strip() for s in samples)


# ── the opt-in text, and the half only the customer can supply ────────


def test_no_attestation_yields_no_opt_in_text() -> None:
    """⚠ THE LOAD-BEARING ONE. Without the customer's consent basis there is no
    lawful campaign to register. Submitting the enforced half alone would
    describe a program with no opt-in — error 30909, and a claim we cannot
    stand behind in somebody else's name."""
    assert a2p_defaults.opt_in_text(attested=False) == ""


def test_an_empty_opt_in_blocks_the_submission() -> None:
    """Both sides (gotcha 28): the empty string only helps if ``validate``
    treats it as missing. Otherwise the refusal is decorative.

    ⚠ Pinned on the CHECKBOX, not on the field name. The message used to read
    "Opt in description is required" — an identifier the customer cannot find
    on the form, because this field is server-rendered and empty exactly when
    they left the consent box unticked. Blocking is the property; naming
    something they can act on is the point.
    """
    problems = a2p.validate(_details(opt_in_description=""))
    assert problems, "an unticked consent box must still block the submission"
    assert any("lease or application" in p for p in problems), problems
    assert not any("opt in description" in p.lower() for p in problems), problems


def test_an_attested_opt_in_states_both_halves() -> None:
    """The customer's paperwork AND the guarantees our code enforces — a
    reviewer needs the consent basis, not just our internal controls."""
    text = a2p_defaults.opt_in_text(attested=True)
    assert "lease" in text.lower() and "application" in text.lower()
    assert "stop" in text.lower()
    assert "consent is recorded" in text.lower()


def test_the_checkbox_label_and_the_registered_text_agree() -> None:
    """⚠ A customer reads the checkbox and a carrier reads the MessageFlow. If
    those describe different facts, one of them has been misled — and the
    customer is the one attesting."""
    label = a2p_defaults.CONSENT_ATTESTATION.lower()
    text = a2p_defaults.opt_in_text(attested=True).lower()
    for token in ("lease", "application", "number"):
        assert token in label and token in text, token


# ── the claims have to be true of the software ────────────────────────


def test_the_campaign_description_matches_what_the_code_can_send() -> None:
    """``send_sms`` is templates-only, so the description is true by
    construction — but only while the four templates stay these four. A fifth
    template that this text does not describe makes the registration
    inaccurate, which is the kind of drift nothing else here would catch."""
    description = a2p_defaults.CAMPAIGN_DESCRIPTION.lower()
    for topic in ("maintenance", "rent", "appointment", "account notice"):
        assert topic in description, topic
    assert set(TEMPLATES) == {
        "rent_reminder",
        "maintenance_update",
        "appointment_confirmation",
        "account_notice",
    }, "the template set changed — the registered campaign description must too"


def test_the_vertical_is_fixed_and_real_estate() -> None:
    """Not because it is ours — because every business that buys this product
    is a property manager. A customer editing it can only make it wrong."""
    assert a2p_defaults.BUSINESS_VERTICAL == "REAL_ESTATE"


def _details(**over: object) -> a2p.BusinessDetails:
    base: dict[str, object] = {
        "legal_business_name": BUSINESS,
        "business_type": "LLC",
        "business_vertical": a2p_defaults.BUSINESS_VERTICAL,
        "street": "200 E Colfax Ave",
        "city": "Denver",
        "region": "CO",
        "postal_code": "80203",
        "rep_first_name": "Aaron",
        "rep_last_name": "Tillman",
        "rep_email": "aaron@example.com",
        "rep_phone": "+13035550100",
        "rep_job_position": "Director",
        "campaign_description": a2p_defaults.CAMPAIGN_DESCRIPTION,
        "opt_in_description": a2p_defaults.opt_in_text(attested=True),
        "sample_messages": a2p_defaults.sample_messages(BUSINESS),
    }
    base.update(over)
    return a2p.BusinessDetails(**base)  # type: ignore[arg-type]


def test_the_generated_submission_validates() -> None:
    """The whole point: a customer who fills in only the facts about their own
    business has a complete, submittable registration."""
    assert a2p.validate(_details()) == []


@pytest.mark.parametrize("field", ["legal_business_name", "street", "rep_email"])
def test_the_fields_the_customer_still_owns_are_still_required(field: str) -> None:
    """Server-filling the compliance text must not accidentally make the
    business facts optional — those are what TCR cross-references, and a blank
    one is a rejection on the facts."""
    assert a2p.validate(_details(**{field: ""})) != []
