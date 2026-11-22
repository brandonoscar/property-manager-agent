"""Gmail → Inbox ingest glue.

The pure decision rules live in :mod:`helixis.webhooks.email_triage`
(the gate, the EnrichedEvent mapping). This module is the worker-facing
layer that feeds those rules:

1. :func:`fetch_contact_emails` — pull a company's known-contact email
   set (tenants + rental owners + vendors) from the Buildium API. This is
   the "important = from a known Buildium contact" half of the gate.
2. :func:`parse_gmail_fetch_result` — turn a Composio ``GMAIL_FETCH_EMAILS``
   result into :class:`InboundEmail` objects, tolerant of provider shape
   drift.
3. :func:`select_emails_to_enqueue` — apply the gate + drop already-seen
   message ids, returning the emails the worker should enqueue.

The Buildium/Composio calls are passed in (a ``BuildiumApi`` instance, a
parsed result dict) so the logic here is unit-testable without network.
The live worker poll cycle (enumerate connected users, call Composio,
construct a per-company Buildium client, write queue rows) is the seam
that wires these together against real services — see the worker.

⚠ The exact ``GMAIL_FETCH_EMAILS`` result shape is a Composio contract
that must be confirmed against a live fetch; :func:`parse_gmail_fetch_result`
reads several common field aliases and skips anything it can't parse
rather than guessing, so a shape mismatch degrades to "0 emails" (safe)
instead of crashing the worker.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from helixis.webhooks.email_triage import (
    EMAIL_EVENT_NAME,
    LONG_EMAIL_MIN_WORDS,
    EmailAttachmentRef,
    InboundEmail,
    is_long,
    normalize_contact_set,
    normalize_email_address,
    should_surface,
)

if TYPE_CHECKING:
    from helixis.buildium.api import BuildiumApi

logger = logging.getLogger(__name__)


def _emails_from_objects(objects: Any, *attrs: str) -> list[str]:
    """Pull the given email attributes off a list of schema objects,
    skipping blanks. Defensive against a list call that returned
    something odd."""
    out: list[str] = []
    if not objects:
        return out
    try:
        iterator = list(objects)
    except TypeError:
        return out
    for obj in iterator:
        for attr in attrs:
            value = getattr(obj, attr, None)
            if isinstance(value, str) and value.strip():
                out.append(value)
    return out


async def fetch_contact_emails(api: BuildiumApi) -> frozenset[str]:
    """Return the normalised set of known-contact emails for the company
    backing ``api`` — tenants, rental owners, and vendors.

    Best-effort per source: a failure fetching one list (Buildium 5xx,
    an empty toolkit) logs and contributes nothing rather than sinking
    the whole set. An empty set means the gate will surface NO emails
    (fail-closed) — better than surfacing spam from unknown senders.
    """
    collected: list[str] = []

    async def _safe(label: str, coro: Any, *attrs: str) -> None:
        try:
            objects = await coro
        except Exception:  # noqa: BLE001
            logger.warning("email_ingest: failed to fetch %s contacts", label, exc_info=True)
            return
        collected.extend(_emails_from_objects(objects, *attrs))

    await _safe("tenant", api.rentals.list_tenants(), "email", "alternate_email")
    await _safe("owner", api.rentals.list_owners(), "email")
    await _safe("vendor", api.vendors.list(), "primary_email", "alternate_email")

    contacts = normalize_contact_set(collected)
    logger.info("email_ingest: resolved %d known-contact email(s)", len(contacts))
    return contacts


# Field aliases Composio / Gmail use across SDK versions for each piece
# of a message. parse_gmail_fetch_result reads the first present alias.
_ID_KEYS = ("messageId", "message_id", "id")
_THREAD_KEYS = ("threadId", "thread_id")
_FROM_KEYS = ("sender", "from", "From", "fromEmail")
_SUBJECT_KEYS = ("subject", "Subject")
_BODY_KEYS = ("messageText", "body", "text", "snippet", "preview", "messageBody")
_DATE_KEYS = ("messageTimestamp", "date", "internalDate", "Date")

# Attachment refs ride each message under ``attachmentList`` (live-confirmed
# key on GMAIL_FETCH_EMAILS, 2026-07-11); the alias tables tolerate SDK drift.
_ATTACHMENT_LIST_KEYS = ("attachmentList", "attachments", "attachment_list")
_ATTACHMENT_ID_KEYS = ("attachmentId", "attachment_id", "id")
_ATTACHMENT_NAME_KEYS = ("filename", "fileName", "name")
_ATTACHMENT_MIME_KEYS = ("mimeType", "mime_type", "contentType")


def _first(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def parse_gmail_attachments(msg: dict[str, Any]) -> tuple[EmailAttachmentRef, ...]:
    """Pull a Gmail message's inline ``attachmentList`` into
    :class:`EmailAttachmentRef` tuples (feeds the #533 photo-triage entry
    point). An entry with no attachment id is skipped — we never fabricate a
    ref we couldn't fetch. Tolerant of list-key / field-name drift; anything
    unrecognisable degrades to ``()`` (safe: nothing to triage)."""
    raw: Any = None
    for key in _ATTACHMENT_LIST_KEYS:
        candidate = msg.get(key)
        if isinstance(candidate, list):
            raw = candidate
            break
    if not raw:
        return ()
    out: list[EmailAttachmentRef] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        attachment_id = _first(item, _ATTACHMENT_ID_KEYS)
        if not attachment_id:
            continue
        out.append(
            EmailAttachmentRef(
                attachment_id=str(attachment_id),
                filename=str(_first(item, _ATTACHMENT_NAME_KEYS) or ""),
                mime_type=str(_first(item, _ATTACHMENT_MIME_KEYS) or ""),
            )
        )
    return tuple(out)


def _split_name_email(raw: Any) -> tuple[str, str]:
    """``"Sarah Chen <sarah@x.com>"`` → ``("Sarah Chen", "sarah@x.com")``."""
    if not isinstance(raw, str):
        return ("", "")
    addr = normalize_email_address(raw)
    name = raw.split("<", 1)[0].strip().strip('"') if "<" in raw else ""
    return (name, addr)


def parse_gmail_fetch_result(result: dict[str, Any] | None) -> list[InboundEmail]:
    """Parse a Composio ``GMAIL_FETCH_EMAILS`` result into
    :class:`InboundEmail` objects.

    Tolerant of shape: the message list may live under ``messages`` /
    ``data.messages`` / ``response_data.messages`` or be the top-level
    list itself. Each message's fields are read via the alias tables
    above. A message with no parseable id or sender is skipped (logged
    at debug) — we never fabricate identity.
    """
    if not result:
        return []

    messages: Any = None
    if isinstance(result, list):
        messages = result
    elif isinstance(result, dict):
        for container in (result, result.get("data"), result.get("response_data")):
            if isinstance(container, dict) and isinstance(container.get("messages"), list):
                messages = container["messages"]
                break
        if messages is None and isinstance(result.get("messages"), list):
            messages = result["messages"]
    if not isinstance(messages, list):
        logger.warning("email_ingest: GMAIL fetch result had no messages list")
        return []

    parsed: list[InboundEmail] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        message_id = _first(msg, _ID_KEYS)
        from_raw = _first(msg, _FROM_KEYS)
        if not message_id or not from_raw:
            logger.debug("email_ingest: skipping message with no id/sender")
            continue
        name, addr = _split_name_email(from_raw)
        if not addr:
            continue
        body = _first(msg, _BODY_KEYS) or ""
        parsed.append(
            InboundEmail(
                message_id=str(message_id),
                from_email=addr,
                from_name=name,
                subject=str(_first(msg, _SUBJECT_KEYS) or ""),
                body_text=str(body),
                received_at=str(_first(msg, _DATE_KEYS) or ""),
                thread_id=(str(_first(msg, _THREAD_KEYS)) if _first(msg, _THREAD_KEYS) else None),
                attachments=parse_gmail_attachments(msg),
            )
        )
    return parsed


# Microsoft Graph (Outlook) message collections come back under "value".
# Composio may also wrap them under data/response_data — be tolerant.
_OUTLOOK_LIST_KEYS = ("value", "messages")
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Crude HTML→text for an Outlook body.content fallback. The body
    preview is plain already; this only runs when we have to read the HTML
    body. Not a full sanitiser — just enough for the triage word-count +
    a readable Inbox preview."""
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", text)).strip()


