"""Decide which inbound Gmail messages are worth surfacing in the Inbox,
and project a surfaced email into the ``InboxActionSurface`` shape.

This is the **deterministic core** of the email-ingest feature (the user
ask: "important emails that are long go through the webhook system").
Everything here is a PURE function — no network, no DB, no LLM — so it's
fully unit-testable. The live seams (poll Gmail via Composio, fetch the
company's Buildium contacts, run the Gemini "Helixis noticed" + draft-reply
enrichment, schedule the worker poll) call into these functions; they are
NOT implemented in this module.

Design mirrors the existing Buildium pipeline's deterministic-floor /
LLM-ceiling split (``surface_builder.build_action_surface_from_routing``):

- :func:`should_surface` is the gate — an email surfaces IFF it is from a
  **known Buildium contact** (tenant / owner / vendor) AND is **long**
  (a substantive message a PM should read, not a one-line "ok thanks").
  Both halves were chosen by the product owner; see the session brief.
- :func:`build_email_action_surface` is the floor: it carries the **full
  email body verbatim** (the user explicitly does NOT want a summary),
  plus the agent-noticed ``signals`` and a ``draft_reply`` the LLM
  enrichment fills in. The frontend already renders ``signals`` as the
  "What Helixis noticed" section (``InboxActionSurface.signals``); the
  ``email`` block and ``draft_reply`` are the additive new fields.

Event identity (for the live wiring slice, NOT done here): these events
ride the existing ``buildium_events`` queue with ``event_name`` starting
``"Email."`` so :mod:`helixis.webhooks.inbox_filter` surfaces them. NOTE:
``event_schemas.dedup_key`` only incorporates INT entity ids, and a Gmail
message id is a hex string — so the poll-loop enqueue must make the
payload's ``EventDateTime`` unique per message (the Gmail ``internalDate``)
or extend the dedup keyspace, otherwise two emails arriving in the same
second could collide on the unique index.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from helixis.security.accommodation_signal import accommodation_floor_signal

# The dotted ``event_name`` family these surfaces use. Kept here so the
# poll loop and the inbox filter agree on one string.
EMAIL_EVENT_NAME = "Email.Received"

# Schema marker for the email action_surface so a future renderer can
# branch on shape. Independent of the Buildium surface's SCHEMA_VERSION.
EMAIL_SURFACE_SCHEMA_VERSION = 1

# "Long" threshold, in words. An email a property manager should actually
# sit down and read — a maintenance write-up, a lease question with
# context, a vendor quote — rather than a one-liner. Tunable; deliberately
# word-based (not chars) so a long signature block doesn't inflate a short
# message past the bar. The product owner can move this knob freely.
LONG_EMAIL_MIN_WORDS = 120

# addr-spec extractor for a "Display Name <local@domain>" header value.
# Falls back to treating the whole string as the address when there's no
# angle-bracket form.
_ANGLE_ADDR_RE = re.compile(r"<([^>]+)>")
_BARE_ADDR_RE = re.compile(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+")


@dataclass(frozen=True)
class EmailAttachmentRef:
    """One attachment reference off a Gmail fetch result's inline
    ``attachmentList`` entry (live-confirmed shape 2026-07-11).

    ``attachment_id`` + the owning message id are exactly what
    ``GMAIL_GET_ATTACHMENT`` needs to fetch the bytes (see
    :mod:`helixis.webhooks.email_photo_triage`). ``mime_type`` here is the
    provider's *declared* part type and is only a coarse image pre-filter —
    it has been observed to lie (``image/png`` on JPEG bytes), so the
    authoritative MIME comes from the downloaded content, never this field."""

    attachment_id: str
    filename: str = ""
    mime_type: str = ""


@dataclass(frozen=True)
class InboundEmail:
    """A single inbound Gmail message, normalised off the Composio/Gmail
    fetch result. The poll loop builds this; the pure functions below
    consume it. Only the fields the triage + surface need are modelled —
    the raw provider payload is carried separately on the queue row."""

    message_id: str
    from_email: str
    subject: str
    body_text: str
    received_at: str  # ISO-8601 string, passed through verbatim
    from_name: str = ""
    thread_id: str | None = None
    to: tuple[str, ...] = field(default_factory=tuple)
    # Which mailbox provider this came from ("gmail" | "outlook"). Carried
    # through to the event payload so a reply can later be routed back to the
    # right provider. Defaults to gmail (the original ingest path).
    provider: str = "gmail"
    # Image (and other) attachments carried on the message, parsed off the
    # Gmail fetch result. Feeds the #533 email photo-triage entry point; empty
    # for Outlook (a different attachment-fetch tool) and attachment-less mail.
    attachments: tuple[EmailAttachmentRef, ...] = field(default_factory=tuple)


def normalize_email_address(raw: str | None) -> str:
    """Return the lowercased bare address from a header value.

    ``"Sarah Chen <Sarah.Chen@Example.com>"`` → ``"sarah.chen@example.com"``.
    ``"BOB@FOO.COM"`` → ``"bob@foo.com"``. Returns ``""`` when no address
    is recognisable (so it can never spuriously match a contact).
    """
    if not raw or not isinstance(raw, str):
        return ""
    candidate = raw.strip()
    angle = _ANGLE_ADDR_RE.search(candidate)
    if angle:
        candidate = angle.group(1).strip()
    else:
        bare = _BARE_ADDR_RE.search(candidate)
        if bare:
            candidate = bare.group(0).strip()
        else:
            return ""
    return candidate.lower()


def normalize_contact_set(emails: Any) -> frozenset[str]:
    """Normalise an iterable of contact emails into a lowercased set for
    membership tests. Skips blanks / non-strings defensively so a messy
    Buildium contact dump can't poison the match.
    """
    out: set[str] = set()
    if not emails:
        return frozenset()
    for raw in emails:
        addr = normalize_email_address(raw if isinstance(raw, str) else None)
        if addr:
            out.add(addr)
    return frozenset(out)


def is_known_contact(from_email: str | None, known_contact_emails: frozenset[str]) -> bool:
    """True when the sender matches a known Buildium contact's email.

    ``known_contact_emails`` must already be normalised (see
    :func:`normalize_contact_set`). The sender is normalised here so a
    raw ``From`` header works directly.
    """
    addr = normalize_email_address(from_email)
    if not addr:
        return False
    return addr in known_contact_emails


def word_count(text: str | None) -> int:
    """Whitespace-delimited word count. ``None``/empty → 0."""
    if not text or not isinstance(text, str):
        return 0
    return len(text.split())


def is_long(body_text: str | None, *, min_words: int = LONG_EMAIL_MIN_WORDS) -> bool:
    """True when the body has at least ``min_words`` words."""
    return word_count(body_text) >= min_words


def should_surface(
    email: InboundEmail,
    known_contact_emails: frozenset[str],
    *,
    min_words: int = LONG_EMAIL_MIN_WORDS,
) -> bool:
    """The gate: surface IFF the email is from a known Buildium contact
    AND is long. Both conditions must hold (the product decision)."""
    return is_known_contact(email.from_email, known_contact_emails) and is_long(
        email.body_text, min_words=min_words
    )


def _sender_label(email: InboundEmail) -> str:
    """Human label for the sender — name when present, else the address."""
    name = (email.from_name or "").strip()
    if name:
        return name
    return normalize_email_address(email.from_email) or "Unknown sender"


def _gmail_url(email: InboundEmail) -> str:
    """Deep link to the message/thread in Gmail's web UI. Falls back to
    the inbox when there's no thread id."""
    if email.thread_id:
        return f"https://mail.google.com/mail/u/0/#all/{email.thread_id}"
    return "https://mail.google.com/mail/u/0/#inbox"


