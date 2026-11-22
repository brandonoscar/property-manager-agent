"""The lead-side half of an inbound text, callable from either webhook route.

**Why this module exists.** A Twilio number has exactly ONE "A message comes
in" URL, and this deployment shipped two handlers that both needed it:

* ``POST /api/v1/twilio/inbound`` — honours STOP/START/HELP against the
  consent ledger, logs every message, bridges it to the Inbox. The TCPA
  compliance lane.
* ``POST /api/v1/crm/sms/inbound`` — find-or-creates a lead and logs the
  message to its thread. The Leasing lane.

Whichever URL the console named, the other lane silently did nothing. Point it
at the CRM and a resident's STOP is never honoured — we keep texting somebody
who revoked, which is the violation carriers audit for. Point it at the
consent route and no lead ever arrives, so Leasing is dead.

The fix is not to choose. This module holds the lead-side work so BOTH routes
can do the whole job, and a misconfigured console costs nothing. Consent is
always applied first — it is the half whose omission is unlawful rather than
merely unhelpful.

⚠ **Never raises.** Twilio retries the entire delivery on any non-2xx, and a
retry re-runs the consent mirroring in the caller. A lead that failed to
create is recoverable by a human; a redelivery storm against the consent
ledger is not worth risking for it.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def handle_inbound_lead_sms(
    params: dict[str, str],
    *,
    settings: Any,
    pool: Any,
    company_id: str,
    from_number: str,
    body: str,
    message_sid: str | None,
) -> str | None:
    """Find-or-create the lead, log the message, triage any photo.

    Returns the lead id, or ``None`` when nothing was created — including
    every failure path, which is logged rather than raised.

    Imports are function-local on purpose: ``api/routes/crm.py`` owns the lead
    repository wiring, and importing it at module scope from here would create
    an import cycle with the route that also calls this.
    """
    if not company_id or not from_number:
        return None

    # Imported here, not at module scope — see the docstring.
    from helixis.api.routes.crm import _ingest_inbound_sms
    from helixis.webhooks import sms_photo_triage
    from helixis.webhooks.sms_photo_triage import first_image_media

    lead_id: str | None = None
    try:
        lead_id = await _ingest_inbound_sms(
            pool=pool,
            company_id=company_id,
            from_number=from_number,
            body=body,
            external_id=message_sid,
        )
    except Exception:  # noqa: BLE001 — a failed lead must not retry the delivery
        logger.warning("inbound lead ingest failed for company=%s", company_id, exc_info=True)

    # #533 entry point 1 — a texted photo (a leak, a broken appliance) is
    # assessed into a DRAFT work order. Purely additive and best-effort: it
    # never changes the response, so a vision hiccup can't make Twilio retry.
    try:
        if first_image_media(params) is not None:
            await sms_photo_triage.triage_inbound_photo(
                params,
                settings=settings,
                pool=pool,
                company_id=company_id,
                from_number=from_number,
            )
    except Exception:  # noqa: BLE001 — same reasoning as above
        logger.warning("inbound photo triage failed for company=%s", company_id, exc_info=True)

    return lead_id


__all__ = ["handle_inbound_lead_sms"]
