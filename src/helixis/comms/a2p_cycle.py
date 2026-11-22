"""The A2P sweep — the worker cycle that drives a registration to done.

``a2p_pipeline`` talks to the carrier and returns column updates; this reads
rows, decides which phase each one is in, and persists what comes back. The
split is deliberate: the pipeline is testable with no database, and this is
testable with no network.

One registration's whole life, driven by repeated cycles:

    draft        -- the customer is still filling the form; not our business
    submitted    -- run the ISV chain: profile, end users, address, trust
                    product, brand, messaging service
    brand_*      -- poll; on approval, submit the campaign
    campaign_*   -- poll; on approval, buy the number and attach it
    approved     -- terminal, but still swept until a number exists
    failed       -- terminal; the customer edits and resubmits

⚠ **A FAILING COMPANY MUST NOT STOP THE OTHERS.** Each row is handled in its
own try block. One customer's rejected address is not a reason to leave every
other registration unadvanced — the same rule the webhook worker's per-event
loop follows, and the one gotcha 14 records for multi-stage Buildium fetches.

⚠ **Every write stamps ``last_attempt_at``, including the failures.** Without
it a row that raises on every cycle is retried on every cycle forever, which
is a carrier-API spend loop that nothing reports. The stamp is what makes
``pipeline.due`` a real backoff rather than a decoration.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from helixis.comms import a2p, a2p_pipeline
from helixis.comms.a2p_pipeline import A2PCredentials

logger = logging.getLogger(__name__)

#: Registrations to consider per cycle. A ceiling rather than a page: the
#: carrier rate limit is one request per second and each row costs several
#: calls, so a large batch would hold the worker for minutes.
CYCLE_LIMIT = 10

_SELECT_ACTIVE = """
    SELECT r.company_id, r.status, r.failure_reason, r.last_attempt_at,
           r.customer_profile_sid, r.trust_product_sid, r.brand_sid,
           r.messaging_service_sid, r.campaign_sid, r.step_sids,
           r.sample_messages, r.legal_business_name, r.ein, r.business_type,
           r.business_vertical, r.website, r.street, r.city, r.region,
           r.postal_code, r.iso_country, r.rep_first_name, r.rep_last_name,
           r.rep_email, r.rep_phone, r.rep_job_position, r.use_case,
           r.campaign_description, r.opt_in_description,
           (SELECT count(*) FROM public.helixis_twilio_numbers n
             WHERE n.company_id = r.company_id) AS number_count
      FROM public.helixis_a2p_registrations r
     WHERE r.status <> 'draft'
     ORDER BY r.updated_at ASC
     LIMIT $1