#: Markers that a body carries somebody ELSE'S words below the sender's own.
#:
#: ⚠ Anchored on an explicit attribution header, never on the bare ``>``
#: character. A single quote mark shows up in ordinary prose ("rent > $2,000",
#: a pasted table) and flagging every one of those would put the caveat on a
#: large share of ordinary email — the cry-wolf direction that gets a signal
#: switched off (gotcha 52). A RUN of quoted lines is admitted separately
#: below, because a reply-chain without a header is still plainly a chain.
_THREAD_HEADERS = (
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}", re.I | re.M),
    re.compile(r"^\s*-{2,}\s*Forwarded message\s*-{2,}", re.I | re.M),
    re.compile(r"^\s*Begin forwarded message\s*:", re.I | re.M),
    # "On Mon, 8 Sep 2026 at 14:02, Marcus Whitfield <m@x> wrote:" — the
    # shape every major client emits, wrapped across lines or not.
    re.compile(r"\bOn\b.{0,200}?\bwrote\s*:", re.I | re.S),
    # The Outlook block header. All three keys required, so a body that
    # merely contains the word "From:" does not trip it.
    re.compile(r"^\s*From\s*:.*\n(?:.*\n)?\s*(?:Sent|Date)\s*:.*\n(?:.*\n)?\s*To\s*:", re.I | re.M),
)

#: How many consecutive ``>``-prefixed lines read as a quoted chain on their
#: own. Two, because a single one is as likely to be a stray character.
_MIN_QUOTED_RUN = 2

