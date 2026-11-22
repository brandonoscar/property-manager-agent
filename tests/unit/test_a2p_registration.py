"""A2P registration: the parts that must be right before a fortnight is spent.

Campaign vetting takes 10-15 days. A field name typo, a wrong brand type or a
missing sample is therefore not a bug you find in testing — it is a bug you
find two weeks later, having charged the customer a registration fee and told
them Leasing would be on. That asymmetry is why the payload shapes and the
validation are pinned here rather than trusted to review.

⚠ These cannot talk to the carrier, and pretending otherwise would be worse
than not testing. What they DO cover is everything that is decidable locally:
the required-field set, the brand-type rule, the sample-message bounds, the
status vocabulary, and the exact wire shape of each request body. What they
cannot cover — that the carrier accepts these names — needs one real
submission against a sandbox ISV account.
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from helixis.comms import a2p, a2p_defaults
from helixis.comms.a2p import (
    MAX_SAMPLE_MESSAGES,
    MIN_SAMPLE_MESSAGES,
    REQUIRED_FIELDS,
    SECONDARY_CUSTOMER_PROFILE_POLICY,
    STATUSES,
    BusinessDetails,
    address_payload,
    brand_kind,
    business_end_user_payload,
    campaign_payload,
    customer_profile_payload,
    is_terminal,
    map_brand_status,
    map_campaign_status,
    next_status,
    representative_end_user_payload,
    validate,
)


def _details(**over) -> BusinessDetails:
    base = dict(
        legal_business_name="Blue Ridge Properties LLC",
        business_type="LLC",
        business_vertical="REAL_ESTATE",
        street="200 E Colfax Ave",
        city="Denver",
        region="CO",
        postal_code="80203",
        rep_first_name="Brandon",
        rep_last_name="Oscar",
        rep_email="b@example.com",
        rep_phone="+13035550100",
        rep_job_position="Owner",
        campaign_description="Maintenance updates and rent reminders to residents.",
        opt_in_description="Residents opt in on the lease and in the resident portal.",
        # ⚠ TWO, and the count is the point. LOW_VOLUME requires >= 2 samples
        # and `MIN_SAMPLE_MESSAGES` says so since 2026-09-04; a one-sample
        # fixture would make every "a complete submission has no complaints"
        # assertion below fail for a reason unrelated to what it tests.
        sample_messages=[
            "Your work order is scheduled for Tuesday 9am. Reply STOP to opt out.",
            "Hi Alex, rent of $1,450 is due on March 1. Reply STOP to opt out, HELP for help.",
        ],
        ein="12-3456789",
    )
    base.update(over)
    return BusinessDetails(**base)


# ── the brand-type rule ───────────────────────────────────────────────


def test_a_business_with_an_ein_is_not_a_sole_proprietor() -> None:
    """⚠ Registering the wrong brand type is rejected by TCR, both ways round.
    The single question "do you have an EIN" decides it, which is why the form
    asks that instead of asking the customer to pick a brand type."""
    assert brand_kind(_details(ein="12-3456789")) == "STANDARD"


def test_a_business_without_an_ein_is_a_sole_proprietor() -> None:
    assert brand_kind(_details(ein=None)) == "SOLE_PROPRIETOR"


def test_a_blank_ein_is_treated_as_absent_not_as_a_value() -> None:
    """A form posts "" for an untouched field. Reading that as "has an EIN"
    would submit a Standard brand with an empty tax id — rejected, two weeks
    later."""
    assert brand_kind(_details(ein="   ")) == "SOLE_PROPRIETOR"


# ── validation, which is the cheap half of a fortnight-long check ─────


def test_a_complete_submission_has_no_complaints() -> None:
    assert validate(_details()) == []


@pytest.mark.parametrize("missing", REQUIRED_FIELDS)
def test_every_required_field_is_actually_enforced(missing: str) -> None:
    """⚠ Walks the REQUIRED_FIELDS list itself rather than a hand-written copy.
    A field added to the list but never checked would pass a hand-written test
    forever — gotcha 45's rule: measure the corpus, do not restate the rule."""
    problems = validate(_details(**{missing: ""}))
    assert problems, f"{missing} is in REQUIRED_FIELDS but an empty value passes validation"


