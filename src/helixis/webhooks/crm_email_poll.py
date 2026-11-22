"""The Gmail→CRM lead poll cycle — runs inside the webhook worker.

Once per cycle, for each (user, company) with an ACTIVE Gmail connection:
fetch recent messages via Composio, keep the ones that look like a NEW lead
(``lead_triage.is_lead`` — a listing-site sender or inquiry-shaped email from
someone NOT already a Buildium contact), and insert each as a ``crm_lead``.
This is the INVERSE of the Inbox email poll (which keeps long emails FROM
known contacts); the two gates are mutually exclusive on the contact
dimension.

Reuses the email-ingest seams verbatim: ``list_poll_targets`` (user→company),
``composio.is_connected`` / ``execute_tool``, ``parse_gmail_fetch_result``,
and ``fetch_contact_emails`` (here used to EXCLUDE existing contacts). The
pure gate + the find-or-create insert live in
``crm_lead_ingest``.

Dedup: unlike the Inbox poll, this does NOT use a seen-ledger — sharing the
Inbox's ``helixis_gmail_seen`` would collide (whichever poll ran first would
mark a message seen and starve the other). Instead idempotency comes from
``find_lead_by_email`` in ``ingest_email_as_lead``: re-examining the same
recent mail each cycle can't create a duplicate lead. Re-examining ~25 recent
messages every couple minutes is cheap.

Fail-soft per user — one user's fetch erroring never stops the others.
Never raises. Gated by ``settings.crm.email_ingest_enabled``.

⚠ Live seams to confirm on first smoke (same as email_poll): the Composio
Gmail slug + fetch args + result shape.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from helixis.db.repositories.gmail_poll_state import list_poll_targets
from helixis.webhooks.crm_lead_ingest import ingest_email_as_lead, select_lead_emails
from helixis.webhooks.email_ingest import fetch_contact_emails, parse_gmail_fetch_result

if TYPE_CHECKING:
    import asyncpg

    from helixis.buildium.api import BuildiumApi
    from helixis.connectors.composio_client import ComposioClient

logger = logging.getLogger(__name__)

# Same read fetch as the Inbox poll (a tunable knob, not a contract).
GMAIL_FETCH_SLUG = "GMAIL_FETCH_EMAILS"
GMAIL_FETCH_ARGS = {"max_results": 25, "query": "in:inbox newer_than:3d"}


async def run_crm_lead_poll_cycle(
    *,
    pool: asyncpg.Pool,
    composio: ComposioClient | None,
    api: BuildiumApi,
) -> int:
    """Run one Gmail→CRM lead poll cycle across all connected users. Returns
    the number of lead emails ingested this cycle. Never raises — failures
    are contained per user and logged."""
    if composio is None or not composio.available:
        return 0

    targets = await list_poll_targets(pool)
    if not targets:
        return 0

    # Known Buildium contacts are used to EXCLUDE existing tenants/owners/
    # vendors (a lead is someone NOT yet in the book). Best-effort: an empty
    # set means "exclude nobody", which is SAFE for leads — so, unlike the
    # Inbox poll, we do NOT fail-closed when there are no contacts (that's the
    # normal pre-Buildium state).
    contact_emails: frozenset[str] = frozenset()
    try:
        contact_emails = await fetch_contact_emails(api)
    except Exception:  # noqa: BLE001
        logger.warning("crm lead poll: contact fetch failed — proceeding with no exclusions")

    total = 0
    for user_id, company_id in targets:
        try:
            total += await _poll_one_user_for_leads(
                pool=pool,
                composio=composio,
                user_id=user_id,
                company_id=company_id,
                contact_emails=contact_emails,
            )
        except Exception:  # noqa: BLE001
            logger.exception("crm lead poll: user %s failed — continuing", user_id)
    if total:
        logger.info("crm lead poll: ingested %d lead email(s)", total)
    return total


async def _poll_one_user_for_leads(
    *,
    pool: asyncpg.Pool,
    composio: ComposioClient,
    user_id: str,
    company_id: str,
    contact_emails: frozenset[str],
) -> int:
    """Fetch + gate + insert leads for one user's Gmail. Returns the count of
    lead emails ingested. No-ops when the user hasn't connected Gmail."""
    if not composio.is_connected(user_id, "gmail"):
        return 0

    result = composio.execute_tool(
        user_id=user_id, slug=GMAIL_FETCH_SLUG, arguments=dict(GMAIL_FETCH_ARGS)
    )
    if result is None:
        return 0

    messages = parse_gmail_fetch_result(result)
    if not messages:
        return 0

    lead_emails = select_lead_emails(messages, contact_emails)
    ingested = 0
    for email in lead_emails:
        try:
            lead_id = await ingest_email_as_lead(pool, company_id=company_id, email=email)
            if lead_id:
                ingested += 1
        except Exception:  # noqa: BLE001
            logger.exception("crm lead poll: ingest failed for message %s", email.message_id)
    return ingested


__all__ = ["run_crm_lead_poll_cycle"]
