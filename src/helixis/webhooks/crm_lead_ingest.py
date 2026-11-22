"""CRM email-lead ingestion — the deterministic core.

Inbound Gmail emails from NEW senders (listing-site inquiries, web-form
notifications, direct inquiries) become CRM leads. This is the INVERSE of the
Inbox email ingest (``email_triage.should_surface``): that keeps LONG emails
from KNOWN Buildium contacts; this keeps inquiry-shaped emails from people who
are NOT yet in the book (``lead_triage.is_lead``). The two gates are mutually
exclusive on the contact dimension, so an email is a lead XOR an Inbox task.

The selection is a pure function (no network/DB) so it's unit-testable; the
poll cycle and the ``/crm/email/simulate`` test route both call it, then use
:func:`ingest_email_as_lead` to persist. Gated by
``settings.crm.email_ingest_enabled``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import asyncpg

from helixis.db.repositories import leads as leads_repo
from helixis.webhooks.email_triage import InboundEmail, normalize_email_address
from helixis.webhooks.lead_triage import build_lead_from_email, is_lead

logger = logging.getLogger(__name__)


def select_lead_emails(
    emails: Iterable[InboundEmail],
    known_contact_emails: frozenset[str],
    *,
    seen_ids: frozenset[str] = frozenset(),
) -> list[InboundEmail]:
    """Keep the emails that are CRM leads and haven't been processed yet.

    A lead is a message from someone NOT already a Buildium contact that
    looks like an inquiry (listing-site sender or inquiry keywords — see
    ``lead_triage.is_lead``). Already-seen message ids are dropped so a poll
    processes each message once.
    """
    out: list[InboundEmail] = []
    for email in emails:
        if email.message_id and email.message_id in seen_ids:
            continue
        if is_lead(email, known_contact_emails):
            out.append(email)
    return out


async def ingest_email_as_lead(
    pool: asyncpg.Pool, *, company_id: str, email: InboundEmail
) -> str | None:
    """Find-or-create a lead by email address, returning its id. A repeat
    inquiry from the same address maps to the EXISTING lead (no duplicate);
    a new address creates a lead via ``build_lead_from_email``."""
    addr = normalize_email_address(email.from_email)
    if addr:
        existing = await leads_repo.find_lead_by_email(pool=pool, company_id=company_id, email=addr)
        if existing is not None and existing.id:
            return existing.id
    new_lead = build_lead_from_email(email, company_id)
    return await leads_repo.insert_lead(pool, new_lead)