def test_every_required_field_is_either_labelled_or_ours_to_fill() -> None:
    """No problem string may be built out of a Python identifier.

    ⚠ Reads the CORPUS, not the function (gotcha 45). `validate` used to
    render its messages with ``name.replace('_', ' ').capitalize()``, so the
    screen said "Rep job position is required" and "Region is required" for
    fields the form labels Job title and State — machine-shaped English
    naming something the reader cannot find. A test feeding hand-written
    input to the formatter would have passed throughout; what has to be
    measured is that every field actually in REQUIRED_FIELDS is accounted
    for, so adding a fifteenth cannot silently reopen it.
    """
    from helixis.comms.a2p import _SERVER_FILLED, FIELD_LABELS

    for name in REQUIRED_FIELDS:
        assert name in FIELD_LABELS or name in _SERVER_FILLED or name == "opt_in_description", (
            f"{name!r} has no customer-facing label, so validate() would name the "
            f"identifier. Add it to FIELD_LABELS, or to _SERVER_FILLED if we fill it."
        )

    # And the other direction: a label for a field nobody requires is dead
    # weight that reads as coverage (gotcha 63's stale-exemption rule).
    for name in FIELD_LABELS:
        assert name in REQUIRED_FIELDS, f"{name!r} is labelled but not required"


def test_a_blank_field_is_reported_by_its_label_and_not_its_identifier() -> None:
    """Assert on the OUTPUT, not on the map.

    ⚠ The completeness test above passes against the ORIGINAL bug — verified
    by mutation. It proves FIELD_LABELS covers every required field and is
    blind to whether `validate` reads it, so restoring
    ``name.replace('_', ' ').capitalize()`` left the whole suite green. A map
    nothing consults is the `comms_triage` failure in miniature (gotcha 62).

    Note `city` and `legal_business_name` cannot distinguish the two
    implementations — capitalising the identifier happens to produce the
    label. `rep_job_position` (Job title), `region` (State) and
    `postal_code` (ZIP) are the rows that actually separate them.
    """
    from helixis.comms.a2p import FIELD_LABELS

    for name, label in FIELD_LABELS.items():
        problems = validate(_details(**{name: ""}))
        assert f"{label} is required." in problems, (
            f"blanking {name!r} did not report {label!r} — got {problems}"
        )


def test_a_missing_server_filled_field_is_never_the_customers_to_fix() -> None:
    """Our defect must not arrive as their to-do item.

    `business_vertical` and `campaign_description` are filled from
    `comms/a2p_defaults` and have no field on the form. Reporting them by
    name is the dead-end checklist item the Leasing gate already deleted once
    — an instruction nobody can carry out.
    """
    problems = validate(_details(campaign_description="", business_vertical=""))
    assert problems, "an unfillable registration must still be blocked"
    assert not any("campaign description" in p.lower() for p in problems)
    assert not any("business vertical" in p.lower() for p in problems)
    # One sentence, not one per field: two identical lines read as two faults.
    ours = [p for p in problems if "compliance wording" in p]
    assert len(ours) == 1, ours


def test_an_unticked_consent_box_reads_as_the_checkbox_it_is() -> None:
    """`opt_in_description` is server-filled too, but it renders empty exactly
    when the consent box is unticked — so it maps back to something the
    customer CAN do, and says so."""
    problems = validate(_details(opt_in_description=""))
    assert any("lease or application" in p for p in problems), problems


def test_a_campaign_with_no_sample_messages_is_refused() -> None:
    """Carriers reject a campaign with no examples of what will be sent. Better
    to say so in the form than to find out after the review queue.

    ⚠ Still blocked, but no longer phrased AT the customer. Samples are
    rendered from the live message templates now, so a missing one is our
    defect and "at least one sample message is required" sent the reader
    hunting for a field the form does not have.
    """
    problems = validate(_details(sample_messages=[]))
    assert problems
    assert not any("sample message" in p.lower() for p in problems), (
        "asks the customer to supply something only the server can produce"
    )


def test_whitespace_only_samples_do_not_count_as_samples() -> None:
    assert validate(_details(sample_messages=["   ", ""]))


def test_too_many_samples_is_refused() -> None:
    problems = validate(_details(sample_messages=["hello"] * (MAX_SAMPLE_MESSAGES + 1)))
    assert any("At most" in p for p in problems)


def test_a_website_without_a_scheme_is_flagged() -> None:
    assert any("http" in p for p in validate(_details(website="occupella.com")))
    assert validate(_details(website="https://occupella.com")) == []


# ── the status machine ────────────────────────────────────────────────