def _graph_address(node: Any) -> tuple[str, str]:
    """Pull (name, address) from a Graph recipient node:
    ``{"emailAddress": {"name": "...", "address": "..."}}``. Tolerant of a
    bare string or a flat ``{"name","address"}`` too."""
    if isinstance(node, str):
        return _split_name_email(node)
    if isinstance(node, dict):
        ea = node.get("emailAddress") if isinstance(node.get("emailAddress"), dict) else node
        if isinstance(ea, dict):
            addr = normalize_email_address(str(ea.get("address") or ea.get("email") or ""))
            name = str(ea.get("name") or "")
            return (name, addr)
    return ("", "")


def parse_outlook_fetch_result(result: dict[str, Any] | None) -> list[InboundEmail]:
    """Parse a Composio Outlook list-messages result into
    :class:`InboundEmail` objects (``provider="outlook"``).

    Outlook rides Microsoft Graph, whose message shape differs from
    Gmail's: the sender is nested (``from.emailAddress.address``), the body
    is ``bodyPreview`` (plain) or ``body.content`` (HTML), the timestamp is
    ``receivedDateTime``, and the thread is ``conversationId``. The
    collection lives under ``value`` (Graph) — tolerant of data/
    response_data wrapping + a top-level list, mirroring the Gmail parser.

    ⚠ Live seam: the exact Composio wrapper shape + slug are confirmed on
    first smoke with a connected Outlook account (see email_poll). A message
    with no parseable id or sender is skipped — identity is never fabricated.
    """
    if not result:
        return []

    messages: Any = None
    if isinstance(result, list):
        messages = result
    elif isinstance(result, dict):
        for container in (result, result.get("data"), result.get("response_data")):
            if not isinstance(container, dict):
                continue
            for key in _OUTLOOK_LIST_KEYS:
                if isinstance(container.get(key), list):
                    messages = container[key]
                    break
            if messages is not None:
                break
    if not isinstance(messages, list):
        logger.warning("email_ingest: OUTLOOK fetch result had no message list")
        return []

    parsed: list[InboundEmail] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        message_id = msg.get("id") or _first(msg, _ID_KEYS)
        name, addr = _graph_address(msg.get("from") or msg.get("sender") or _first(msg, _FROM_KEYS))
        if not message_id or not addr:
            logger.debug("email_ingest: skipping outlook message with no id/sender")
            continue
        body = msg.get("bodyPreview")
        if not body:
            raw_body = msg.get("body")
            if isinstance(raw_body, dict):
                body = _strip_html(str(raw_body.get("content") or ""))
            else:
                body = _first(msg, _BODY_KEYS) or ""
        received = (
            msg.get("receivedDateTime") or msg.get("sentDateTime") or _first(msg, _DATE_KEYS) or ""
        )
        thread = msg.get("conversationId") or _first(msg, _THREAD_KEYS)
        parsed.append(
            InboundEmail(
                message_id=str(message_id),
                from_email=addr,
                from_name=name,
                subject=str(msg.get("subject") or _first(msg, _SUBJECT_KEYS) or ""),
                body_text=str(body or ""),
                received_at=str(received),
                thread_id=(str(thread) if thread else None),
                provider="outlook",
            )
        )
    return parsed


