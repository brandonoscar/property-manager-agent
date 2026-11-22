"""CRM email-lead ingestion — selection gate, dedup, and the poll cycle.

The pure gate (`select_lead_emails`) is tested directly; the poll cycle is
tested with the Composio client + repo functions mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from helixis.webhooks.crm_email_poll import run_crm_lead_poll_cycle
from helixis.webhooks.crm_lead_ingest import ingest_email_as_lead, select_lead_emails
from helixis.webhooks.email_triage import InboundEmail, normalize_contact_set


def _email(
    *, from_email: str, subject: str = "", body: str = "", message_id: str = "m1"
) -> InboundEmail:
    return InboundEmail(
        message_id=message_id,
        from_email=from_email,
        subject=subject,
        body_text=body,
        received_at="2026-07-04T12:00:00Z",
    )


# ── select_lead_emails (pure gate) ───────────────────────────────────


def test_keeps_listing_sender_and_inquiry():
    emails = [
        _email(from_email="jordan@convo.zillow.com", subject="New contact", message_id="a"),
        _email(
            from_email="stranger@gmail.com", subject="Is the unit still available?", message_id="b"
        ),
    ]
    kept = select_lead_emails(emails, frozenset())
    assert {e.message_id for e in kept} == {"a", "b"}


def test_drops_known_contact_and_non_inquiry():
    contacts = normalize_contact_set(["tenant@example.com"])
    emails = [
        # existing contact asking an inquiry-shaped question → NOT a lead
        _email(from_email="tenant@example.com", subject="Is the unit available?", message_id="a"),
        # unknown sender, no inquiry signal → NOT a lead
        _email(from_email="news@marketing.com", subject="Weekly digest", message_id="b"),
    ]
    assert select_lead_emails(emails, contacts) == []


def test_skips_already_seen_ids():
    emails = [_email(from_email="jordan@convo.zillow.com", subject="hi", message_id="seen-1")]
    kept = select_lead_emails(emails, frozenset(), seen_ids=frozenset({"seen-1"}))
    assert kept == []


# ── ingest_email_as_lead (find-or-create dedup) ──────────────────────


async def test_ingest_creates_new_lead():
    email = _email(from_email="jordan@convo.zillow.com", subject="Interested in 4B")
    insert_lead = AsyncMock(return_value="new-lead-1")
    with (
        patch(
            "helixis.webhooks.crm_lead_ingest.leads_repo.find_lead_by_email",
            AsyncMock(return_value=None),
        ),
        patch("helixis.webhooks.crm_lead_ingest.leads_repo.insert_lead", insert_lead),
    ):
        lead_id = await ingest_email_as_lead(MagicMock(), company_id="co-1", email=email)
    assert lead_id == "new-lead-1"
    insert_lead.assert_awaited_once()


async def test_ingest_dedups_to_existing_lead():
    from helixis.db.repositories.leads import LeadRow

    email = _email(from_email="jordan@convo.zillow.com", subject="Following up")
    existing = LeadRow(
        id="existing-1",
        company_id="co-1",
        name="Jordan",
        kind="renter",
        stage="new",
        source="zillow",
    )
    insert_lead = AsyncMock(return_value="should-not-be-called")
    with (
        patch(
            "helixis.webhooks.crm_lead_ingest.leads_repo.find_lead_by_email",
            AsyncMock(return_value=existing),
        ),
        patch("helixis.webhooks.crm_lead_ingest.leads_repo.insert_lead", insert_lead),
    ):
        lead_id = await ingest_email_as_lead(MagicMock(), company_id="co-1", email=email)
    assert lead_id == "existing-1"
    insert_lead.assert_not_called()  # dedup — no duplicate lead


# ── the poll cycle (wiring, mocked) ──────────────────────────────────


async def test_poll_cycle_ingests_lead_emails():
    composio = MagicMock()
    composio.available = True
    composio.is_connected.return_value = True
    composio.execute_tool.return_value = {"messages": ["raw"]}  # shape irrelevant (parser mocked)

    parsed = [
        _email(from_email="jordan@convo.zillow.com", subject="Available?", message_id="a"),
        _email(from_email="news@marketing.com", subject="Digest", message_id="b"),  # not a lead
    ]
    ingest = AsyncMock(return_value="lead-x")

    with (
        patch(
            "helixis.webhooks.crm_email_poll.list_poll_targets",
            AsyncMock(return_value=[("user-1", "co-1")]),
        ),
        patch(
            "helixis.webhooks.crm_email_poll.fetch_contact_emails",
            AsyncMock(return_value=frozenset()),
        ),
        patch("helixis.webhooks.crm_email_poll.parse_gmail_fetch_result", return_value=parsed),
        patch("helixis.webhooks.crm_email_poll.ingest_email_as_lead", ingest),
    ):
        n = await run_crm_lead_poll_cycle(pool=MagicMock(), composio=composio, api=MagicMock())

    assert n == 1  # only the zillow lead; the newsletter was gated out
    ingest.assert_awaited_once()
    assert ingest.await_args.kwargs["company_id"] == "co-1"


async def test_poll_cycle_noops_without_composio():
    n = await run_crm_lead_poll_cycle(pool=MagicMock(), composio=None, api=MagicMock())
    assert n == 0
