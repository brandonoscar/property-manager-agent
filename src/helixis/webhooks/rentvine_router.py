"""POST /webhooks/rentvine/{account_code} — inbound Rentvine receiver. Phase 5.

FAIL-CLOSED BY CONSTRUCTION, AND THAT IS THE WHOLE POINT
    Nobody knows how Rentvine signs a webhook: the captured vendor spec has
    ZERO occurrences of ``webhook``, ``signature`` or ``hmac`` (api-notes §7).
    A receiver that fails OPEN on an unknown algorithm is strictly worse than
    no receiver — it accepts forged events into a landlord's books with a 200
    and nothing saying the payload was unverified. So
    ``rentvine/signature.verify_signature`` refuses BEFORE it looks at anything
    else, and until ``scripts/rentvine_webhook_capture.py`` derives the scheme
    from one real delivery this route rejects every signed request it receives.
    SETTLED 2026-10-04: Mailgun's construction over the body's ``auth`` block
    (``rentvine/signature.py``, beside ``BODY_PREFIX``). It is still read from
    settings, so until a deployment sets the six values every delivery is
    refused.

REFUSED DELIVERIES ARE KEPT (migration 0106)
    Rentvine signs in the URL's query string (measured 2026-10-04), and a
    refused delivery used to be kept nowhere, so deriving the scheme needed a
    tunnel to the founder's machine. Each refusal for a known account is now
    stored verbatim, bounded per company (``rentvine/webhook_deliveries.py``),
    and ``scripts/rentvine_webhook_capture.py --analyse-db`` derives from them.

WHY THE ACCOUNT CODE IS IN THE URL
    A Rentvine delivery carries no company context and the payload's shape is
    undocumented, so resolving the company from a payload FIELD would be
    guessing a key (gotcha 79). The customer pastes this URL into the Rentvine
    UI by hand — one webhook row at a time, there is no subscription API — so
    the account code is something WE choose to put in a path segment rather
    than something we hope to find in a body. It is a subdomain, not a secret:
    it is already in every API URL this connector emits.

WHAT A DELIVERY ACTUALLY BUYS, AND WHAT IT DELIBERATELY DOES NOT
    It re-reads ONE record from Rentvine and upserts it — a targeted freshness
    improvement on a mirror the poll already maintains. Decision (c) makes
    polling PRIMARY on Rentvine and webhooks an optimisation for four object
    types, so a refused or failed delivery costs latency, never data.

    ⚠ **It does NOT run ``dispatch_event``, the enrichment registry or the
    routing pass, and that is a finding rather than an omission.** Decision (c)
    says normalising at the door leaves the eight handler modules untouched —
    true of the NAMES and insufficient for the WORK, because every mirror
    handler's body is a BUILDIUM fetch (``open_api_for_company`` →
    ``api.rentals.get_property(...)``). A Rentvine company has no Buildium
    credentials — migration 0100's trigger forbids holding both — so those
    handlers would mirror nothing and write an activity row describing a fetch
    that never happened, and the inbox-eligible names (``WorkOrder.``,
    ``Lease.``) would additionally enqueue a planner row whose enricher fails
    the same way. An empty Inbox card is worse than no card. Teaching the
    enrichers a second PMS is Phase 6's decision, taken with the handlers in
    front of you rather than smuggled in here.

    The normalised name is still produced and still a real member of the
    existing 43-name vocabulary — a guard pins that — so Phase 6 inherits a
    door that already speaks the right language.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from helixis.api.rate_limit import SlidingWindowCounter, webhook_limiter
from helixis.db.engine import get_pool
from helixis.db.repositories import ActivityEntry, write_activity
from helixis.rentvine import events
from helixis.rentvine.client import RentvineClient
from helixis.rentvine.credentials import (
    decrypt_rentvine_settings_from,
    decrypt_webhook_secret,
    fetch_rentvine_row_by_account_code,
)
from helixis.rentvine.signature import SignatureUnverifiable, scheme_from_settings, verify_signature
from helixis.rentvine.sync import refresh_one
from helixis.rentvine.webhook_deliveries import RECORDS_PER_HOUR, record_refused_delivery
from helixis.settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

#: Largest body this route will read before refusing.
#:
#: ⚠ Read BEFORE the signature check, because the signature is over the bytes —
#: so an unbounded read is work an unauthenticated caller can make us do. The
#: ceiling is generous against four small object payloads and is not a guess
#: about Rentvine: it is a bound on what a stranger can spend of ours.
MAX_BODY_BYTES = 256 * 1024

#: Refused deliveries recorded per company, per process (migration 0106).
#:
#: ⚠ Separate from ``webhook_limiter`` on purpose. That one admits 1,200 a
#: minute per IP, which is right for a door and wrong for a diagnostic table:
#: anyone who knows an account code can send refused requests, and each one
#: would otherwise be a write of up to ``MAX_BODY_BYTES``.
_record_budget = SlidingWindowCounter(requests=RECORDS_PER_HOUR, window_seconds=3600)


@router.post("/webhooks/rentvine/{account_code}", status_code=status.HTTP_200_OK)
async def rentvine_webhook(account_code: str, request: Request) -> dict[str, Any]:
    # Rate limit FIRST — cheaper than an HMAC, so a flood of bogus signatures
    # cannot make us spend one per request. Refunded below the moment a
    # delivery proves it carries this company's own key, so the ceiling can
    # only ever shed forged traffic (gotcha 114's shape, same reasoning).
    await webhook_limiter.check_by_ip(request)

    settings = get_settings()
    if not settings.rentvine.enabled:
        # The deployment-level kill switch. A company-level question is
        # answered by the credentials row below; these are different facts and
        # neither substitutes for the other (gotchas 72/73).
        logger.info("rentvine webhook for %s while the connector is off", account_code)
        return {"status": "connector_disabled"}

    raw_body = await request.body()
    if len(raw_body) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Payload too large",
        )

    row = await fetch_rentvine_row_by_account_code(account_code)
    if row is None:
        # Unknown, unconfigured, or AMBIGUOUS — the loader declines rather than
        # picking one of two companies, because filing a delivery in the wrong
        # landlord's books is a cross-tenant leak (the `helixis_twilio_numbers`
        # posture). 200 rather than 401: a non-2xx spends the sender's retry
        # budget on a delivery we will never want.
        logger.warning(
            "rentvine webhook for unknown or ambiguous account_code %r — "
            "acknowledging so the sender does not retry",
            account_code,
        )
        return {"status": "unknown_account"}

    company_id = str(row["company_id"])
    secret = decrypt_webhook_secret(row, settings)

    try:
        verify_signature(
            scheme=scheme_from_settings(settings.rentvine),
            secret=secret,
            headers=dict(request.headers),
            body=raw_body,
            path=request.url.path,
            # RAW, as sent: Rentvine signs in the URL (measured 2026-10-04).
            query=request.url.query,
        )
    except SignatureUnverifiable as exc:
        # ⚠ 401, and the log says which company it was for. This is the branch
        # the connector ships in — no scheme is configured, so every signed
        # delivery lands here until a capture settles it. That is the intended
        # state, not a broken one: polling keeps the mirror correct meanwhile.
        logger.warning(
            "rentvine webhook refused for company=%s account=%s: %s",
            company_id,
            account_code,
            exc,
        )
        # Kept verbatim so the scheme can be derived from what Rentvine really
        # sent (`rentvine_webhook_capture.py --analyse-db`). Never raises, so
        # the answer below is a 401 whether or not the row was written.
        if _record_budget.is_allowed(company_id)[0]:
            await record_refused_delivery(
                get_pool(),
                company_id=company_id,
                method=request.method,
                path=request.url.path,
                query=request.url.query,
                headers=dict(request.headers),
                body=raw_body,
                reason=str(exc),
                secret=secret,
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unverified signature",
        ) from None

    # AUTHENTIC — give the abuse budget its slot back. Placed here and nowhere
    # earlier: the branches above have verified nothing, so refunding at any of
    # them would hand the budget to traffic we cannot vouch for.
    webhook_limiter.refund_by_ip(request)

    try:
        payload = json.loads(raw_body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("body is not a JSON object")
    except (ValueError, UnicodeDecodeError) as exc:
        logger.warning("rentvine webhook body unreadable for company=%s: %s", company_id, exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid body"
        ) from None

    try:
        envelope = events.to_envelope(payload, company_id=company_id, raw_body=raw_body)
        table, path, path_params = events.refresh_target(payload, envelope)
    except events.UnmappableEvent as exc:
        # Acknowledged and DROPPED rather than given a Buildium name it might
        # not deserve. The poll is what covers it.
        logger.warning("rentvine webhook not mappable for company=%s: %s", company_id, exc)
        return {"status": "unmapped"}

    event_name = str(envelope["EventName"])
    object_key, _verb = events.identify(payload)
    id_field = events.OBJECTS[object_key]["rentvine_id"]

    refreshed = False
    detail = "no client"
    try:
        creds = decrypt_rentvine_settings_from(row, settings)
    except Exception:  # noqa: BLE001 — a decrypt failure must not 500 the receiver
        logger.exception(
            "rentvine webhook: could not open a client for company=%s — the poll "
            "is the fallback for this record",
            company_id,
        )
        creds = None

    if creds is not None:
        client = RentvineClient(creds, company_id=company_id)
        try:
            refreshed = await refresh_one(
                get_pool(),
                client,
                table=table,
                path=path,
                path_params=path_params,
                id_field=id_field,
                company_id=company_id,
            )
            detail = "refreshed" if refreshed else "refresh declined"
        except Exception:  # noqa: BLE001 — the receiver must answer 200
            # ⚠ A 500 here would spend the sender's retry budget on work the
            # next poll does anyway. The mirror is never left wrong by this
            # path — only unchanged.
            logger.exception(
                "rentvine webhook refresh crashed for company=%s %s", company_id, path_params
            )
            detail = "refresh failed"
        finally:
            await client.aclose()

    # ⚠ The activity ledger is where this delivery is recorded, NOT
    # `buildium_events`. Enqueueing there marks inbox-eligible names
    # planner-pending, and the planner's enrichers fetch from Buildium — see
    # this module's docstring. Best-effort by construction: `write_activity`
    # never raises.
    await write_activity(
        get_pool(),
        ActivityEntry(
            account_id=None,
            company_id=company_id,
            source="webhook",
            source_label=f"Rentvine · {event_name}",
            action_type=f"rentvine_{object_key}_{_verb.lower()}",
            description=(
                f"Rentvine {event_name} for {object_key} #{path_params.get(id_field)} — {detail}"
            ),
            entity_type=object_key,
            entity_id=path_params.get(id_field),
            raw={"pms": "rentvine", "refreshed": refreshed, "message_id": envelope["MessageId"]},
        ),
    )

    logger.info(
        "rentvine webhook accepted company=%s event=%s refreshed=%s",
        company_id,
        event_name,
        refreshed,
    )
    return {"event_name": event_name, "refreshed": refreshed}


__all__ = ["MAX_BODY_BYTES", "router"]