def select_emails_to_enqueue(
    messages: list[InboundEmail],
    contact_emails: frozenset[str],
    *,
    already_seen: frozenset[str],
    min_words: int | None = None,
    allow_all_senders: bool = False,
) -> list[InboundEmail]:
    """Filter parsed messages to the ones the worker should enqueue:
    they pass the gate (known contact AND long) and haven't been seen
    on a prior poll cycle (``already_seen`` = message ids already
    processed for this user).

    ``allow_all_senders`` is a TESTING override (see the worker's
    ``HELIXIS_EMAIL_INGEST_ALLOW_ALL_SENDERS`` flag): it drops the
    known-contact half of the gate so the pipeline can be validated
    before a Buildium key exists (the contact list is what normally
    defines "known"). Length still applies — it never surfaces a
    one-liner. NOT for production: the shipped product gates on Buildium
    contacts.
    """
    effective_min = LONG_EMAIL_MIN_WORDS if min_words is None else min_words
    out: list[InboundEmail] = []
    for email in messages:
        if email.message_id in already_seen:
            continue
        if allow_all_senders:
            surfaced = is_long(email.body_text, min_words=effective_min)
        else:
            surfaced = should_surface(email, contact_emails, min_words=effective_min)
        if surfaced:
            out.append(email)
    return out


def _parse_dt(value: str) -> datetime:
    """Parse an ISO-ish received-at string into an aware datetime, falling
    back to now() when the provider gave us something unparseable. The
    queue needs a real ``event_time``; the exact value only affects
    ordering, so a fallback is harmless."""
    if value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(UTC)


def build_email_event_payload(email: InboundEmail, account_id: int) -> dict[str, Any]:
    """Build the ``buildium_events.payload`` for an email row.

    Shaped like a Buildium envelope (``EventName`` / ``EventDateTime`` /
    ``AccountId``) so it flows through the same queue + dedup. ``MessageId``
    is the per-message dedup key (see ``event_schemas.dedup_key``); the
    rest carries enough to render/reply without re-fetching.
    """
    return {
        "EventName": EMAIL_EVENT_NAME,
        "EventDateTime": email.received_at or datetime.now(UTC).isoformat(),
        "AccountId": account_id,
        "MessageId": email.message_id,
        "ThreadId": email.thread_id,
        "From": email.from_email,
        "FromName": email.from_name,
        "Subject": email.subject,
        "Provider": email.provider,
    }


def email_event_time(email: InboundEmail) -> datetime:
    """The ``event_time`` to enqueue for an email row."""
    return _parse_dt(email.received_at)


__all__ = [
    "build_email_event_payload",
    "email_event_time",
    "fetch_contact_emails",
    "parse_gmail_attachments",
    "parse_gmail_fetch_result",
    "parse_outlook_fetch_result",
    "select_emails_to_enqueue",
]