def test_an_unknown_carrier_status_maps_to_nothing() -> None:
    """⚠ The most important assertion in this file. Twilio's vocabulary can
    gain a value; guessing what an unrecognised one means is how a pending
    registration starts reading as approved and a customer is told they can
    text when they cannot. Unmapped must stay unmapped so the caller leaves
    the row where it is."""
    assert map_brand_status("SOMETHING_NEW") is None
    assert map_campaign_status("SOMETHING_NEW") is None
    assert map_brand_status(None) is None
    assert map_brand_status("") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("APPROVED", "brand_approved"),
        ("approved", "brand_approved"),
        ("  Approved  ", "brand_approved"),
        ("PENDING", "brand_pending"),
        ("FAILED", "failed"),
    ],
)
def test_brand_status_mapping_is_case_and_space_insensitive(raw: str, expected: str) -> None:
    """Carrier payloads are not consistently cased, and a status that fails to
    map leaves a finished registration stuck pending forever."""
    assert map_brand_status(raw) == expected


def test_campaign_verified_and_approved_both_mean_done() -> None:
    """Twilio uses VERIFIED for campaigns where the brand flow says APPROVED.
    Handling only one leaves a live campaign reading as pending."""
    assert map_campaign_status("VERIFIED") == "approved"
    assert map_campaign_status("APPROVED") == "approved"


def test_the_status_walk_terminates() -> None:
    seen, cur = [], "draft"
    while cur:
        seen.append(cur)
        assert len(seen) <= len(STATUSES), "next_status cycles"
        cur = next_status(cur)
    assert seen[-1] == "approved"


def test_terminal_states_go_nowhere() -> None:
    assert next_status("approved") is None
    assert next_status("failed") is None
    assert is_terminal("approved") and is_terminal("failed")
    assert not is_terminal("campaign_pending")


# ── wire shapes ───────────────────────────────────────────────────────


def test_the_customer_profile_always_carries_a_status_callback() -> None:
    """⚠ Twilio treats StatusCallback as optional and it is NOT optional for
    us: without it there is no `failure_reason` on address validation, which
    is the most common rejection. Losing it turns "your suite number is
    missing" into "registration failed"."""
    payload = customer_profile_payload(_details(), email="b@example.com", callback="https://x/cb")
    assert payload["StatusCallback"] == "https://x/cb"
    assert payload["PolicySid"] == SECONDARY_CUSTOMER_PROFILE_POLICY


def test_the_business_end_user_declares_ein_only_when_there_is_one() -> None:
    with_ein = business_end_user_payload(_details(ein="12-3456789"))["Attributes"]
    assert with_ein["business_registration_identifier"] == "EIN"
    assert with_ein["business_registration_number"] == "12-3456789"

    without = business_end_user_payload(_details(ein=None))["Attributes"]
    assert without["business_registration_identifier"] == "None"
    # ⚠ Absent, not empty. A blank registration number with identifier "None"
    # is a different submission from one that omits the key.
    assert "business_registration_number" not in without


def test_the_representative_payload_carries_what_tcr_emails_for_2fa() -> None:
    """TCR emails the authorized representative to complete attestation. A
    missing or wrong address stalls the brand with no visible error."""
    attrs = representative_end_user_payload(_details())["Attributes"]
    for key in ("first_name", "last_name", "email", "phone_number", "job_position"):
        assert attrs.get(key), f"{key} missing from the representative payload"


def test_the_address_payload_uses_twilio_field_names() -> None:
    p = address_payload(_details())
    assert set(p) == {"CustomerName", "Street", "City", "Region", "PostalCode", "IsoCountry"}
    assert p["IsoCountry"] == "US"


def test_the_campaign_payload_sends_cleaned_samples_and_the_brand() -> None:
    # The two policy URLs are REQUIRED since 2026-06-30 (error 30933) and the
    # builder refuses without them — see test_trust_pages.py for that half.
    p = campaign_payload(
        _details(sample_messages=["  a sample  ", "", "   "]),
        brand_sid="BN123",
        privacy_policy_url="https://api.example.com/c/co/privacy",
        terms_url="https://api.example.com/c/co/terms",
    )
    assert p["BrandRegistrationSid"] == "BN123"
    assert p["MessageSamples"] == ["a sample"], "blank samples must be dropped, not sent"
    # ⚠ Re-pointed 2026-09-02, MIXED -> LOW_VOLUME. Sourced from the shared
    # constant rather than re-typed, so the tier lives in exactly one place
    # and this test cannot drift into pinning the expensive one.
    assert p["UsAppToPersonUsecase"] == a2p_defaults.USE_CASE


