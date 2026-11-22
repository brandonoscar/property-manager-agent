"""Orchestration test for the Gmail poll cycle.

Everything external (Composio, Buildium API, DB repo + queue) is mocked;
we assert the wiring: connected users get fetched, surfaced emails get
enqueued + enriched, and every examined message is marked seen.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import helixis.webhooks.email_poll as ep

LONG = " ".join(["word"] * 200)


def _fetch_result(*msgs: dict) -> dict:
    return {"messages": list(msgs)}


def _msg(mid: str, sender: str, body: str = LONG) -> dict:
    return {
        "messageId": mid,
        "threadId": f"t-{mid}",
        "sender": sender,
        "subject": "Hello",
        "messageText": body,
        "messageTimestamp": "2026-06-27T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_poll_cycle_enqueues_surfaced_and_marks_all_seen(monkeypatch) -> None:
    # One target user, Gmail connected, fetch returns a known-long email
    # (surfaces) and a stranger email (doesn't).
    monkeypatch.setattr(ep, "list_poll_targets", AsyncMock(return_value=[("u1", "co1")]))
    monkeypatch.setattr(
        ep, "fetch_contact_emails", AsyncMock(return_value=frozenset({"known@x.com"}))
    )
    monkeypatch.setattr(ep, "get_seen_message_ids", AsyncMock(return_value=frozenset()))
    mark_seen = AsyncMock()
    monkeypatch.setattr(ep, "mark_messages_seen", mark_seen)
    enqueue = AsyncMock(return_value=MagicMock(id="evt-1", duplicate=False))
    monkeypatch.setattr(ep, "enqueue", enqueue)
    store = AsyncMock()
    monkeypatch.setattr(ep, "store_enrichment", store)

    composio = MagicMock()
    composio.available = True
    # Provider-aware: this user has Gmail connected, not Outlook — so the
    # poll's Outlook pass no-ops and only the Gmail path runs.
    composio.is_connected.side_effect = lambda _uid, toolkit: toolkit == "gmail"
    composio.execute_tool.return_value = _fetch_result(
        _msg("good", "known@x.com"),
        _msg("spam", "stranger@x.com"),
    )

    n = await ep.run_email_poll_cycle(pool=MagicMock(), composio=composio, api=MagicMock())

    assert n == 1  # only the known+long email enqueued
    assert enqueue.await_count == 1
    assert store.await_count == 1  # enrichment + floor surface attached
    # Both messages recorded as examined; only the good one surfaced.
    _, kwargs = mark_seen.await_args
    assert kwargs["surfaced_ids"] == {"good"}
    assert kwargs["examined_ids"] == {"good", "spam"}


def _graph_result(*msgs: dict) -> dict:
    return {"value": list(msgs)}


def _graph_msg(mid: str, address: str, body: str = LONG) -> dict:
    return {
        "id": mid,
        "subject": "Hello",
        "bodyPreview": body,
        "from": {"emailAddress": {"name": "Known", "address": address}},
        "receivedDateTime": "2026-06-27T00:00:00Z",
        "conversationId": f"c-{mid}",
    }


@pytest.mark.asyncio
async def test_poll_cycle_ingests_outlook_when_connected(monkeypatch) -> None:
    """An Outlook-connected user (no Gmail) gets polled via the Outlook
    pass: the Graph result is parsed + the known-long email enqueued."""
    monkeypatch.setattr(ep, "list_poll_targets", AsyncMock(return_value=[("u1", "co1")]))
    monkeypatch.setattr(
        ep, "fetch_contact_emails", AsyncMock(return_value=frozenset({"known@x.com"}))
    )
    monkeypatch.setattr(ep, "get_seen_message_ids", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(ep, "mark_messages_seen", AsyncMock())
    enqueue = AsyncMock(return_value=MagicMock(id="evt-1", duplicate=False))
    monkeypatch.setattr(ep, "enqueue", enqueue)
    monkeypatch.setattr(ep, "store_enrichment", AsyncMock())

    composio = MagicMock()
    composio.available = True
    # Outlook connected, Gmail not — only the Outlook pass should fetch.
    composio.is_connected.side_effect = lambda _uid, toolkit: toolkit == "outlook"
    composio.execute_tool.return_value = _graph_result(_graph_msg("o1", "known@x.com"))

    n = await ep.run_email_poll_cycle(pool=MagicMock(), composio=composio, api=MagicMock())

    assert n == 1
    assert enqueue.await_count == 1
    # The single fetch used the Outlook slug, not Gmail.
    _, kwargs = composio.execute_tool.call_args
    assert kwargs["slug"] == ep.OUTLOOK_FETCH_SLUG


@pytest.mark.asyncio
async def test_poll_cycle_triages_photo_attachments_on_gmail(monkeypatch) -> None:
    """A surfaced Gmail email carrying an image attachment runs through the
    #533 photo triage; a non-Gmail (Outlook) path does not."""
    monkeypatch.setattr(ep, "list_poll_targets", AsyncMock(return_value=[("u1", "co1")]))
    monkeypatch.setattr(
        ep, "fetch_contact_emails", AsyncMock(return_value=frozenset({"known@x.com"}))
    )
    monkeypatch.setattr(ep, "get_seen_message_ids", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(ep, "mark_messages_seen", AsyncMock())
    monkeypatch.setattr(ep, "enqueue", AsyncMock(return_value=MagicMock(id="e1", duplicate=False)))
    monkeypatch.setattr(ep, "store_enrichment", AsyncMock())
    triage = AsyncMock(return_value=1)
    monkeypatch.setattr(ep, "triage_email_photos", triage)

    msg = _msg("good", "known@x.com")
    msg["attachmentList"] = [
        {"attachmentId": "a1", "filename": "leak.jpg", "mimeType": "image/jpeg"}
    ]

    composio = MagicMock()
    composio.available = True
    composio.is_connected.side_effect = lambda _uid, toolkit: toolkit == "gmail"
    composio.execute_tool.return_value = _fetch_result(msg)

    n = await ep.run_email_poll_cycle(pool=MagicMock(), composio=composio, api=MagicMock())

    assert n == 1
    triage.assert_awaited_once()
    _, kwargs = triage.await_args
    # Triage gets the surfaced email with its parsed attachment ref.
    assert kwargs["user_id"] == "u1"
    assert kwargs["company_id"] == "co1"
    assert len(kwargs["emails"]) == 1
    assert kwargs["emails"][0].attachments[0].attachment_id == "a1"


@pytest.mark.asyncio
async def test_poll_cycle_photo_triage_failure_never_breaks_poll(monkeypatch) -> None:
    """Photo triage raising must not sink the enqueue result."""
    monkeypatch.setattr(ep, "list_poll_targets", AsyncMock(return_value=[("u1", "co1")]))
    monkeypatch.setattr(
        ep, "fetch_contact_emails", AsyncMock(return_value=frozenset({"known@x.com"}))
    )
    monkeypatch.setattr(ep, "get_seen_message_ids", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(ep, "mark_messages_seen", AsyncMock())
    monkeypatch.setattr(ep, "enqueue", AsyncMock(return_value=MagicMock(id="e1", duplicate=False)))
    monkeypatch.setattr(ep, "store_enrichment", AsyncMock())
    monkeypatch.setattr(
        ep, "triage_email_photos", AsyncMock(side_effect=RuntimeError("gemini down"))
    )

    msg = _msg("good", "known@x.com")
    msg["attachmentList"] = [{"attachmentId": "a1", "mimeType": "image/jpeg"}]
    composio = MagicMock()
    composio.available = True
    composio.is_connected.side_effect = lambda _uid, toolkit: toolkit == "gmail"
    composio.execute_tool.return_value = _fetch_result(msg)

    n = await ep.run_email_poll_cycle(pool=MagicMock(), composio=composio, api=MagicMock())
    assert n == 1  # enqueue still succeeded despite triage blowing up


@pytest.mark.asyncio
async def test_poll_cycle_noop_when_composio_unavailable() -> None:
    composio = MagicMock()
    composio.available = False
    n = await ep.run_email_poll_cycle(pool=MagicMock(), composio=composio, api=MagicMock())
    assert n == 0


@pytest.mark.asyncio
async def test_poll_cycle_skips_user_without_gmail(monkeypatch) -> None:
    monkeypatch.setattr(ep, "list_poll_targets", AsyncMock(return_value=[("u1", "co1")]))
    monkeypatch.setattr(
        ep, "fetch_contact_emails", AsyncMock(return_value=frozenset({"known@x.com"}))
    )
    enqueue = AsyncMock()
    monkeypatch.setattr(ep, "enqueue", enqueue)

    composio = MagicMock()
    composio.available = True
    composio.is_connected.return_value = False  # not connected

    n = await ep.run_email_poll_cycle(pool=MagicMock(), composio=composio, api=MagicMock())
    assert n == 0
    composio.execute_tool.assert_not_called()
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_cycle_fails_closed_with_no_contacts(monkeypatch) -> None:
    monkeypatch.setattr(ep, "list_poll_targets", AsyncMock(return_value=[("u1", "co1")]))
    monkeypatch.setattr(ep, "fetch_contact_emails", AsyncMock(return_value=frozenset()))
    composio = MagicMock()
    composio.available = True
    n = await ep.run_email_poll_cycle(pool=MagicMock(), composio=composio, api=MagicMock())
    assert n == 0  # no known contacts → surface nothing


@pytest.mark.asyncio
async def test_poll_cycle_allow_all_skips_buildium_contacts(monkeypatch) -> None:
    # Override ON: no Buildium contact fetch, surface a long email from an
    # unknown sender. Proves the chain works without a Buildium key.
    monkeypatch.setenv("HELIXIS_EMAIL_INGEST_ALLOW_ALL_SENDERS", "true")
    monkeypatch.setattr(ep, "list_poll_targets", AsyncMock(return_value=[("u1", "co1")]))
    fetch_contacts = AsyncMock(return_value=frozenset())
    monkeypatch.setattr(ep, "fetch_contact_emails", fetch_contacts)
    monkeypatch.setattr(ep, "get_seen_message_ids", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(ep, "mark_messages_seen", AsyncMock())
    enqueue = AsyncMock(return_value=MagicMock(id="evt-1", duplicate=False))
    monkeypatch.setattr(ep, "enqueue", enqueue)
    monkeypatch.setattr(ep, "store_enrichment", AsyncMock())

    composio = MagicMock()
    composio.available = True
    # Provider-aware: this user has Gmail connected, not Outlook — so the
    # poll's Outlook pass no-ops and only the Gmail path runs.
    composio.is_connected.side_effect = lambda _uid, toolkit: toolkit == "gmail"
    composio.execute_tool.return_value = _fetch_result(_msg("good", "stranger@nowhere.com"))

    n = await ep.run_email_poll_cycle(pool=MagicMock(), composio=composio, api=MagicMock())
    assert n == 1
    fetch_contacts.assert_not_awaited()  # Buildium never touched under override