"""


async def run_a2p_cycle(pool: Any, creds: A2PCredentials, *, limit: int = CYCLE_LIMIT) -> int:
    """Advance every registration that is due. Returns how many were touched.

    ⚠ Deliberately GLOBAL over companies — this is a fleet janitor, not a
    tenant-scoped read, and every row it touches it touches by its own
    ``company_id`` (CLAUDE.md gotcha 47 asks that a global query say so).

    A deployment with no carrier credentials does nothing and says so once,
    rather than raising per row: an unconfigured deployment is a normal state
    before launch, not an error.
    """
    if not creds.usable:
        logger.debug("a2p sweep: no carrier credentials configured — nothing to do")
        return 0

    rows = await pool.fetch(_SELECT_ACTIVE, limit)
    touched = 0
    for record in rows:
        row = dict(record)
        company_id = str(row["company_id"])
        needs_number = int(row.get("number_count") or 0) == 0
        if not a2p_pipeline.due(row, needs_number=needs_number):
            continue
        try:
            if await _advance_one(pool, company_id, row, creds, needs_number=needs_number):
                touched += 1
        except Exception:  # noqa: BLE001 — one company must not stop the rest
            logger.exception("a2p sweep failed for company=%s", company_id)
            await _stamp_attempt(pool, company_id)
    return touched


async def _advance_one(
    pool: Any,
    company_id: str,
    row: dict[str, Any],
    creds: A2PCredentials,
    *,
    needs_number: bool,
) -> bool:
    status = row.get("status") or ""
    updates: dict[str, Any] = {}

    try:
        if status == "submitted":
            # The ISV chain, up to and including the brand. Resumable — every
            # step with a stored SID is skipped.
            updates = await a2p_pipeline.advance_submission(
                row, creds, callback_url=_callback_url(creds)
            )
        elif status in ("brand_pending", "campaign_pending"):
            updates = await a2p_pipeline.poll_status(row, creds)
        elif status == "brand_approved":
            # The brand cleared, so the campaign can go in. This is the step
            # that starts the 10-15 day clock.
            updates = await a2p_pipeline.submit_campaign(row, creds)
        elif status == a2p.APPROVED and needs_number:
            updates = {}
        else:
            return False
    except a2p.A2PError as exc:
        # The carrier's own words, kept verbatim — a paraphrase is
        # unactionable and "address could not be validated" is the single most
        # common rejection.
        logger.warning("a2p %s failed at %s for company=%s", status, exc.step, company_id)
        await _persist(
            pool,
            company_id,
            {"status": "failed", "failure_reason": exc.reason or str(exc)},
        )
        return True

    # The number is the last step and rides on the row as it stands AFTER any
    # status change this cycle produced — an approval and its number can then
    # land in the same sweep instead of waiting a whole backoff window.
    merged = {**row, **updates}
    if needs_number and merged.get("status") == a2p.APPROVED:
        try:
            number = await a2p_pipeline.ensure_number(company_id, merged, creds, existing_numbers=0)
            if number:
                await _record_number(pool, company_id, number)
        except Exception:  # noqa: BLE001
            # ⚠ Never let this fail the status write below. The registration
            # IS approved; losing that fact because a purchase failed would
            # re-run the whole poll next cycle and report the customer as
            # still pending when they are not.
            logger.exception("a2p: could not provision a number for company=%s", company_id)

    await _persist(pool, company_id, updates)
    return True


def _callback_url(creds: A2PCredentials) -> str:
    """Where the carrier posts profile status.

    ⚠ Without it there is no ``failure_reason`` on an address validation
    rejection — the most common one — so the API treats it as optional and we
    do not. An empty base URL yields an empty string, which the carrier
    accepts; the consequence is a rejection we cannot explain, not a failure.

    ⚠ **This pointed at ``/api/v1/twilio/status`` until 2026-09-06, which is the
    SMS DELIVERY-RECEIPT route.** That handler's first act is to read
    ``MessageSid``; a Trust Hub callback carries none, so it warned and
    returned 204 — the paragraph above described a guarantee the deployment did
    not have, because the URL resolved to a handler that could only throw the
    reason away. One number, two handlers, and the wrong one wired (gotcha 68).
    ``/a2p-status`` is the handler that records it.
    """
    return f"{creds.webhook_base_url}/api/v1/twilio/a2p-status" if creds.webhook_base_url else ""


async def _persist(pool: Any, company_id: str, updates: dict[str, Any]) -> None:
    """Write the returned columns, always stamping the attempt.

    ⚠ The stamp happens even when ``updates`` is empty — an unchanged poll IS
    an attempt, and not recording it means the backoff never advances and the
    row is re-polled every cycle forever.
    """
    allowed = (
        "status",
        "failure_reason",
        "customer_profile_sid",
        "trust_product_sid",
        "brand_sid",
        "messaging_service_sid",
        "campaign_sid",
        "step_sids",
        "brand_approved_at",
        "campaign_approved_at",
    )
    sets: list[str] = []
    values: list[Any] = [company_id]
    for column in allowed:
        if column in updates:
            values.append(updates[column])
            cast = "::jsonb" if column == "step_sids" else ""
            sets.append(f"{column} = ${len(values)}{cast}")
    values.append(datetime.now(UTC))
    sets.append(f"last_attempt_at = ${len(values)}")
    sets.append("updated_at = now()")
    await pool.execute(
        "UPDATE public.helixis_a2p_registrations SET "
        + ", ".join(sets)
        + " WHERE company_id = $1::uuid",
        *values,
    )


async def _stamp_attempt(pool: Any, company_id: str) -> None:
    """Back off after an unexpected error, so a broken row is not a hot loop."""
    try:
        await pool.execute(
            "UPDATE public.helixis_a2p_registrations "
            "SET last_attempt_at = $2, updated_at = now() WHERE company_id = $1::uuid",
            company_id,
            datetime.now(UTC),
        )
    except Exception:  # noqa: BLE001 — best effort; the sweep must not die here
        logger.exception("a2p: could not stamp an attempt for company=%s", company_id)


async def _record_number(pool: Any, company_id: str, number: str) -> None:
    """⚠ ON CONFLICT, not a plain insert. The number is already BOUGHT by the
    time this runs; a unique violation raised here would charge the account
    and record nothing, which is the one outcome with no recovery path."""
    await pool.execute(
        "INSERT INTO public.helixis_twilio_numbers (company_id, phone_number, label) "
        "VALUES ($1::uuid, $2, $3) "
        "ON CONFLICT (phone_number) DO UPDATE SET updated_at = now()",
        company_id,
        number,
        "Provisioned on approval",
    )
    logger.info("a2p: provisioned %s for company=%s", number, company_id)