def test_no_payload_leaks_a_field_the_customer_never_gave_us() -> None:
    """A guard against a future edit inventing a plausible-looking value. Every
    string in the submitted bodies has to have come from the customer or be a
    documented constant."""
    d = _details()
    supplied = {str(v) for v in dataclasses.asdict(d).values() if isinstance(v, str)}
    supplied |= {s for s in d.sample_messages}
    constants = {
        # ⚠ The job-title enum, sourced from the constant rather than retyped.
        # `job_position` is coerced onto it (2026-09-04), so "Other" legitimately
        # appears in a payload without the customer having typed it — the one
        # documented case of a value this guard would otherwise call a leak.
        *a2p.JOB_POSITIONS,
        "EIN",
        "None",
        "direct_customer",
        "USA_AND_CANADA",
        "MIXED",
        SECONDARY_CUSTOMER_PROFILE_POLICY,
        "https://x/cb",
        "b@example.com",
        f"{d.legal_business_name} business info",
        f"{d.rep_first_name} {d.rep_last_name}",
        "customer_profile_business_information",
        "authorized_representative_1",
    }
    for payload in (
        customer_profile_payload(d, email="b@example.com", callback="https://x/cb"),
        business_end_user_payload(d),
        representative_end_user_payload(d),
        address_payload(d),
    ):
        for value in list(payload.values()) + list(payload.get("Attributes", {}).values()):
            if isinstance(value, str):
                assert value in supplied or value in constants, (
                    f"payload carries {value!r}, which is neither customer-supplied "
                    "nor a documented constant"
                )


# ── Low-Volume Standard (2026-09-02) ──────────────────────────────────────
#
# A2P campaign fees are per CUSTOMER per month under white-label, so they are
# the costs that do NOT amortise with growth. Standard is $46 + $10/mo;
# Low-Volume is $4.50 + $1.50/mo for a throughput ceiling no property manager
# reaches. At 50 customers that is $425/month.
#
# ⚠ Neither value below is live-probed — twilio.com is egress-blocked from the
# dev container (gotcha 75) and both came from the founder's research spec.
# These tests pin what we SEND, not that the carrier accepts it;
# `scripts/a2p_mock_run.py --tier mock` is what settles that, for free.


def test_the_campaign_asks_for_the_low_volume_tier():
    from helixis.comms import a2p_defaults

    assert a2p_defaults.USE_CASE == "LOW_VOLUME", (
        "The campaign use case drives a per-customer monthly fee. Changing it "
        "back to MIXED costs $8.50/customer/month — do that deliberately."
    )


def test_the_dataclass_default_matches_the_shared_constant():
    """⚠ Two places name the tier and only one is obvious.

    `BusinessDetails.use_case` has its own default, so a caller that builds
    the dataclass without passing one would silently register on whatever
    that default says — the expensive tier, invisibly, for that customer only.
    """
    from helixis.comms import a2p, a2p_defaults

    assert a2p.BusinessDetails.__dataclass_fields__["use_case"].default == (a2p_defaults.USE_CASE)


def test_the_brand_skips_paid_secondary_vetting():
    """Secondary vetting buys throughput this product does not send.

    Source-level because the value is a literal in the request form and a mock
    would happily accept any spelling (gotcha 79's lesson: a fake answers
    whatever it is handed).
    """
    import pathlib

    src = pathlib.Path("src/helixis/comms/a2p_pipeline.py").read_text()
    assert '"SkipAutomaticSecVet": "true"' in src, (
        "The brand registration must skip automatic secondary vetting — it is "
        "a paid add-on for throughput we do not use, charged per customer."
    )


# ── the WIRE, not the payload ─────────────────────────────────────────
#
# ⚠ EVERYTHING ABOVE THIS LINE TESTS A DICT, AND THE DEFECT THAT COST THE
# FIRST LIVE MOCK RUN LIVED BETWEEN THE DICT AND THE SOCKET.
#
# `campaign_payload` built `{"MessageSamples": [s1, s2, s3]}` perfectly, and
# `test_the_campaign_payload_sends_cleaned_samples_and_the_brand` asserted
# exactly that and passed — while `_post` JSON-stringified the list into ONE
# form field and the carrier counted one sample. Producer correct, consumer
# correct, the encoding between them wrong: gotcha 28's both-sides rule, where
# the second side is a transport rather than an enum.
#
# So these read the bytes httpx actually puts on the wire.

