"""Tests for the Gmail→Inbox ingest glue (``email_ingest``).

The Buildium/Composio calls are mocked; the parse + select logic is
exercised directly.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.webhooks.email_ingest import (
    fetch_contact_emails,
    parse_gmail_fetch_result,
    parse_outlook_fetch_result,
    select_emails_to_enqueue,
)
from helixis.webhooks.email_triage import InboundEmail, normalize_contact_set

# ── Outlook (Microsoft Graph) parser ────────────────────────────────


def _graph_msg(**over):
    base = {
        "id": "AAMk-1",
        "subject": "Lease renewal question",
        "bodyPreview": "Hi, I have a question about renewing my lease next month.",
        "from": {"emailAddress": {"name": "Sarah Chen", "address": "Sarah@X.com"}},
        "receivedDateTime": "2026-06-28T10:00:00Z",
        "conversationId": "conv-1",
    }
    base.update(over)
    return base


def test_parse_outlook_graph_shape():
    result = {"value": [_graph_msg()]}
    emails = parse_outlook_fetch_result(result)
    assert len(emails) == 1
    e = emails[0]
    assert e.provider == "outlook"
    assert e.message_id == "AAMk-1"
    assert e.from_email == "sarah@x.com"  # normalized lower-case
    assert e.from_name == "Sarah Chen"
    assert e.thread_id == "conv-1"
    assert "lease" in e.body_text.lower()


def test_parse_outlook_falls_back_to_html_body():
    msg = _graph_msg(
        bodyPreview="", body={"contentType": "html", "content": "<p>Hello <b>there</b></p>"}
    )
    emails = parse_outlook_fetch_result({"value": [msg]})
    assert emails[0].body_text == "Hello there"  # tags stripped


def test_parse_outlook_skips_message_without_sender():
    good = _graph_msg()
    bad = _graph_msg(id="AAMk-2", **{"from": {}})  # no address
    emails = parse_outlook_fetch_result({"value": [good, bad]})
    assert [e.message_id for e in emails] == ["AAMk-1"]


def test_parse_outlook_tolerates_wrapped_containers():
    # Composio may wrap the Graph collection under data/response_data.
    wrapped = {"response_data": {"value": [_graph_msg()]}}
    assert len(parse_outlook_fetch_result(wrapped)) == 1


def test_parse_outlook_empty_or_malformed():
    assert parse_outlook_fetch_result(None) == []
    assert parse_outlook_fetch_result({}) == []
    assert parse_outlook_fetch_result({"value": "nope"}) == []


LONG = " ".join(["word"] * 200)


# ── fetch_contact_emails ─────────────────────────────────────────────


def _api(*, tenants=(), owners=(), vendors=()) -> MagicMock:
    api = MagicMock()
    api.rentals.list_tenants = AsyncMock(return_value=list(tenants))
    api.rentals.list_owners = AsyncMock(return_value=list(owners))
    api.vendors.list = AsyncMock(return_value=list(vendors))
    return api


@pytest.mark.asyncio
async def test_fetch_contact_emails_collects_all_three_sources() -> None:
    api = _api(
        tenants=[SimpleNamespace(email="Tenant@X.com", alternate_email="alt@x.com")],
        owners=[SimpleNamespace(email="owner@y.com")],
        vendors=[SimpleNamespace(primary_email="vend@z.com", alternate_email=None)],
    )
    contacts = await fetch_contact_emails(api)
    assert contacts == frozenset({"tenant@x.com", "alt@x.com", "owner@y.com", "vend@z.com"})


@pytest.mark.asyncio
async def test_fetch_contact_emails_best_effort_on_partial_failure() -> None:
    api = _api(owners=[SimpleNamespace(email="owner@y.com")])
    api.rentals.list_tenants = AsyncMock(side_effect=RuntimeError("Buildium 503"))
    contacts = await fetch_contact_emails(api)
    # Tenant fetch blew up but owners still came through — fail-soft.
    assert contacts == frozenset({"owner@y.com"})


# ── parse_gmail_fetch_result ─────────────────────────────────────────


def test_parse_reads_common_aliases() -> None:
    result = {
        "data": {
            "messages": [
                {
                    "messageId": "m1",
                    "threadId": "t1",
                    "sender": "Sarah Chen <sarah@x.com>",
                    "subject": "Leak",
                    "messageText": LONG,
                    "messageTimestamp": "2026-06-27T00:00:00Z",
                }
            ]
        }
    }
    [email] = parse_gmail_fetch_result(result)
    assert email.message_id == "m1"
    assert email.thread_id == "t1"
    assert email.from_email == "sarah@x.com"
    assert email.from_name == "Sarah Chen"
    assert email.subject == "Leak"
    assert email.body_text == LONG


def test_parse_skips_messages_without_id_or_sender() -> None:
    result = {
        "messages": [
            {"sender": "x@y.com"},  # no id
            {"messageId": "m2"},  # no sender
            {"messageId": "m3", "from": "ok@z.com", "body": "hi"},
        ]
    }
    parsed = parse_gmail_fetch_result(result)
    assert [e.message_id for e in parsed] == ["m3"]


def test_parse_tolerates_empty_and_bad_shapes() -> None:
    assert parse_gmail_fetch_result(None) == []
    assert parse_gmail_fetch_result({}) == []
    assert parse_gmail_fetch_result({"messages": "nope"}) == []


# ── attachment parsing (#533 entry point 3) ──────────────────────────


def test_parse_gmail_attachments_live_shape() -> None:
    # The live-confirmed shape: attachmentList with {attachmentId, filename, mimeType}.
    from helixis.webhooks.email_ingest import parse_gmail_attachments

    msg = {
        "messageId": "m1",
        "attachmentList": [
            {"attachmentId": "ANGjdJ8...", "filename": "leak.png", "mimeType": "image/png"},
            {"attachmentId": "OTHER", "filename": "notes.pdf", "mimeType": "application/pdf"},
        ],
    }
    refs = parse_gmail_attachments(msg)
    assert [r.attachment_id for r in refs] == ["ANGjdJ8...", "OTHER"]
    assert refs[0].filename == "leak.png"
    assert refs[0].mime_type == "image/png"


def test_parse_gmail_attachments_skips_entries_without_id() -> None:
    from helixis.webhooks.email_ingest import parse_gmail_attachments

    msg = {"attachmentList": [{"filename": "x.png"}, {"attachmentId": "ok"}, "not-a-dict"]}
    refs = parse_gmail_attachments(msg)
    assert [r.attachment_id for r in refs] == ["ok"]


def test_parse_gmail_attachments_empty_when_absent() -> None:
    from helixis.webhooks.email_ingest import parse_gmail_attachments

    assert parse_gmail_attachments({"messageId": "m1"}) == ()
    assert parse_gmail_attachments({"attachmentList": "nope"}) == ()
    assert parse_gmail_attachments({"attachmentList": []}) == ()


def test_parse_gmail_fetch_result_populates_attachments() -> None:
    result = {
        "data": {
            "messages": [
                {
                    "messageId": "m1",
                    "sender": "sarah@x.com",
                    "messageText": LONG,
                    "attachmentList": [
                        {"attachmentId": "a1", "filename": "leak.jpg", "mimeType": "image/jpeg"}
                    ],
                }
            ]
        }
    }
    [email] = parse_gmail_fetch_result(result)
    assert len(email.attachments) == 1
    assert email.attachments[0].attachment_id == "a1"
    assert email.attachments[0].mime_type == "image/jpeg"


def test_parse_gmail_fetch_result_no_attachments_is_empty_tuple() -> None:
    result = {"messages": [{"messageId": "m1", "sender": "a@b.com", "messageText": LONG}]}
    [email] = parse_gmail_fetch_result(result)
    assert email.attachments == ()


# ── select_emails_to_enqueue ─────────────────────────────────────────


def _email(mid: str, addr: str, body: str = LONG) -> InboundEmail:
    return InboundEmail(
        message_id=mid,
        from_email=addr,
        subject="s",
        body_text=body,
        received_at="2026-06-27T00:00:00Z",
        from_name="N",
    )


def test_select_applies_gate_and_dedup() -> None:
    contacts = normalize_contact_set(["known@x.com"])
    messages = [
        _email("a", "known@x.com"),  # known + long → keep
        _email("b", "stranger@x.com"),  # unknown → drop
        _email("c", "known@x.com", body="short"),  # known but short → drop
        _email("d", "known@x.com"),  # known + long but already seen → drop
    ]
    out = select_emails_to_enqueue(messages, contacts, already_seen=frozenset({"d"}))
    assert [e.message_id for e in out] == ["a"]


# ── enqueue payload builder + dedup ──────────────────────────────────


def test_build_email_event_payload_shape() -> None:
    from helixis.webhooks.email_ingest import build_email_event_payload

    payload = build_email_event_payload(_email("m9", "known@x.com"), account_id=0)
    assert payload["EventName"] == "Email.Received"
    assert payload["MessageId"] == "m9"
    assert payload["AccountId"] == 0
    assert payload["From"] == "known@x.com"


def test_dedup_key_uses_message_id_for_emails() -> None:
    from helixis.webhooks.email_ingest import build_email_event_payload
    from helixis.webhooks.event_schemas import dedup_key

    # Two distinct messages that arrived in the SAME second must NOT
    # collide on the dedup key (MessageId disambiguates them).
    a = _email("m1", "known@x.com")
    b = _email("m2", "known@x.com")
    pa = build_email_event_payload(a, 0)
    pb = build_email_event_payload(b, 0)
    pa["EventDateTime"] = pb["EventDateTime"] = "2026-06-27T00:00:00Z"
    assert dedup_key(pa) != dedup_key(pb)
    # Same message → stable key (Gmail could be re-fetched).
    assert dedup_key(pa) == dedup_key(
        build_email_event_payload(a, 0) | {"EventDateTime": "2026-06-27T00:00:00Z"}
    )


def test_dedup_key_unaffected_for_buildium_payloads() -> None:
    from helixis.webhooks.event_schemas import dedup_key

    # No MessageId → behaves exactly as before (regression guard). LeaseId
    # is in _ENTITY_ID_FIELDS so it still appears; no MessageId segment.
    buildium = {
        "AccountId": 42,
        "EventName": "Lease.Created",
        "EventDateTime": "2026-06-27T00:00:00Z",
        "LeaseId": 7,
    }
    key = dedup_key(buildium)
    assert key == "account=42|Lease.Created|2026-06-27T00:00:00Z|LeaseId=7"
    assert "MessageId" not in key


def test_select_allow_all_senders_drops_contact_gate_keeps_length() -> None:
    # Testing override: surface long emails from ANYONE, but still never
    # a one-liner. Contacts set is empty (no Buildium).
    messages = [
        _email("a", "stranger@x.com"),  # long, unknown → kept under override
        _email("b", "stranger@x.com", body="short"),  # short → still dropped
    ]
    out = select_emails_to_enqueue(
        messages, frozenset(), already_seen=frozenset(), allow_all_senders=True
    )
    assert [e.message_id for e in out] == ["a"]