_QUOTED_RUN = re.compile(rf"(?:^[ \t]*>.*\n){{{_MIN_QUOTED_RUN},}}", re.M)


def carries_quoted_thread(body_text: str | None) -> bool:
    """Does this body contain replies or forwards below the sender's message?

    ⚠ **Why this matters more than it looks.** ``build_email_enrichment`` maps
    the FULL body verbatim onto ``task.description`` and the LAST sender onto
    ``requested_by_first_name`` / ``requested_by_last_name``. On a reply chain
    those two disagree: the description carries several people's words and the
    attribution names one of them. A planner reading it can answer a question
    the sender never asked, or quote a figure the person below them wrote and
    credit it upstairs — gotchas 41/51/104's cross-boundary attribution,
    arriving in an inbox card instead of a tool result.

    This does NOT try to parse the thread. Splitting quoted mail correctly is a
    famously lossy job and a wrong split would hide the sender's own text,
    which is worse than the ambiguity it fixes. It states a fact instead
    (gotcha 26's truncation footer, gotcha 51's uppercase FORMER): the body is
    a chain, so only the top of it is this sender's.
    """
    body = body_text or ""
    if not body.strip():
        return False
    if any(pattern.search(body) for pattern in _THREAD_HEADERS):
        return True
    return _QUOTED_RUN.search(body) is not None


#: The floor line. Deterministic, so it survives an LLM that is down or out of
#: budget — the same reasoning as the comms hazard floor.
QUOTED_THREAD_SIGNAL = (
    "This email is a thread — the message carries replies or forwarded text "
    "below the sender's own words. Only the top belongs to the sender named "
    "here; check who wrote a quote before acting on it."
)


def _signals_with_floor(email: InboundEmail, signals: list[str] | None) -> list[str]:
    """The deterministic floor lines first, then whatever the enrichment supplied.

    ⚠ The accommodation line leads. A thread caveat says "check who wrote
    this"; an accommodation line says "this message has a legal duty attached
    and must not be answered as a routine pet or parking matter". If only one
    survives a skim, it has to be the second.
    """
    supplied = list(signals) if signals else []
    floor: list[str] = []

    accommodation = accommodation_floor_signal(email.subject, email.body_text)
    if accommodation and accommodation not in supplied:
        floor.append(accommodation)

    if carries_quoted_thread(email.body_text) and QUOTED_THREAD_SIGNAL not in supplied:
        floor.append(QUOTED_THREAD_SIGNAL)

    return [*floor, *supplied]


def build_email_action_surface(
    email: InboundEmail,
    *,
    signals: list[str] | None = None,
    draft_reply: str | None = None,
) -> dict[str, Any]:
    """Project an :class:`InboundEmail` into an ``InboxActionSurface`` dict.

    The deterministic floor: even with no LLM enrichment (``signals`` /
    ``draft_reply`` both ``None``) the Inbox renders the full email and a
    "Draft a reply" action. When the Gemini enrichment has run, its
    ``signals`` (the "What Helixis noticed" points the PM might miss) and
    ``draft_reply`` (a reply for review — never auto-sent) layer on top.

    Contract notes:
    - ``summary`` stays a one-liner (sender + subject) for the Inbox list
      row; the FULL body lives under ``email.body_text`` so the detail
      pane shows the real message, not a summary (the user's explicit
      ask). No truncation here — truncation, if any, is the renderer's
      call.
    - The reply action is ``send_reply`` and is intended to flow through
      the existing Composio write-confirm gate when wired, so it never
      sends without the user's tap.
    """
    sender = _sender_label(email)
    subject = (email.subject or "").strip() or "(no subject)"
    surface: dict[str, Any] = {
        "schema_version": EMAIL_SURFACE_SCHEMA_VERSION,
        "kind": "email",
        "summary": f"Email from {sender}: {subject}",
        "email": {
            "message_id": email.message_id,
            "thread_id": email.thread_id,
            "from_email": normalize_email_address(email.from_email),
            "from_name": (email.from_name or "").strip(),
            "subject": subject,
            "received_at": email.received_at,
            "body_text": email.body_text or "",
        },
        # The "What Helixis noticed" section reads this verbatim. Empty
        # list (not null) on the floor so the renderer shows a clean
        # "nothing flagged yet" rather than guessing.
        #
        # ⚠ The thread line is a DETERMINISTIC floor and leads, ahead of any
        # LLM signals — it is a fact about who wrote the words below, and the
        # model that would otherwise have to notice it is the same one liable
        # to mis-attribute them. Deduped so an enrichment that says the same
        # thing does not print it twice.
        "signals": _signals_with_floor(email, signals),
        # A reply for review. None on the floor; the renderer pre-fills
        # the inline editor when present.
        "draft_reply": draft_reply,
        "actions": [
            {
                "tool_id": "send_reply",
                "label": "Review & send reply",
                "intent": (
                    "Open the drafted reply for editing; sends through Gmail "
                    "only after you confirm. Never auto-sends."
                ),
                "parameters": {
                    "thread_id": email.thread_id,
                    "to": normalize_email_address(email.from_email),
                    "subject": subject,
                },
            },
            {
                "tool_id": "noop_open_gmail",
                "label": "Open in Gmail",
                "intent": "Open the original message in Gmail in a new tab.",
                "parameters": {"gmail_url": _gmail_url(email)},
            },
        ],
        "freeform_placeholder": "Edit the reply or add a note…",
    }
    return surface