import json  # noqa: E402
import urllib.parse  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402,F811

from helixis.comms import a2p_pipeline  # noqa: E402


def _capture(form: dict) -> str:
    """The literal request body `_post` would send for `form`."""
    sent: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["body"] = request.read().decode()
        return httpx.Response(200, json={"sid": "XX"})

    creds = a2p_pipeline.A2PCredentials(
        account_sid="AC" + "0" * 32, auth_token="t", webhook_base_url="https://api.example.com"
    )

    async def go() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            await a2p_pipeline._post(
                client, "https://x.example.com/v1/Thing", form, creds, step="probe"
            )

    asyncio.run(go())
    return sent["body"]


def test_message_samples_reach_the_carrier_as_separate_fields() -> None:
    """The bug the carrier caught, pinned at the layer that had it.

    Twilio counts one sample per `MessageSamples` field. Three in a JSON array
    inside one field is one sample, which is what produced

        Number of Message Samples < 2 for usecase: UseCase(LOW_VOLUME)

    on the first live `--tier mock` run.
    """
    body = _capture({"MessageSamples": ["alpha", "bravo", "charlie"], "Description": "d"})
    assert body.count("MessageSamples=") == 3, (
        f"expected three repeated MessageSamples fields, got: {body}"
    )
    for word in ("alpha", "bravo", "charlie"):
        assert word in body, f"{word!r} never reached the wire: {body}"
    assert "%5B" not in body, (
        "a '[' is url-encoded as %5B — the samples were JSON-stringified into a "
        f"single field again: {body}"
    )


def test_attributes_still_reach_the_carrier_as_a_json_string() -> None:
    """The other half, and why the two branches must not be merged.

    Twilio wants `Attributes` as a JSON STRING. A fix that dropped the dict
    branch along with the list branch would break all three end-user payloads —
    a worse bug than the one being fixed, in a step that runs first.

    ⚠ PARSED AS JSON, not matched as a substring. The first cut of this test
    asserted ``"Attributes=%7B" in body`` and a mutation walked straight
    through it: ``%7B`` is ``{``, and a dict httpx encodes WITHOUT
    ``json.dumps`` also starts with ``{`` — as a Python repr. The two differ
    only in the quote character, ``%22`` against ``%27``:

        json.dumps  -> Attributes=%7B%22business_name%22%3A+%22Acme%22%7D
        raw dict    -> Attributes=%7B%27business_name%27%3A+%27Acme%27%7D

    Twilio rejects the second — single quotes are not JSON — so a guard that
    cannot tell them apart is not guarding the property it names.
    """
    body = _capture({"Attributes": {"business_name": "Acme", "business_type": "LLC"}})
    value = urllib.parse.parse_qs(body)["Attributes"][0]
    assert json.loads(value) == {"business_name": "Acme", "business_type": "LLC"}, (
        f"Attributes must be a JSON object string, got {value!r}"
    )


def test_the_sample_floor_matches_the_use_case_we_actually_register() -> None:
    """The local check and the carrier must agree on "enough samples".

    ⚠ Reads BOTH sides. `MIN_SAMPLE_MESSAGES` is the number `validate()`
    enforces before spending the brand fee; `USE_CASE` is what the carrier
    judges it against. They disagreed until 2026-09-04 — the validator said 1,
    LOW_VOLUME wants 2 — so a submission passed locally and was refused at
    step 8, after the money.
    """
    assert a2p_defaults.USE_CASE == "LOW_VOLUME", (
        "the use case changed. MIN_SAMPLE_MESSAGES was derived from LOW_VOLUME's "
        "own minimum of 2 — re-derive it against the new tier rather than "
        "assuming the old floor carries over, then update this assertion."
    )
    assert MIN_SAMPLE_MESSAGES >= 2, (
        "LOW_VOLUME refuses a campaign with fewer than 2 message samples "
        "(carrier's words: 'Number of Message Samples < 2 for usecase: "
        "UseCase(LOW_VOLUME)'). A lower floor here waves through a submission "
        "the carrier rejects after the brand fee is spent."
    )


