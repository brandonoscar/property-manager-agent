"""The Gmail poll cycle — the live seam that ties the email-ingest pieces
together and runs inside the webhook worker.

Once per cycle, for each (user, company) that has an ACTIVE Gmail
connection: fetch recent messages via Composio, keep the ones from a
known Buildium contact that are long, and enqueue each into
``buildium_events`` with the email mapped onto the EnrichedEvent task
shape + a deterministic floor surface. From there the EXISTING planner +
summarizer queues enrich it exactly like a Buildium event — the whole
point of "basically the one we already have for Buildium webhooks".

The pure decision logic is in ``email_triage`` / ``email_ingest`` (all
unit-tested). This module is the orchestration: it calls Composio, the
Buildium API, and the DB. It is fail-soft per user — one user's Gmail
fetch erroring never stops the others, and the worst case is a message
re-examined next cycle (the dedup_key unique index + the seen-ledger
both guard against double-surfacing).

⚠ Live seams to confirm on first smoke (cannot be verified offline):
the Composio Gmail tool slug + fetch arguments, the result shape
(``email_ingest.parse_gmail_fetch_result`` is tolerant), and the poll
cadence. These are isolated to the two constants below + the parse path.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from helixis.common.company_clock import company_timezone, today_in
from helixis.db.repositories.gmail_poll_state import (
    get_seen_message_ids,
    list_poll_targets,
    mark_messages_seen,
)
from helixis.db.repositories.promises import record_promise
from helixis.reminders.promises import extract_promise_date
from helixis.webhooks.email_ingest import (
    build_email_event_payload,
    email_event_time,
    fetch_contact_emails,
    parse_gmail_fetch_result,
    parse_outlook_fetch_result,
    select_emails_to_enqueue,
)
from helixis.webhooks.email_photo_triage import triage_email_photos
from helixis.webhooks.email_triage import build_email_action_surface, build_email_enrichment
from helixis.webhooks.queue import enqueue, store_enrichment

if TYPE_CHECKING:
    from collections.abc import Callable

    import asyncpg

    from helixis.buildium.api import BuildiumApi
    from helixis.connectors.composio_client import ComposioClient

logger = logging.getLogger(__name__)

# Composio Gmail tool slug for a read fetch. ⚠ confirm against the live
# toolkit on first smoke — Composio occasionally renames slugs.
GMAIL_FETCH_SLUG = "GMAIL_FETCH_EMAILS"

# Fetch arguments. Bound the window so a poll never pulls the whole
# mailbox; the seen-ledger means we only act on genuinely new mail. Both
# are tunable knobs, not contracts.
GMAIL_FETCH_ARGS = {"max_results": 25, "query": "in:inbox newer_than:3d"}

# Composio Outlook list-messages slug + args. ⚠ NEW live seams — confirm
# against the live toolkit on first smoke with a connected Outlook account
# (Composio slug names + the exact arg keys are not verifiable offline).
# Outlook rides Microsoft Graph, so there's no Gmail-style query string;
# bound the page size and let the seen-ledger dedup.
OUTLOOK_FETCH_SLUG = "OUTLOOK_LIST_MESSAGES"
OUTLOOK_FETCH_ARGS = {"top": 25}

# Mailbox providers swept each cycle: (composio toolkit slug for the
# is_connected check, fetch tool slug, fetch args, result parser). Gmail is
# the original; Outlook rides the SAME enqueue → enrich → plan → Inbox
# pipeline once parsed into InboundEmail. Per-user, only connected providers
# actually fetch (the rest no-op).
_PROVIDERS: tuple[tuple[str, str, dict, Callable[[dict | None], list]], ...] = (
    ("gmail", GMAIL_FETCH_SLUG, GMAIL_FETCH_ARGS, parse_gmail_fetch_result),
    ("outlook", OUTLOOK_FETCH_SLUG, OUTLOOK_FETCH_ARGS, parse_outlook_fetch_result),
)

# No real Buildium AccountId for an email; the company UUID is what the
# Inbox query filters on, so a 0 sentinel for the int column is fine.
_EMAIL_ACCOUNT_SENTINEL = 0


def _allow_all_senders() -> bool:
    """TESTING override. When set, the poll surfaces any long email from a
    real sender, skipping the known-Buildium-contact gate — so the whole
    Gmail → triage → plan → Inbox → reply chain can be validated BEFORE a
    Buildium key exists. Default OFF; the shipped product gates on
    Buildium contacts."""
    return os.getenv("HELIXIS_EMAIL_INGEST_ALLOW_ALL_SENDERS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


async def run_email_poll_cycle(
    *,
    pool: asyncpg.Pool,
    composio: ComposioClient | None,
    api: BuildiumApi,
) -> int:
    """Run one Gmail poll cycle across all connected users. Returns the
    number of emails enqueued this cycle (0 when Composio is unconfigured
    or nobody has Gmail connected). Never raises — all failures are
    contained per user and logged."""
    if composio is None or not composio.available:
        return 0

    targets = await list_poll_targets(pool)
    if not targets:
        return 0

    allow_all = _allow_all_senders()

    # The known-contact set is company-scoped. The worker runs one shared
    # Buildium client today (single-company deploy), so resolve it once
    # per cycle rather than per user. When the worker goes per-company,
    # move this inside the loop keyed by company. Skipped entirely under
    # the allow-all testing override (no Buildium needed to validate).
    contact_emails: frozenset[str] = frozenset()
    if not allow_all:
        try:
            contact_emails = await fetch_contact_emails(api)
        except Exception:  # noqa: BLE001
            logger.exception("email poll: contact fetch failed — skipping cycle")
            return 0
        if not contact_emails:
            # Fail-closed: with no known contacts we'd surface nothing anyway.
            return 0
    else:
        logger.warning(
            "email poll: ALLOW_ALL_SENDERS override ON — surfacing any long "
            "email regardless of Buildium contacts (testing only)"
        )

    total_enqueued = 0
    for user_id, company_id in targets:
        # Poll EACH connected mailbox provider for this user. A property
        # manager usually has one; _poll_one_user no-ops cleanly on a
        # provider the user hasn't connected. Per-provider failures are
        # contained so Gmail still polls if Outlook errors, and vice-versa.
        for toolkit, slug, args, parser in _PROVIDERS:
            try:
                total_enqueued += await _poll_one_user(
                    pool=pool,
                    composio=composio,
                    user_id=user_id,
                    company_id=company_id,
                    contact_emails=contact_emails,
                    allow_all_senders=allow_all,
                    toolkit=toolkit,
                    fetch_slug=slug,
                    fetch_args=args,
                    parser=parser,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "email poll: user %s provider %s failed — continuing", user_id, toolkit
                )
    if total_enqueued:
        logger.info("email poll: enqueued %d new email(s)", total_enqueued)
    return total_enqueued


async def _poll_one_user(
    *,
    pool: asyncpg.Pool,
    composio: ComposioClient,
    user_id: str,
    company_id: str,
    contact_emails: frozenset[str],
    allow_all_senders: bool = False,
    toolkit: str = "gmail",
    fetch_slug: str = GMAIL_FETCH_SLUG,
    fetch_args: dict | None = None,
    parser: Callable[[dict | None], list] = parse_gmail_fetch_result,
) -> int:
    """Fetch + filter + enqueue for a single user's ONE mailbox provider.
    Returns count enqueued. No-ops (returns 0) when the user hasn't
    connected ``toolkit``."""
    if not composio.is_connected(user_id, toolkit):
        return 0

    result = composio.execute_tool(
        user_id=user_id,
        slug=fetch_slug,
        arguments=dict(fetch_args if fetch_args is not None else GMAIL_FETCH_ARGS),
    )
    if result is None:
        return 0

    messages = parser(result)
    if not messages:
        return 0

    seen = await get_seen_message_ids(pool, user_id)
    to_enqueue = select_emails_to_enqueue(
        messages, contact_emails, already_seen=seen, allow_all_senders=allow_all_senders
    )

    enqueued = 0
    for email in to_enqueue:
        try:
            await _enqueue_email(pool, email, company_id)
            enqueued += 1
        except Exception:  # noqa: BLE001
            logger.exception("email poll: enqueue failed for message %s", email.message_id)

    # #533 entry point 3: assess image attachments on surfaced emails → draft
    # work order. Gmail only (GMAIL_GET_ATTACHMENT is Gmail-specific; Outlook
    # rides a different fetch tool). Best-effort — a maintenance draft is a
    # bonus on top of ingest; a failure here never touches the enqueue path.
    if toolkit == "gmail":
        try:
            drafted = await triage_email_photos(
                pool,
                composio,
                user_id=user_id,
                company_id=company_id,
                emails=to_enqueue,
            )
            if drafted:
                logger.info("email poll: drafted %d work order(s) from email photos", drafted)
        except Exception:  # noqa: BLE001 — defensive; triage is already best-effort
            logger.exception("email poll: photo triage failed for user %s", user_id)

    # Reminders v1.5: a surfaced email (known contact in prod) that carries
    # a deterministic payment promise ("will pay the 17th") records it, so
    # the rent_missing card stays quiet until the promised date passes.
    await _record_payment_promises(pool, company_id, to_enqueue)

    # Record EVERY examined message (not just surfaced) so non-qualifying
    # mail isn't re-examined forever; flag which ones surfaced.
    await mark_messages_seen(
        pool,
        user_id,
        company_id,
        surfaced_ids={e.message_id for e in to_enqueue},
        examined_ids={m.message_id for m in messages},
    )
    return enqueued


async def _record_payment_promises(pool, company_id: str, emails: list) -> None:  # noqa: ANN001
    """Best-effort promise extraction over the emails surfaced this cycle.
    A failure here never touches the enqueue path — a missed promise just
    means the normal rent reminder fires."""
    # ⚠ The company's date, not the container's: "will pay the 17th" is resolved
    # relative to today, so on the wrong day it can land in the wrong month and
    # suppress (or fail to suppress) a rent reminder (migration 0098).
    today = today_in(await company_timezone(pool, company_id))
    for email in emails:
        try:
            promised = extract_promise_date(f"{email.subject}\n{email.body_text}", today=today)
            if promised is not None:
                await record_promise(
                    pool,
                    company_id=company_id,
                    email=email.from_email,
                    promised_date=promised,
                    source_message_id=email.message_id,
                )
                logger.info(
                    "email poll: payment promise %s recorded from %s",
                    promised.isoformat(),
                    email.from_email,
                )
        except Exception:  # noqa: BLE001
            logger.exception("promise record failed for message %s", email.message_id)


async def _enqueue_email(pool: asyncpg.Pool, email, company_id: str) -> None:  # noqa: ANN001
    """Insert one email row + attach its enrichment and floor surface so
    the existing planner/summarizer queues pick it up."""
    payload = build_email_event_payload(email, _EMAIL_ACCOUNT_SENTINEL)
    result = await enqueue(
        pool,
        event_name=payload["EventName"],
        event_time=email_event_time(email),
        account_id=_EMAIL_ACCOUNT_SENTINEL,
        entity_ids={},
        payload=payload,
        company_id=company_id,
    )
    if result.duplicate:
        return
    # Attach the email→task enrichment (so planner/summarizer run on it)
    # and the deterministic floor surface (so the Inbox shows the full
    # email immediately, before the planner's signals/draft land).
    await store_enrichment(
        pool,
        result.id,
        enrichment=build_email_enrichment(email, company_id),
        action_surface=build_email_action_surface(email),
    )


__all__ = ["run_email_poll_cycle"]