def email_synthetic_task_id(message_id: str) -> int:
    """Map a Gmail message id (a hex string) to a stable positive int so
    it can fill ``EnrichedTask.id`` (typed ``int``, required).

    The existing planner/summarizer pipeline keys some logic off the
    task id; emails have no Buildium task, so we derive a deterministic
    surrogate from the message id. ``crc32`` is stable across processes
    (unlike ``hash()``) and fits in a signed 32-bit int — collisions are
    astronomically unlikely across one company's mailbox, and the real
    identity (the message id) is preserved on ``raw`` either way.
    """
    return zlib.crc32(message_id.encode("utf-8")) & 0x7FFFFFFF


def build_email_enrichment(email: InboundEmail, company_id: str) -> dict[str, Any]:
    """Project an :class:`InboundEmail` onto the Buildium ``EnrichedEvent``
    shape so the EXISTING enrichment → planner → summarizer pipeline
    consumes an email natively — no parallel "email brain".

    The mapping treats an email from a known contact like the resident
    request the pipeline already handles:

    - ``task.title``       ← subject
    - ``task.description`` ← the FULL body, verbatim (the frontend renders
      the real email from here — the user explicitly does NOT want it
      summarized; the Summarizer's one-line narrative is separate).
    - ``task.category``    ← ``"Email"`` so prompts can branch on it.
    - ``task.requested_by_first_name/last_name`` ← the sender's name.
    - ``task.raw``         ← the sender address, message/thread ids, and
      received time so the email-reply tool can target the thread.

    Returns a plain dict (``EnrichedEvent`` JSON shape) ready to store in
    ``buildium_events.enrichment``; the worker reconstructs an
    ``EnrichedEvent`` from it exactly as it does for Buildium rows. Built
    as a dict (not the pydantic model) to keep this module import-light
    and free of a hard dependency on the agent package.
    """
    first, _, last = (email.from_name or "").strip().partition(" ")
    sender_addr = normalize_email_address(email.from_email)
    return {
        "schema_version": 1,
        "event_name": EMAIL_EVENT_NAME,
        "company_id": company_id,
        "task": {
            "id": email_synthetic_task_id(email.message_id),
            "title": (email.subject or "").strip() or "(no subject)",
            "description": email.body_text or "",
            "status": None,
            "category": "Email",
            "priority": None,
            "requested_by_first_name": first or None,
            "requested_by_last_name": last or None,
            "raw": {
                "source": "gmail",
                "message_id": email.message_id,
                "thread_id": email.thread_id,
                "from_email": sender_addr,
                "from_name": (email.from_name or "").strip(),
                "received_at": email.received_at,
                "to": list(email.to),
                # ⚠ The description above is the FULL body and the two
                # requested_by fields name the LAST sender. On a reply chain
                # those disagree: several people's words, one name. Stated
                # here so the planner can see the mismatch rather than having
                # to notice it in prose (gotchas 41/51/104).
                "has_quoted_thread": carries_quoted_thread(email.body_text),
            },
        },
        "unit": None,
        "active_lease": None,
        "history_unit": [],
        "history_property": [],
        "errors": [],
        "fetched_at": datetime.now(UTC).isoformat(),
    }


__all__ = [
    "EMAIL_EVENT_NAME",
    "EMAIL_SURFACE_SCHEMA_VERSION",
    "LONG_EMAIL_MIN_WORDS",
    "QUOTED_THREAD_SIGNAL",
    "EmailAttachmentRef",
    "InboundEmail",
    "build_email_action_surface",
    "build_email_enrichment",
    "carries_quoted_thread",
    "email_synthetic_task_id",
    "is_known_contact",
    "is_long",
    "normalize_contact_set",
    "normalize_email_address",
    "should_surface",
    "word_count",
]