def test_we_generate_enough_samples_to_clear_our_own_floor() -> None:
    """The generator and the floor, read together.

    Samples are DERIVED from the live SMS templates, so shrinking
    `SAMPLE_TEMPLATE_IDS` — a change that looks like tidying an unrelated
    list — would silently drop the submission under the carrier's minimum.
    """
    generated = a2p_defaults.sample_messages("Acme LLC")
    assert len(generated) >= MIN_SAMPLE_MESSAGES, (
        f"only {len(generated)} samples are generated but {MIN_SAMPLE_MESSAGES} "
        "are required — every registration would now fail validation"
    )
    assert len(generated) <= MAX_SAMPLE_MESSAGES


# ── the tier, and the number, both of which cost money when wrong ─────


def test_no_module_names_a_use_case_tier_except_the_defaults_module() -> None:
    """The tier is named in four places and two of them had drifted.

    ⚠ Reads the CORPUS, not a function (gotcha 45) — the failure mode here is
    "silently permits a MORE EXPENSIVE choice", which is exactly the direction
    nobody notices. On 2026-09-04 `a2p_pipeline._details_from_row` fell back to
    `"MIXED"` and migration 0084's column DEFAULT was `'MIXED'` — the $46 +
    $10/month tier, against LOW_VOLUME's $4.50 + $1.50. Under white-label those
    fees are per customer per month and never amortise.

    A hand-written test asserting `USE_CASE == "LOW_VOLUME"` would have passed
    throughout: the constant was always right, and two other places disagreed
    with it.
    """
    import pathlib

    root = pathlib.Path(a2p_defaults.__file__).parent
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "a2p_defaults.py":
            continue  # the one legitimate home for the literal
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]  # a comment may quote the wrong tier
            if '"MIXED"' in code or "'MIXED'" in code:
                offenders.append(f"{path.name}:{i}: {line.strip()}")
    assert not offenders, (
        "these name a use-case tier directly instead of sourcing it from "
        f"a2p_defaults.USE_CASE, so they can drift from the tier we actually "
        f"pay for: {offenders}"
    )


def test_the_migration_default_matches_the_tier_we_register() -> None:
    """The column DEFAULT and the constant, read together.

    ⚠ `migration_check.py` cannot see this: 0091 adds no table and no column,
    so the schema sweep reports green whether or not it was ever applied
    (gotchas 53/59). This is the only thing in CI that can notice the two
    disagreeing.
    """
    import pathlib

    migrations = pathlib.Path(__file__).resolve().parents[2] / "supabase" / "migrations"
    files = sorted(migrations.glob("*a2p_use_case*.sql"))
    assert files, (
        "the migration that moves the use_case column default off MIXED is "
        "missing — a fresh database would start every registration on the "
        "expensive tier"
    )
    sql = files[-1].read_text(encoding="utf-8")
    statements = " ".join(
        line.split("--", 1)[0] for line in sql.splitlines()
    )  # comments quote 'MIXED' to explain the fix (gotcha 59's scoping rule)
    wanted = f"set default '{a2p_defaults.USE_CASE}'".lower()
    assert wanted in statements.lower(), (
        f"the migration does not {wanted!r} on use_case — a fresh database "
        f"would start every registration on whatever 0084 declared: {statements}"
    )


def test_the_bought_number_is_percent_encoded_in_the_lookup() -> None:
    """A raw `+` in a query string decodes as a SPACE.

    `_number_sid` interpolated an E.164 number straight into the query, so the
    carrier searched for " 13035550100" and matched nothing — and the caller
    raises "bought number not found on the account" for a number we had just
    paid for. It fires immediately after a carrier approval, which is the one
    moment the product cannot afford a spurious failure.

    ⚠ Asserted on the URL the function BUILDS, not on the source text: a
    substring guard over the source would be defeated by `ruff format`
    wrapping the f-string (gotcha 40/49).
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["param"] = request.url.params.get("PhoneNumber", "")
        return httpx.Response(200, json={"incoming_phone_numbers": [{"sid": "PN" + "0" * 32}]})

    creds = a2p_pipeline.A2PCredentials(account_sid="AC" + "0" * 32, auth_token="t")

    async def go() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await a2p_pipeline._number_sid(client, creds, "+13035550100")

    asyncio.run(go())
    assert "%2B" in seen["url"], (
        f"the leading + must be percent-encoded or it decodes as a space: {seen['url']}"
    )
    assert seen["param"] == "+13035550100", (
        "the carrier must receive the E.164 number it was sent, not a "
        f"space-prefixed one: {seen['param']!r}"
    )
