"""The A2P ISV pipeline — submission, polling and the number that follows.

``comms/a2p.py`` holds the pure half: the status machine, the field contract
and the wire payloads. This is the half that talks to the carrier side and
advances a row. Together they are the whole white-label flow: the customer
fills in a form and never opens a carrier account.

Four entry points, one per phase of a registration's life:

  * ``advance_submission``  the queued row -> profile, end users, address,
                            trust product, BRAND, messaging service
  * ``poll_status``         brand/campaign vetting outcomes -> our status
  * ``submit_campaign``     reached only once the BRAND is approved — a
                            campaign against an unapproved brand is rejected
  * ``ensure_number``       once approved, buy the number and attach it

⚠ **RESUMABLE, NEVER RESTARTED.** Every step is skipped when its SID is
already stored. A brand costs a fee and the registry rejects duplicates, so a
retry after a failed campaign must reuse the existing brand rather than create
a second one. That is why each step reads and writes the row rather than
taking the whole payload as an argument.

⚠ **ONE REQUEST PER SECOND** for brand and campaign registration, per the ISV
guide. The steps here are sequential and awaited, which satisfies it. Do NOT
"optimise" this into a gather — the rate limit is the reason it looks serial.

⚠ **THE PATHS BELOW ARE DOCUMENTED, NOT LIVE-PROBED.** This container has no
carrier account, so unlike the Buildium surface (where CLAUDE.md requires
grounding every field on a live row) these endpoint paths come from the ISV
guide the module docstring in ``a2p.py`` cites. The PAYLOADS are unit-pinned;
the paths are not. The failure mode is deliberately cheap and visible: a wrong
path fails the step, stores the carrier's verbatim words in ``failure_reason``,
and the customer sees a rejection they can act on — and nothing is charged
before ``BrandRegistration``, which is step 6. Treat the first live run as the
probe, and fix a path against the response rather than against a guess.

⚠ **MONEY LIVES IN EXACTLY TWO STEPS.** Brand registration carries a
registration fee; buying a number is a recurring monthly charge. Both are
guarded by a stored SID / an existing row, checked BEFORE the call. Everything
before step 6 is free, which is why the pipeline is safe to retry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from helixis.comms import a2p, a2p_defaults, trust_pages
from helixis.comms.phone import normalize_e164
from helixis.connectors import twilio_client

logger = logging.getLogger(__name__)

_TRUSTHUB = "https://trusthub.twilio.com/v1"
_MESSAGING = "https://messaging.twilio.com/v1"
_API = "https://api.twilio.com/2010-04-01"

_TIMEOUT_S = 30.0

#: Enforced pause before the two rate-limited calls. The ISV guide caps brand
#: and campaign registration at ONE REQUEST PER SECOND.
#:
#: ⚠ Awaiting each step in sequence does NOT satisfy that on its own — it
#: spaces calls by however long the network happens to take, which is usually
#: enough and is not a guarantee. A constant that documents a limit nothing
#: enforces is worse than no constant: it reads as a control (gotcha 48). So
#: this is actually slept, and only where the limit applies.
_RATE_LIMIT_S = 1.1

#: The form field that makes a BrandRegistration a MOCK one — free, unroutable,
#: 30-day expiry, and the only way to exercise this chain end to end without
#: spending per attempt.
#:
#: ⚠ **NOT LIVE-PROBED** (twilio.com is egress-blocked from the dev container —
#: gotcha 75), so this name is the single most likely thing in this module to
#: be wrong. That is exactly why the call site reads the response back instead
#: of trusting the 200: a wrong name here is ignored by the API and registers a
#: REAL brand at full fee. Confirm it on the first live run.
_MOCK_BRAND_PARAM = "Mock"

#: Response keys that would tell us a brand is mock. Checked as a SET because
#: the exact shape is unprobed; any one of them saying so is enough, and none
#: of them saying so is a refusal rather than an assumption.
_MOCK_BRAND_KEYS = ("mock", "is_mock")


def _is_mock_brand(body: dict[str, Any]) -> bool:
    """Whether the carrier's own response says this brand is a mock.

    ⚠ Deliberately does NOT default to True on an unrecognised shape. An
    unreadable answer means we cannot rule out a real registration, and
    "probably free" is not a thing to report about a fee.
    """
    for key in _MOCK_BRAND_KEYS:
        value = body.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
    return False


#: How long to leave a pending registration alone between polls. Campaign
#: vetting is 10-15 DAYS — polling hourly spends API calls on a status that
#: cannot have moved, and the customer is not watching a spinner either way.
POLL_BACKOFF = timedelta(hours=6)
#: A failed SUBMISSION retries sooner than a pending one is polled: a failure
#: is often ours (a wrong path, a transient 502) and a customer waiting on a
#: fixable error should not wait a quarter of a day to find out.
RETRY_BACKOFF = timedelta(minutes=30)


class A2PCredentials:
    """Deployment-level carrier credentials.

    ⚠ Deployment-global, and correctly so (gotcha 69): one messaging account
    serves every customer — that is what white-label MEANS here. Only the
    NUMBER is per-company.
    """

    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        webhook_base_url: str = "",
        primary_profile_sid: str = "",
    ) -> None:
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.webhook_base_url = (webhook_base_url or "").strip().rstrip("/")
        # ⚠ Deliberately NOT part of ``usable``. Missing credentials and a
        # missing primary profile are different failures with different fixes,
        # and folding them together would report "no credentials" at a caller
        # that has perfectly good ones.
        self.primary_profile_sid = (primary_profile_sid or "").strip()

    @property
    def auth(self) -> tuple[str, str]:
        return (self.account_sid, self.auth_token)

    @property
    def usable(self) -> bool:
        return bool(self.account_sid and self.auth_token)


async def _post(
    client: httpx.AsyncClient, url: str, form: dict[str, Any], creds: A2PCredentials, *, step: str
) -> dict[str, Any]:
    """One carrier POST, with the carrier's own words preserved on failure.

    ⚠ ``reason`` is stored and shown VERBATIM. "Address could not be
    validated" is the most common rejection and tells the customer exactly
    what to fix; "registration failed" tells them nothing and generates a
    support ticket.

    ⚠ A DICT IS JSON. A LIST IS A REPEATED FIELD. Do not merge these branches.

    This line read ``isinstance(v, dict | list)`` until the first live
    ``--tier mock`` run on 2026-09-04, and the carrier caught it:

        Number of Message Samples < 2 for usecase: UseCase(LOW_VOLUME)

    Three samples were generated correctly and one arrived, because
    ``json.dumps`` collapses them into a SINGLE form field holding a JSON
    array. Measured on httpx 0.28.1, which is what decides this:

        {"MessageSamples": ["a", "b", "c"]}
            -> MessageSamples=a&MessageSamples=b&MessageSamples=c   (wanted)
        {"MessageSamples": json.dumps(["a", "b", "c"])}
            -> MessageSamples=%5B%22a%22%2C+%22b%22%2C+%22c%22%5D   (one sample)

    ``Attributes`` is the only other shape here and it is a dict on all three
    of its call sites (``a2p.py`` 333/341, and the address assignment below);
    Twilio genuinely wants that one as a JSON STRING, which is why the dict
    branch stays. ``MessageSamples`` (``a2p.py:400``) is the only list-valued
    field in the module — checked across every payload builder, not assumed —
    so this change is contained to the campaign body.

    ⚠ The failure position is what made it expensive. The campaign is step 8;
    the brand fee is step 6. A defect here is discovered AFTER the money, which
    is the exact shape gotcha 77 records. It was free this time only because
    the brand was mock.
    """
    flat = {k: (json.dumps(v) if isinstance(v, dict) else v) for k, v in form.items()}
    try:
        resp = await client.post(url, data=flat, auth=creds.auth)
    except httpx.HTTPError as exc:
        raise a2p.A2PError(f"{step} request failed", step=step, reason=str(exc)) from exc
    if resp.status_code >= 300:
        raise a2p.A2PError(
            f"{step} returned {resp.status_code}",
            step=step,
            reason=_carrier_message(resp),
        )
    try:
        return dict(resp.json())
    except Exception as exc:  # noqa: BLE001
        raise a2p.A2PError(f"{step} returned unparseable JSON", step=step) from exc


async def _get(
    client: httpx.AsyncClient, url: str, creds: A2PCredentials, *, step: str
) -> dict[str, Any]:
    try:
        resp = await client.get(url, auth=creds.auth)
    except httpx.HTTPError as exc:
        raise a2p.A2PError(f"{step} request failed", step=step, reason=str(exc)) from exc
    if resp.status_code >= 300:
        raise a2p.A2PError(
            f"{step} returned {resp.status_code}", step=step, reason=_carrier_message(resp)
        )
    try:
        return dict(resp.json())
    except Exception as exc:  # noqa: BLE001
        raise a2p.A2PError(f"{step} returned unparseable JSON", step=step) from exc


def _carrier_message(resp: httpx.Response) -> str:
    """The carrier's own message, never our paraphrase and never the token.

    Falls back to a truncated body: an unparseable error is still more
    actionable than a status code alone, and 300 characters is enough for a
    validation message without pasting an HTML error page into a customer's
    screen.
    """
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return resp.text[:300]
    if isinstance(body, dict):
        for key in ("message", "detail", "error_message"):
            if body.get(key):
                return str(body[key])[:500]
    return resp.text[:300]


# ── evaluation: the dry run that must happen BEFORE the submission ────


def _evaluation_failures(body: dict[str, Any]) -> str:
    """Every failed requirement, in the carrier's own words.

    ⚠ VERBATIM, the same rule ``_post`` applies to ``reason``. "The status of
    the Primary Customer Profile must be in an approved state" names the fix;
    "bundle invalid" starts a support ticket. Requirement-level and field-level
    messages are BOTH kept because they answer different questions — which
    requirement failed, and which field inside it did.
    """
    out: list[str] = []
    for req in body.get("results") or []:
        if not isinstance(req, dict) or req.get("passed"):
            continue
        name = str(req.get("friendly_name") or req.get("requirement_name") or "requirement")
        before = len(out)
        if req.get("failure_reason"):
            out.append(f"{name}: {req['failure_reason']}")
        for item in req.get("fields") or []:
            if not isinstance(item, dict) or item.get("passed") or not item.get("failure_reason"):
                continue
            label = item.get("friendly_name") or item.get("object_field") or "field"
            out.append(f"{name} -> {label}: {item['failure_reason']}")
        if len(out) == before:
            # A requirement that failed without saying why is still a failure.
            # Reporting it unnamed beats dropping it and printing a shorter,
            # cleaner, wrong answer.
            out.append(f"{name}: failed without a stated reason")
    return "; ".join(out) or "the carrier reported the bundle noncompliant and named no reason"


async def _evaluate(
    client: httpx.AsyncClient,
    collection: str,
    bundle_sid: str,
    policy_sid: str,
    creds: A2PCredentials,
    *,
    step: str,
) -> None:
    """Dry-run the policy and REFUSE to submit a bundle that fails it.

    ⚠ **BEFORE the submission, never after.** ``POST /Evaluations`` is free and
    repeatable; ``Status=pending-review`` is the one-shot that spends the
    submission and, on a bad bundle, leaves a ``twilio-rejected`` row that
    cannot be reused. The six rejected profiles this account collected on
    2026-09-04 are what checking-afterwards looks like.

    ⚠ **This is the whole meaning of a green run.** Until this existed the
    chain checked only that Twilio ACCEPTED each POST: every step returned
    2xx, both bundles were rejected seconds later, and the harness still
    exited 0. A 2xx is proof of receipt and never proof of validity — the
    same shape as a routing sweep that reports SAFE because it classified
    nothing. Whatever else changes here, do not let a submission run without
    this in front of it.

    ⚠ ``Evaluations``, NOT the SDK class name — the identical trap already
    recorded for ``EntityAssignments`` below. Live-probed 2026-09-04 on
    AC01833f...:
        /CustomerProfiles/{sid}/Evaluations -> 200
        /TrustProducts/{sid}/Evaluations    -> 200
    """
    body = await _post(
        client,
        f"{_TRUSTHUB}/{collection}/{bundle_sid}/Evaluations",
        {"PolicySid": policy_sid},
        creds,
        step=step,
    )
    status = str(body.get("status") or "").strip().lower()
    if status == "compliant":
        return
    raise a2p.A2PError(
        f"{step} did not pass the carrier's own policy check",
        step=step,
        reason=_evaluation_failures(body),
    )


def _details_from_row(row: dict[str, Any]) -> a2p.BusinessDetails:
    raw = row.get("sample_messages")
    samples = json.loads(raw) if isinstance(raw, str) else list(raw or [])
    text = {
        f: (row.get(f) or "")
        for f in a2p.REQUIRED_FIELDS
        if f not in ("ein", "website", "iso_country", "use_case")
    }
    return a2p.BusinessDetails(
        **text,
        sample_messages=samples,
        # ⚠ NOT coerced to "". Absence decides the brand kind, and the
        # registry rejects the wrong one (a2p.brand_kind).
        ein=row.get("ein"),
        website=row.get("website"),
        iso_country=row.get("iso_country") or "US",
        # ⚠ A FALLBACK MUST NAME THE CHEAP TIER, NOT THE EXPENSIVE ONE.
        #
        # This read `or "MIXED"` until 2026-09-04. MIXED is the $46 one-time +
        # $10/month brand; LOW_VOLUME is $4.50 + $1.50. Under white-label those
        # fees are per CUSTOMER per month and never amortise, so at 50
        # customers the difference is ~$425/month — the entire saving the
        # 2026-09-02 tier change was made for, sitting in a fallback that
        # silently undoes it.
        #
        # ⚠ It was LATENT, not live, and the distinction matters for anyone
        # re-deriving this: `api/routes/a2p.py` writes `use_case` explicitly on
        # both the INSERT and the upsert's EXCLUDED list, so a registration
        # created through the app has always been LOW_VOLUME. This fires only
        # for a row written by some other path, or one carrying an empty
        # string. That is exactly why five live harness runs never showed it —
        # the harness fixture passes `use_case` too.
        #
        # Sourced from `a2p_defaults.USE_CASE` rather than re-typed, because
        # the tier is now named in four places and two of them had drifted.
        # `test_a2p_registration.py` reads the corpus so a fifth cannot.
        use_case=row.get("use_case") or a2p_defaults.USE_CASE,
    )


def _step_sids(row: dict[str, Any]) -> dict[str, str]:
    raw = row.get("step_sids")
    if isinstance(raw, str):
        return dict(json.loads(raw or "{}"))
    return dict(raw or {})


# ── phase 1: submission ───────────────────────────────────────────────


async def advance_submission(
    row: dict[str, Any],
    creds: A2PCredentials,
    *,
    callback_url: str = "",
    stop_before_paid: bool = False,
    mock: bool = False,
) -> dict[str, Any]:
    """Run the ISV chain as far as it will go. Returns column updates.

    ``stop_before_paid`` returns after step 5. Steps 1-5 are free and are where
    a wrong field name or an unvalidatable address actually shows up, so this
    is the whole chain's cheap half — run it before spending anything.

    ``mock`` registers a MOCK brand (no fee, expires in 30 days, cannot route
    real messages). The response is READ BACK and the run refuses if it does
    not confirm the brand is mock, because the failure mode of a wrong
    parameter name is a silent real registration at full fee.

    Every step is a no-op when its SID is already stored, so calling this on a
    partially-built registration resumes rather than restarts. The return value
    is a dict of column -> value for the caller to persist; this function does
    no database work of its own, which is what makes it testable without one.

    Raises ``A2PError`` carrying the failing step and the carrier's verbatim
    reason. The caller decides whether that is terminal.
    """
    details = _details_from_row(row)
    problems = a2p.validate(details)
    if problems:
        # Should be unreachable — the route validates before queueing — but a
        # row edited by hand must not spend a registration fee on a body the
        # registry will reject.
        raise a2p.A2PError(
            "registration is incomplete", step="validate", reason="; ".join(problems)
        )

    # ⚠ CHECKED BEFORE THE FIRST RESOURCE IS CREATED, not at the step that
    # needs it. Without the primary the secondary profile is rejected with
    # "Primary customer profile bundle is null" — so discovering it late means
    # a customer profile, two end users, an address and a document already
    # exist on the account for a bundle that cannot pass. Refusing here costs
    # nothing and leaves no debris.
    if not creds.primary_profile_sid:
        raise a2p.A2PError(
            "no ISV primary customer profile is configured",
            step="primary_profile",
            reason=(
                "Set HELIXIS_TWILIO__PRIMARY_PROFILE_SID to this Twilio account's "
                "APPROVED primary customer profile bundle (BU...). Every secondary "
                "profile must be assigned to it; the carrier rejects the bundle "
                "otherwise. It is one value per deployment, not per customer."
            ),
        )

    sids = _step_sids(row)
    updates: dict[str, Any] = {}

    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        # 1. Secondary customer profile — the customer's business under ours.
        profile_sid = row.get("customer_profile_sid")
        if not profile_sid:
            body = await _post(
                client,
                f"{_TRUSTHUB}/CustomerProfiles",
                a2p.customer_profile_payload(
                    details, email=details.rep_email, callback=callback_url
                ),
                creds,
                step="customer_profile",
            )
            profile_sid = str(body.get("sid") or "")
            if not profile_sid:
                raise a2p.A2PError("no profile SID returned", step="customer_profile")
            updates["customer_profile_sid"] = profile_sid

        # 2. The two end users: business facts, and the human the registry
        #    emails for attestation.
        for key, payload in (
            ("business_end_user", a2p.business_end_user_payload(details)),
            ("representative_end_user", a2p.representative_end_user_payload(details)),
        ):
            if sids.get(key):
                continue
            body = await _post(client, f"{_TRUSTHUB}/EndUsers", payload, creds, step=key)
            sids[key] = str(body.get("sid") or "")

        # 3. The address, then the document that wraps it. The address is an
        #    account resource (api.twilio.com); the supporting document that
        #    references it is a trust-hub one.
        if not sids.get("address"):
            body = await _post(
                client,
                f"{_API}/Accounts/{creds.account_sid}/Addresses.json",
                a2p.address_payload(details),
                creds,
                step="address",
            )
            sids["address"] = str(body.get("sid") or "")
        if not sids.get("supporting_document"):
            body = await _post(
                client,
                f"{_TRUSTHUB}/SupportingDocuments",
                {
                    "FriendlyName": f"{details.legal_business_name} address",
                    "Type": "customer_profile_address",
                    "Attributes": {"address_sids": sids["address"]},
                },
                creds,
                step="supporting_document",
            )
            sids["supporting_document"] = str(body.get("sid") or "")

        # 4. Bind them to the profile, then submit it for review.
        for key in ("business_end_user", "representative_end_user", "supporting_document"):
            assigned = f"assigned_{key}"
            if sids.get(assigned) or not sids.get(key):
                continue
            await _post(
                client,
                # ⚠ ``EntityAssignments``, NOT ``CustomerProfilesEntityAssignments``.
                # The latter is the twilio-python SDK's CLASS name; the REST
                # sub-resource is plain ``EntityAssignments``, and the wrong one
                # 404s. Live-probed 2026-09-01 on AC01833f...:
                #   /CustomerProfiles/{sid}/CustomerProfilesEntityAssignments -> 404
                #   /CustomerProfiles/{sid}/EntityAssignments                 -> 200
                f"{_TRUSTHUB}/CustomerProfiles/{profile_sid}/EntityAssignments",
                {"ObjectSid": sids[key]},
                creds,
                step=assigned,
            )
            sids[assigned] = sids[key]
        # 4b. ⚠ ISV guide step 1.9 — "Assign the Secondary Customer Profile to
        #     the Primary Customer Profile", ObjectSid = the PRIMARY's SID.
        #     This step did not exist in this module until 2026-09-04 and its
        #     absence is why no registration could ever have passed: the
        #     evaluation says "Primary customer profile bundle is null".
        #
        #     ⚠ Same sub-resource trap as above — ``EntityAssignments``, never
        #     the SDK's class name. The ObjectSid here is a BUNDLE, not an end
        #     user, which is the whole point: it is the only assignment on this
        #     profile that does not come from the customer's own form.
        if not sids.get("assigned_primary_profile"):
            await _post(
                client,
                f"{_TRUSTHUB}/CustomerProfiles/{profile_sid}/EntityAssignments",
                {"ObjectSid": creds.primary_profile_sid},
                creds,
                step="assign_primary_profile",
            )
            sids["assigned_primary_profile"] = creds.primary_profile_sid

        if not sids.get("profile_submitted"):
            await _evaluate(
                client,
                "CustomerProfiles",
                profile_sid,
                a2p.SECONDARY_CUSTOMER_PROFILE_POLICY,
                creds,
                step="evaluate_profile",
            )
            await _post(
                client,
                f"{_TRUSTHUB}/CustomerProfiles/{profile_sid}",
                {"Status": "pending-review"},
                creds,
                step="submit_profile",
            )
            sids["profile_submitted"] = "yes"

        # 5. The A2P trust product — the compliance container the brand reads.
        trust_sid = row.get("trust_product_sid")
        if not trust_sid:
            body = await _post(
                client,
                f"{_TRUSTHUB}/TrustProducts",
                {
                    "FriendlyName": f"{details.legal_business_name} A2P",
                    "Email": details.rep_email,
                    "PolicySid": a2p.A2P_TRUST_PRODUCT_POLICY,
                    "StatusCallback": callback_url,
                },
                creds,
                step="trust_product",
            )
            trust_sid = str(body.get("sid") or "")
            if not trust_sid:
                raise a2p.A2PError("no trust product SID returned", step="trust_product")
            updates["trust_product_sid"] = trust_sid
        if not sids.get("assigned_profile_to_trust"):
            await _post(
                client,
                # ⚠ Same SDK-class-name-as-REST-path mistake as the customer
                # profile above. Live-probed 2026-09-01:
                #   /TrustProducts/{sid}/TrustProductsEntityAssignments -> 404
                #   /TrustProducts/{sid}/EntityAssignments              -> 200
                f"{_TRUSTHUB}/TrustProducts/{trust_sid}/EntityAssignments",
                {"ObjectSid": profile_sid},
                creds,
                step="assign_profile_to_trust",
            )
            sids["assigned_profile_to_trust"] = profile_sid
        # 5b. The A2P messaging profile end user — ``company_type``. Absent
        #     from this module until 2026-09-04, which the trust product's own
        #     evaluation reported as "The Company Type is invalid." (it was
        #     missing, not malformed). Created and assigned here, to the TRUST
        #     PRODUCT and not the customer profile, because that is the bundle
        #     whose evaluation names it.
        if not sids.get("a2p_profile_end_user"):
            body = await _post(
                client,
                f"{_TRUSTHUB}/EndUsers",
                a2p.a2p_messaging_profile_payload(details),
                creds,
                step="a2p_profile_end_user",
            )
            sids["a2p_profile_end_user"] = str(body.get("sid") or "")
        if not sids.get("assigned_a2p_profile") and sids.get("a2p_profile_end_user"):
            await _post(
                client,
                f"{_TRUSTHUB}/TrustProducts/{trust_sid}/EntityAssignments",
                {"ObjectSid": sids["a2p_profile_end_user"]},
                creds,
                step="assign_a2p_profile",
            )
            sids["assigned_a2p_profile"] = sids["a2p_profile_end_user"]

        if not sids.get("trust_submitted"):
            await _evaluate(
                client,
                "TrustProducts",
                trust_sid,
                a2p.A2P_TRUST_PRODUCT_POLICY,
                creds,
                step="evaluate_trust_product",
            )
            await _post(
                client,
                f"{_TRUSTHUB}/TrustProducts/{trust_sid}",
                {"Status": "pending-review"},
                creds,
                step="submit_trust_product",
            )
            sids["trust_submitted"] = "yes"

        # 6. ⚠ THE FIRST PAID STEP. Guarded by the stored SID above all else:
        #    a second brand for one business costs another fee AND is rejected
        #    by the registry as a duplicate.
        brand_sid = row.get("brand_sid")
        if not brand_sid:
            if stop_before_paid:
                # Everything above this line is free. Stopping here validates
                # the whole trust-hub chain — the half where a wrong field name
                # actually lives — without spending anything.
                updates["step_sids"] = json.dumps(sids)
                return updates
            await asyncio.sleep(_RATE_LIMIT_S)
            form: dict[str, Any] = {
                "CustomerProfileBundleSid": profile_sid,
                "A2PProfileBundleSid": trust_sid,
                "BrandType": a2p.brand_kind(details),
                # ⚠ Skips TCR's automatic SECONDARY vetting, which is a paid
                # add-on we do not need: it exists to lift daily throughput
                # for high-volume senders, and this product sends templated
                # notices to a property manager's own residents. Paying for
                # throughput nobody uses is a per-customer fee under
                # white-label, so it compounds with every signup.
                #
                # Safe direction by construction — the only thing it can do is
                # NOT buy an extra step. A wrong value here cannot spend more
                # than the base brand fee.
                #
                # ⚠ Field name from the founder's research spec, NOT a live
                # call (twilio.com is egress-blocked — gotcha 75). Twilio
                # IGNORES an unrecognised form field rather than rejecting it,
                # so a typo here is silent and simply buys the vetting anyway.
                # Confirm on the first `--tier mock` run.
                "SkipAutomaticSecVet": "true",
            }
            if mock:
                form[_MOCK_BRAND_PARAM] = "true"
            body = await _post(
                client,
                f"{_MESSAGING}/a2p/BrandRegistrations",
                form,
                creds,
                step="brand_registration",
            )
            brand_sid = str(body.get("sid") or "")
            if not brand_sid:
                raise a2p.A2PError("no brand SID returned", step="brand_registration")
            if mock and not _is_mock_brand(body):
                # ⚠ READ BACK, do not assume (gotcha 20/70: a 2xx is not the
                # write). If the mock parameter name is wrong the API ignores
                # it, returns 200, and registers a REAL brand at full fee — a
                # harness that reported that as free would be worse than no
                # harness. This cannot prevent the charge; it makes the charge
                # impossible to miss and stops the run before the campaign
                # spends more.
                raise a2p.A2PError(
                    "brand registered but NOT confirmed mock",
                    step="brand_registration",
                    reason=(
                        f"created {brand_sid} and the response does not say it is a mock "
                        f"brand — a real registration fee may have been charged. Check the "
                        f"{_MOCK_BRAND_PARAM!r} parameter name against the live API, and "
                        f"delete {brand_sid} if it is real."
                    ),
                )
            updates["brand_sid"] = brand_sid
            updates["status"] = a2p.map_brand_status(body.get("status")) or "brand_pending"

        # 7. The sender pool the campaign attaches to.
        service_sid = row.get("messaging_service_sid")
        if not service_sid:
            body = await _post(
                client,
                f"{_MESSAGING}/Services",
                {
                    "FriendlyName": f"Occupella {details.legal_business_name}"[:64],
                    "InboundRequestUrl": f"{creds.webhook_base_url}/api/v1/twilio/inbound",
                    "StatusCallback": f"{creds.webhook_base_url}/api/v1/twilio/status",
                },
                creds,
                step="messaging_service",
            )
            service_sid = str(body.get("sid") or "")
            if not service_sid:
                raise a2p.A2PError("no messaging service SID returned", step="messaging_service")
            updates["messaging_service_sid"] = service_sid

    updates["step_sids"] = json.dumps(sids)
    return updates


async def submit_campaign(row: dict[str, Any], creds: A2PCredentials) -> dict[str, Any]:
    """Step 8 — the campaign, which only goes in once the BRAND is approved.

    Deliberately separate from ``advance_submission``: the brand has to clear
    vetting first, so this is reached by the poller rather than by the initial
    run. Submitting a campaign against an unapproved brand is rejected, and a
    rejection here is the one that costs 10-15 days.
    """
    if row.get("campaign_sid"):
        return {}
    brand_sid = row.get("brand_sid")
    service_sid = row.get("messaging_service_sid")
    if not (brand_sid and service_sid):
        raise a2p.A2PError("campaign needs a brand and a messaging service first", step="campaign")
    details = _details_from_row(row)
    # ⚠ The two policy URLs are a hard requirement since 2026-06-30 (error
    # 30933) and Twilio FETCHES them, so they must point at a domain that
    # actually serves the pages — which is why they are built from the same
    # webhook base URL Twilio already reaches for the inbound webhooks, not
    # from a marketing domain that only proxies /api.
    company_id = str(row.get("company_id") or "")
    privacy_url = trust_pages.page_url(creds.webhook_base_url, company_id, trust_pages.PRIVACY)
    terms_url = trust_pages.page_url(creds.webhook_base_url, company_id, trust_pages.TERMS)
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        await asyncio.sleep(_RATE_LIMIT_S)
        body = await _post(
            client,
            f"{_MESSAGING}/Services/{service_sid}/Compliance/Usa2p",
            a2p.campaign_payload(
                details,
                brand_sid=str(brand_sid),
                privacy_policy_url=privacy_url,
                terms_url=terms_url,
            ),
            creds,
            step="campaign",
        )
    sid = str(body.get("sid") or "")
    if not sid:
        raise a2p.A2PError("no campaign SID returned", step="campaign")
    return {
        "campaign_sid": sid,
        "status": a2p.map_campaign_status(body.get("campaign_status")) or "campaign_pending",
    }


# ── phase 2: polling ──────────────────────────────────────────────────


async def poll_status(row: dict[str, Any], creds: A2PCredentials) -> dict[str, Any]:
    """Read the carrier's verdict and map it onto our status.

    ⚠ An UNRECOGNISED carrier status maps to ``None`` and this returns no
    status change (``a2p.map_*_status`` is deliberately not a ``.get`` with a
    default). Inventing a state for an unknown response is how a pending
    registration starts reading as approved — the exact shape of gotcha 55's
    decline-to-assert, pointed at a third party's vocabulary.
    """
    updates: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        campaign_sid = row.get("campaign_sid")
        if campaign_sid:
            # ⚠ THE SID IS PART OF THE URL. Without it this is the LIST
            # endpoint, which answers 200 with a wrapper — so nothing raises,
            # nothing logs, and the campaign silently never leaves pending.
            # Measured 2026-09-04 against the live campaign QE2c6890da…:
            #
            #   …/Compliance/Usa2p        -> {"compliance": [ {...} ], "meta": …}
            #                                `campaign_status` absent at top level
            #                                -> map_campaign_status(None) -> None
            #                                -> poll_status returns {} FOREVER
            #   …/Compliance/Usa2p/{sid}  -> flat, `campaign_status` = "VERIFIED"
            #                                -> "approved"
            #
            # The campaign was ALREADY approved while the poller reported no
            # change, and `ensure_number` only runs at `approved` — so the
            # number is never bought and the registration stalls one step from
            # done. The brand poll below always interpolated its SID; this is
            # the same shape, and the asymmetry was the whole defect.
            body = await _get(
                client,
                f"{_MESSAGING}/Services/{row['messaging_service_sid']}"
                f"/Compliance/Usa2p/{campaign_sid}",
                creds,
                step="poll_campaign",
            )
            mapped = a2p.map_campaign_status(body.get("campaign_status"))
            if mapped:
                updates["status"] = mapped
                if mapped == "approved":
                    updates["campaign_approved_at"] = datetime.now(UTC)
                if mapped == "failed":
                    updates["failure_reason"] = _rejection_text(body)
            return updates

        brand_sid = row.get("brand_sid")
        if brand_sid:
            body = await _get(
                client,
                f"{_MESSAGING}/a2p/BrandRegistrations/{brand_sid}",
                creds,
                step="poll_brand",
            )
            mapped = a2p.map_brand_status(body.get("status"))
            if mapped:
                updates["status"] = mapped
                if mapped == "brand_approved":
                    updates["brand_approved_at"] = datetime.now(UTC)
                if mapped == "failed":
                    updates["failure_reason"] = _rejection_text(body)
    return updates


def _rejection_text(body: dict[str, Any]) -> str:
    """The registry's reasons, joined, verbatim.

    Brand and campaign rejections arrive as a list of ``{code, description}``;
    a bare "failed" is unactionable and the description is the whole value.
    """
    reasons = body.get("failure_reason") or body.get("errors") or []
    if isinstance(reasons, str):
        return reasons[:500]
    parts = [
        str(r.get("description") or r.get("message") or r)
        for r in reasons
        if isinstance(r, dict) or r
    ]
    return "; ".join(p for p in parts if p)[:500] or "The registry rejected the registration."


# ── phase 3: the number ───────────────────────────────────────────────


async def ensure_number(
    company_id: str,
    row: dict[str, Any],
    creds: A2PCredentials,
    *,
    existing_numbers: int,
) -> str | None:
    """Buy this company its number once the campaign is approved.

    Returns the E.164 number bought, or None when nothing was bought.

    ⚠ **This spends money** — a number is a recurring monthly charge. Two
    guards, in this order and both BEFORE any carrier call:

    1. **Already has a number wins over everything.** Two numbers for one
       company is a real cost and an ambiguous outbound identity;
       ``telephony_for_company`` would pick by ``created_at`` and the other
       number would keep receiving replies nobody reads.
    2. **Approved, and only approved.** A number attached to an unapproved
       campaign cannot send, so buying early charges for something dark.

    The webhook base URL is required by ``buy_number`` itself: an unwired
    number receives messages that go nowhere and the charge is already
    incurred.
    """
    if existing_numbers > 0:
        return None
    if row.get("status") != "approved":
        return None
    if not creds.usable or not creds.webhook_base_url:
        logger.warning("a2p: approved but cannot buy a number — carrier config incomplete")
        return None

    candidates = await twilio_client.search_available_numbers(
        account_sid=creds.account_sid, auth_token=creds.auth_token
    )
    if not candidates:
        logger.warning("a2p: approved for company=%s but no numbers were available", company_id)
        return None
    bought = await twilio_client.buy_number(
        account_sid=creds.account_sid,
        auth_token=creds.auth_token,
        phone_number=candidates[0]["phone_number"],
        webhook_base_url=creds.webhook_base_url,
        friendly_name=f"Occupella {company_id[:8]}",
    )
    number = normalize_e164(bought) or bought

    # Attach it to the campaign's messaging service. A number that is bought
    # and wired but NOT in the service sends under no campaign, which carriers
    # filter — the one failure that looks like "texting just doesn't work".
    service_sid = row.get("messaging_service_sid")
    if service_sid:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            try:
                await _post(
                    client,
                    f"{_MESSAGING}/Services/{service_sid}/PhoneNumbers",
                    {"PhoneNumberSid": await _number_sid(client, creds, number)},
                    creds,
                    step="attach_number",
                )
            except a2p.A2PError:
                # ⚠ Do NOT re-raise: the number is BOUGHT by this point and
                # the charge is incurred. Losing the row to an exception would
                # leave a paid, unrecorded number — the one outcome with no
                # recovery path. Attachment is retried on the next sweep.
                logger.exception("a2p: bought %s but could not attach it to the service", number)
    return number


async def _number_sid(client: httpx.AsyncClient, creds: A2PCredentials, number: str) -> str:
    # ⚠ PERCENT-ENCODED, and the character that matters is the leading "+".
    #
    # In a query string "+" means SPACE (form-urlencoded), so interpolating a
    # raw E.164 number searched for " 13035550100" and matched nothing —
    # every lookup raised "bought number not found on the account" for a
    # number that was sitting right there, already paid for.
    #
    # This fires immediately after a carrier approval: `ensure_number` buys the
    # number, then calls this to attach it to the messaging service. So the
    # first customer to be approved would have had the step AFTER the money
    # fail, on a number they now own and we cannot find.
    #
    # `safe=""` because the default leaves "+" alone, which is the whole bug.
    body = await _get(
        client,
        f"{_API}/Accounts/{creds.account_sid}/IncomingPhoneNumbers.json"
        f"?PhoneNumber={urllib.parse.quote(number, safe='')}",
        creds,
        step="lookup_number",
    )
    rows = body.get("incoming_phone_numbers") or []
    if not rows:
        raise a2p.A2PError("bought number not found on the account", step="lookup_number")
    return str(rows[0].get("sid") or "")


def due(row: dict[str, Any], *, now: datetime | None = None, needs_number: bool = False) -> bool:
    """Should the sweep touch this row now?

    ⚠ Backoff is the whole point. Campaign vetting is 10-15 days; polling it
    every worker cycle spends carrier API calls on a status that cannot have
    moved, and the customer is not watching. A failed submission retries much
    sooner because a failure is often OURS and fixable.

    ⚠ **`approved` is TERMINAL and still has work left.** The last step of the
    flow — buying the number — happens AFTER the status reaches its final
    value, so a plain `is_terminal` skip would mean a company gets approved
    and never receives the number that approval was for. `needs_number` is the
    caller's answer to "does this company still lack one", and it is the one
    thing that keeps a terminal row in the sweep.
    """
    if a2p.is_terminal(row.get("status") or "") and not (
        needs_number and row.get("status") == "approved"
    ):
        return False
    last: datetime | None = row.get("last_attempt_at")
    if last is None:
        return True
    # A naive stamp is UTC by construction (the column is timestamptz and we
    # write datetime.now(UTC)), but a fake or a hand-edited row can be naive
    # and subtracting one from an aware now() raises.
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    window = RETRY_BACKOFF if row.get("failure_reason") else POLL_BACKOFF
    return bool((now or datetime.now(UTC)) - last >= window)
