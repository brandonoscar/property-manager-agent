"""Twilio customer-care SMS webhooks — inbound replies + delivery receipts.

Stage 2 of the outbound-SMS system (``comms/sms.py`` is Stage 1). Two
form-encoded POST endpoints, NOT JWT-authed (Twilio can't send one) — the
``X-Twilio-Signature`` header is the authentication mechanism, verified
FAIL CLOSED against ``twilio.webhook_base_url`` + the route path
(``comms/twilio_signature.py``): a missing header, bad signature, empty
auth token, or empty base URL all 403 before ANY form parsing or DB work.

* ``POST /api/v1/twilio/inbound`` — a tenant/manager texted our number.
  STOP-family keywords revoke consent on EVERY ``helixis_sms_consent`` row
  matching the phone across ALL companies (conservative: one STOP silences
  every sender); START-family restores only previously-revoked rows and
  NEVER creates consent (no prior relationship = no consent); HELP touches
  no ledger (Twilio Advanced Opt-Out auto-responds). Every inbound —
  keyword or not — gets a ``helixis_sms_log`` row (direction='inbound',
  body kept). Responds with empty TwiML so Twilio sends no auto-reply
  beyond its own opt-out handling.

* ``POST /api/v1/twilio/status`` — delivery receipts (``StatusCallback``,
  requested by ``send_sms`` when ``webhook_base_url`` is set): advances the
  log row keyed by MessageSid. An unknown sid is noise (a sid we never
  sent), not an error — warn + 204, because Twilio retries on non-2xx.

This is DISTINCT from the CRM lead lane (``/crm/sms/inbound``): that path
creates leads for one configured company and validates against
``request.url``; this path is the compliance ledger for the A2P
customer-care campaign and has no testing bypass — there is deliberately
NO ``skip_signature_check`` here (an unsigned request could forge consent
revocations/restorations).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response

from helixis.comms.consent_keywords import apply_consent_keywords, classify_keyword
from helixis.comms.number_routing import resolve_company_for_number
from helixis.comms.twilio_signature import validate_twilio_signature
from helixis.db.engine import get_pool
from helixis.settings import Settings
from helixis.webhooks.comms_ingest import bridge_inbound_comms
from helixis.webhooks.crm_sms_inbound import handle_inbound_lead_sms

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/twilio", tags=["twilio"])

# The opt-out / opt-in keyword families moved to ``comms/consent_keywords``
# so both inbound routes apply identical rules.
#
# Statuses the log's CHECK constraint accepts. Twilio can callback with
# intermediate statuses outside this set (e.g. 'sending', 'accepted') —
# those are skipped rather than 500ing into a Twilio retry loop.
_LOG_STATUSES = {"queued", "sent", "delivered", "undelivered", "failed", "received"}

_EMPTY_TWIML = "<Response/>"


async def _verified_params(request: Request) -> dict[str, str]:
    """403 unless the request carries a valid Twilio signature.

    FAIL CLOSED: empty ``auth_token``, empty ``webhook_base_url``, or a
    missing header all 403 BEFORE the form is parsed or any DB is touched.
    The signed URL is built from the CONFIGURED base + the route path —
    never from ``request.url``, which a proxy can rewrite — so it matches
    exactly what the Twilio console has registered.
    """
    settings: Settings = request.app.state.settings
    auth_token = settings.twilio.auth_token
    base = (settings.twilio.webhook_base_url or "").strip().rstrip("/")
    signature = request.headers.get("X-Twilio-Signature")
    if not auth_token or not base or not signature:
        raise HTTPException(status_code=403, detail="Invalid Twilio signature.")

    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    if not validate_twilio_signature(
        auth_token=auth_token,
        url=f"{base}{request.url.path}",
        params=params,
        signature=signature,
    ):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature.")
    return params


@router.post("/inbound")
async def inbound_sms(request: Request) -> Response:
    """A reply texted to our customer-care number. Mirrors STOP/START into
    the consent ledger and logs every message. See the module docstring."""
    params = await _verified_params(request)
    from_number = params.get("From", "").strip()
    body = params.get("Body", "") or ""
    message_sid = (params.get("MessageSid") or params.get("SmsSid") or "").strip()
    pool = get_pool()
    settings: Settings = request.app.state.settings

    # ⚠ Resolved from the `To` NUMBER, not from a consent row. The opt-out
    # record below has to work for somebody who has no consent row — that is
    # the entire reason it exists — so deriving the company from the ledger
    # would be circular and would silently skip every lead.
    receiving_company = await resolve_company_for_number(
        pool, params.get("To", "").strip(), twilio_settings=settings.twilio
    )

    # Consent FIRST, and unconditionally. The keyword handling lives in
    # ``comms/consent_keywords`` so the CRM's inbound route applies the exact
    # same rules — a Twilio number has ONE inbound URL, so whichever route the
    # console names must do the whole job (see ``webhooks/crm_sms_inbound``).
    await apply_consent_keywords(
        pool,
        from_number=from_number,
        body=body,
        company_id=receiving_company or "",
        message_sid=message_sid,
    )

    # Every inbound — keyword or not — gets a log row. company_id comes from
    # a matching consent row when one exists (any company; revoked rows still
    # attribute), else null (e.g. a wrong-number text). ON CONFLICT makes a
    # Twilio retry of the same MessageSid idempotent.
    company_row = None
    if from_number:
        company_row = await pool.fetchrow(
            "SELECT company_id FROM public.helixis_sms_consent "
            "WHERE phone = $1 ORDER BY consented_at LIMIT 1",
            from_number,
        )
    logged = await pool.fetchrow(
        "INSERT INTO public.helixis_sms_log "
        "(company_id, direction, phone, body, twilio_sid, status) "
        "VALUES ($1::uuid, 'inbound', $2, $3, $4, 'received') "
        "ON CONFLICT (twilio_sid) DO NOTHING "
        "RETURNING created_at",
        company_row["company_id"] if company_row else None,
        from_number,
        body,
        message_sid or None,
    )

    # Surface it in the Inbox — the receiving half of comms_triage, which had
    # no caller until now (see webhooks/comms_ingest). Gated on a FRESH log
    # row: ON CONFLICT returns nothing for a Twilio redelivery, so the retry
    # cannot re-card. The queue's MessageId dedup is the second layer.
    #
    # ⚠ A SID-less message is never bridged. `twilio_sid` is nullable and NULLs
    # do not collide in a unique index, so those rows insert fresh EVERY time —
    # the idempotency this branch depends on simply does not exist without one.
    # Real Twilio deliveries always carry a SID; declining is cheaper than
    # double-carding a resident's text.
    #
    # bridge_inbound_comms never raises: Twilio retries the whole delivery on a
    # non-2xx, which would re-run the consent mirroring above.
    if logged is not None and message_sid and from_number:
        await bridge_inbound_comms(
            pool,
            channel="sms",
            from_number=from_number,
            to_number=params.get("To", "").strip(),
            body=body,
            sid=message_sid,
            occurred_at=logged["created_at"],
            # ⚠ The company that OWNS the number they texted. Without it the
            # bridge falls back to "which ledgers know this sender", which is
            # ambiguous for anyone in two companies' books and silently cards
            # nothing.
            company_id=receiving_company or "",
        )

    # The Leasing half. Same reason as the consent block above: this route may
    # be the one the console names, and if it is, a lead that texts in must
    # still reach the pipeline. Company comes from the number this arrived AT
    # — never a global default, which would file every deployment's inbound
    # texts into one company.
    #
    # A keyword message is not a lead enquiry: someone texting STOP is opting
    # out, and creating a lead from it would be both useless and tasteless.
    if from_number and classify_keyword(body) is None:
        company_id = await resolve_company_for_number(
            pool, params.get("To", ""), twilio_settings=settings.twilio
        )
        if company_id:
            await handle_inbound_lead_sms(
                params,
                settings=settings,
                pool=pool,
                company_id=company_id,
                from_number=from_number,
                body=body,
                message_sid=message_sid or None,
            )

    # Empty TwiML — accepted, and no auto-reply beyond Twilio's own
    # Advanced Opt-Out handling.
    return Response(content=_EMPTY_TWIML, media_type="application/xml")


@router.post("/status")
async def delivery_status(request: Request) -> Response:
    """A delivery receipt for an outbound send — advance the log row's
    status by MessageSid. Always 2xx once verified (Twilio retries on
    non-2xx, and none of the soft cases below merit a retry)."""
    params = await _verified_params(request)
    message_sid = (params.get("MessageSid") or params.get("SmsSid") or "").strip()
    status = (params.get("MessageStatus") or "").strip().lower()
    error_code = (params.get("ErrorCode") or "").strip() or None

    if not message_sid:
        logger.warning("sms status callback without a MessageSid — ignored")
        return Response(status_code=204)
    if status not in _LOG_STATUSES:
        # An intermediate/unknown status the log doesn't model (e.g.
        # 'sending') — skip rather than violate the CHECK constraint.
        logger.info("sms status callback sid=%s unmodeled status=%r — ignored", message_sid, status)
        return Response(status_code=204)

    pool = get_pool()
    result = await pool.execute(
        "UPDATE public.helixis_sms_log "
        "SET status = $2, error_code = $3, updated_at = now() "
        "WHERE twilio_sid = $1",
        message_sid,
        status,
        error_code,
    )
    if result == "UPDATE 0":
        # A sid we never sent is noise (or a log insert that best-effort
        # failed), not an error — warn, don't make Twilio retry.
        logger.warning("sms status callback for unknown sid=%s status=%s", message_sid, status)
    return Response(status_code=204)


# ── A2P Trust Hub status callback ───────────────────────────────────────────
#
# ⚠ THIS EXISTED AS A URL AND NOT AS A HANDLER. ``a2p_cycle._callback_url``
# pointed the carrier at ``/api/v1/twilio/status`` — the DELIVERY-RECEIPT route
# directly above — whose first act is to read ``MessageSid``. A Trust Hub
# bundle callback carries no MessageSid, so every profile status the carrier
# ever posted hit `logger.warning(... ignored)` and returned 204. The one thing
# the callback exists to deliver is ``failure_reason`` on an address-validation
# rejection, which that function's own docstring calls "the most common one" —
# so the field the customer needs in order to fix their registration was being
# thrown away, while the docstring said we treat the callback as required.
# Gotcha 68's shape (two handlers, one URL) meeting gotcha 48's (a comment that
# cannot fail).
#
#: Keys a Trust Hub / brand callback might carry the resource SID under.
#: ⚠ twilio.com is EGRESS-BLOCKED from this container (gotcha 75), so the exact
#: spelling is NOT live-probed. That is the reason for a SET rather than one
#: name, and the reason an unresolved callback logs its whole parameter set
#: below instead of being dropped: the first real callback is what settles
#: this, and it can only settle it if we wrote down what arrived.
_A2P_SID_KEYS = ("BundleSid", "Sid", "ResourceSid", "BrandSid", "CampaignSid", "ObjectSid")

#: Carrier vocabularies that mean REJECTED. Everything else — including every
#: flavour of approval — is deliberately unmapped.
_A2P_REJECTED = {"twilio-rejected", "rejected", "failed", "in-review-rejected"}


@router.post("/a2p-status")
async def a2p_status(request: Request) -> Response:
    """A Trust Hub bundle / brand status callback for an A2P registration.

    ⚠ **This route can move a registration to ``failed`` and can never move it
    forward.** Promoting a row on a callback would let one misparsed or
    replayed POST read as carrier approval, and ``a2p.APPROVED`` is the single
    value that opens outbound SMS for a company — the exact pre-approval send
    the whole compliance gate exists to prevent (gotchas 67/72). The poller's
    authoritative GET is what promotes; under-trusting a callback costs one
    poll cycle, over-trusting it is the violation carriers audit for. Same
    decline-rather-than-guess posture as ``map_brand_status`` refusing to
    invent a state for an unrecognised carrier string (gotcha 55).

    So its whole job is the REASON: keep the carrier's verbatim words, which
    are what tell a customer their street address failed validation.
    """
    params = await _verified_params(request)
    sid = next((v.strip() for k in _A2P_SID_KEYS if (v := params.get(k, "")).strip()), "")
    status = (params.get("Status") or params.get("BundleStatus") or "").strip().lower()
    # Kept VERBATIM. A paraphrased rejection is unactionable (0084's own column
    # comment says so), and this is the only place the text ever appears.
    reason = (params.get("FailureReason") or params.get("StatusCallbackError") or "").strip()

    if not sid:
        logger.warning("a2p status callback carried no recognisable sid — params=%r", params)
        return Response(status_code=204)

    if status not in _A2P_REJECTED and not reason:
        # An approval or an intermediate state: nothing to record, and the
        # poller owns forward movement. Logged at info so a real callback's
        # shape is still visible the first time one arrives.
        logger.info("a2p status callback sid=%s status=%r — no action", sid, status)
        return Response(status_code=204)

    pool = get_pool()
    # ⚠ Company scoping is the ROW: `helixis_a2p_registrations.company_id` is
    # UNIQUE, and the SID was minted by us for one company, so matching on it
    # selects exactly that company's registration (gotcha 47 — deliberate, not
    # a forgotten filter).
    result = await pool.execute(
        "UPDATE public.helixis_a2p_registrations "
        "SET status = CASE WHEN $2 THEN 'failed' ELSE status END, "
        "    failure_reason = COALESCE(NULLIF($3, ''), failure_reason), "
        "    updated_at = now() "
        "WHERE customer_profile_sid = $1 OR trust_product_sid = $1 "
        "   OR brand_sid = $1 OR campaign_sid = $1",
        sid,
        status in _A2P_REJECTED,
        reason,
    )
    if result == "UPDATE 0":
        # ⚠ The params go in the log. A SID we cannot resolve is precisely the
        # case where the key spelling above is wrong, and dropping it silently
        # is how this defect survived in the first place.
        logger.warning(
            "a2p status callback for unknown sid=%s status=%r params=%r", sid, status, params
        )
    else:
        logger.info("a2p status callback sid=%s status=%r reason=%r recorded", sid, status, reason)
    return Response(status_code=204)
