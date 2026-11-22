"""Turn a finished call/text with a Buildium contact into the pipeline's
``EnrichedEvent`` — so a conversation "gives the AI context or auto-creates a
task" through the EXISTING enrichment → planner → summarizer path, not a
parallel "comms brain".

This is the deterministic core of the click-to-call/text feature's *context*
half (founder goal 2026-08). It is a direct sibling of
:mod:`helixis.webhooks.email_triage`: every function here is PURE — no network,
no DB, no LLM, no Twilio — so it's fully unit-testable without a phone number.
The live seams it does NOT implement (receive the Twilio webhook, transcribe an
answered call, persist the thread, run the planner) call into these functions.

**How it differs from email triage — deliberately:**

- **The gate is phone-identity, not length.** Email length-gates (≥120 words)
  to keep newsletters out; a text from a known resident is never a newsletter,
  and the shortest texts ("water pouring through my ceiling") are often the most
  urgent. So :func:`should_surface_comms` gates ONLY on "is this a known
  Buildium contact?" (the caller passes the already-resolved
  :class:`~helixis.comms.contact_resolver.ResolvedContact`) plus non-empty
  content. No length floor.
- **Identity is the resolved contact, not an email address.** The phone→contact
  resolution happens upstream (``comms.contact_resolver``); this module consumes
  its result, so an unknown number (``contact=None``) never surfaces.

The produced dict is the ``EnrichedEvent`` JSON shape (same top-level keys as
``email_triage.build_email_enrichment``); the worker reconstructs an
``EnrichedEvent`` from it exactly as it does for Buildium/email rows.
"""

from __future__ import annotations

import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from helixis.comms.contact_resolver import ResolvedContact
from helixis.comms.phone import normalize_e164
from helixis.comms.transcript import transcript_caveat
from helixis.security.accommodation_signal import accommodation_floor_signal
from helixis.security.hazard_lexicon import detect_hazards, hazard_label

# Dotted ``event_name`` families, one per channel. Kept here so the (future)
# receive-webhook seam and the inbox filter agree on the strings. The ``Comms.``
# prefix lets a renderer / planner branch on the family the way ``Email.`` does.
SMS_EVENT_NAME = "Comms.SmsReceived"
CALL_EVENT_NAME = "Comms.CallCompleted"

# Schema marker for the comms action_surface, independent of the email/Buildium
# surface versions.
COMMS_SURFACE_SCHEMA_VERSION = 1

# How many leading words of a text/transcript become the Inbox title. The FULL
# body always rides in ``description`` (verbatim) — this is only the list-row
# label, mirroring email's subject line.
_TITLE_SNIPPET_WORDS = 9

# Channel vocabulary. ``voice`` covers both a transcribed answered call and a
# transcribed voicemail — the transcript is the body either way.
_CHANNELS = ("sms", "voice")


@dataclass(frozen=True)
class InboundComms:
    """A finished inbound call or text, normalised off the Twilio webhook.

    The receive seam builds this (resolving the contact via
    ``comms.contact_resolver`` first); the pure functions below consume it. Only
    the fields triage + surface need are modelled — the raw provider payload is
    carried separately on the queue row.

    ``body`` is the message text for SMS, or the transcript for a voice call /
    voicemail. ``contact`` is the resolved Buildium contact, or ``None`` for an
    unknown number (which never surfaces). ``sid`` is Twilio's Message/Call SID
    — the stable identity used for the synthetic task id and dedup.
    """

    channel: str  # "sms" | "voice"
    from_number: str
    to_number: str
    body: str
    occurred_at: str  # ISO-8601 string, passed through verbatim
    contact: ResolvedContact | None = None
    #: EVERY distinct Buildium contact this number matches, when more than one
    #: does. Empty for the ordinary single-match case.
    #:
    #: ⚠ A household handset is shared. `resolve_contact_by_phone` picks the
    #: highest-precedence match and logs the rest, so a text from a couple was
    #: carded as "Text from Josh Gil" — a confident false statement about which
    #: real person said it, and the auto-task attributed the request to them.
    #: Same shape as gotchas 41/51: the other candidates were fetched and
    #: thrown away. `resolve_all_contacts_by_phone` already returns them and
    #: its docstring says callers that must disambiguate should use it; until
    #: now nothing did (gotcha 62's wired-to-nothing).
    candidates: tuple[ResolvedContact, ...] = field(default_factory=tuple)
    direction: str = "inbound"  # "inbound" | "outbound"
    sid: str = ""
    provider: str = "twilio"
    # MMS / recording media URLs carried on the message. Empty for a plain text
    # or a transcript-only call. The photo-triage entry point can consume these
    # the way email attachments feed #533.
    media_urls: tuple[str, ...] = field(default_factory=tuple)


def has_content(body: str | None) -> bool:
    """True when there's a non-whitespace message/transcript to act on."""
    return bool(body and body.strip())


def is_known_contact(comms: InboundComms) -> bool:
    """True when the number resolved to a Buildium contact upstream."""
    return comms.contact is not None


def should_surface_comms(comms: InboundComms) -> bool:
    """The gate: surface IFF the message is from a known Buildium contact AND
    carries content. No length floor (see module docstring) — a two-word text
    from a resident is exactly what a PM must see."""
    return is_known_contact(comms) and has_content(comms.body)


def _channel_label(channel: str) -> str:
    return {"sms": "Text", "voice": "Call"}.get(channel, "Message")


def _category(channel: str) -> str:
    """The ``EnrichedTask.category`` a prompt can branch on."""
    return {"sms": "Text message", "voice": "Phone call"}.get(channel, "Message")


def is_ambiguous(comms: InboundComms) -> bool:
    """Does this number belong to more than one distinct Buildium contact?

    Distinct by (kind, buildium_id) — the same person listed on two leases is
    one contact, not an ambiguity, and treating it as one would caveat a large
    share of ordinary texts into uselessness (gotcha 52).
    """
    return len({(c.kind, c.buildium_id) for c in comms.candidates}) > 1


def _contact_name(comms: InboundComms) -> str:
    """Who this is from, and it must not name ONE person out of several.

    ⚠ Every candidate is listed rather than picking by precedence, so no
    downstream reader can quote a name that was chosen by a tiebreak. Gotcha
    41's rule, applied to a person instead of an address: make the line
    self-contained so there is no join left to mis-wire.
    """
    if is_ambiguous(comms):
        names = [c.name for c in comms.candidates if c.name]
        if len(names) > 2:
            return f"{names[0]}, {names[1]} or {len(names) - 2} other(s) at this number"
        if names:
            return " or ".join(names)
    if comms.contact is not None:
        return comms.contact.name
    return normalize_e164(comms.from_number) or comms.from_number or "Unknown caller"


def _snippet(text: str | None, *, max_words: int = _TITLE_SNIPPET_WORDS) -> str:
    """First ``max_words`` words of ``text`` for a list-row title, ``…`` when
    truncated. The full text is never lost — it rides in ``description``."""
    words = (text or "").split()
    if not words:
        return ""
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words]) + "…"


def comms_synthetic_task_id(sid: str) -> int:
    """Map a Twilio SID (a string) to a stable positive int for
    ``EnrichedTask.id`` (typed ``int``, required) — a call/text has no Buildium
    task. ``crc32`` is process-stable (unlike ``hash()``) and fits a signed
    32-bit int; the real identity (the SID) is preserved on ``raw``. Mirrors
    ``email_triage.email_synthetic_task_id``."""
    return zlib.crc32((sid or "").encode("utf-8")) & 0x7FFFFFFF


def _event_name(channel: str) -> str:
    return CALL_EVENT_NAME if channel == "voice" else SMS_EVENT_NAME


def comms_hazards(comms: InboundComms, *, known: Sequence[str] | None = None) -> list[str]:
    """Life-safety hazard slugs in the message/transcript (empty = none).

    A thin pass-through to the deterministic ``security.hazard_lexicon`` scan —
    the SAME scanner the CRM SMS path uses — so an inbound "gas leak" / "no
    heat" / "flooding" is flagged at ingest without waiting on an LLM. Never
    acts; the slugs bump task priority and seed a floor signal (below).

    ``known`` carries slugs scanned EARLIER, off the real body — the queue
    envelope's ``Hazards``. It is a FLOOR, never a replacement: the union of
    both scans, so a hazard either source saw survives.

    ⚠ **Why a floor rather than a re-scan.** The self-heal path rebuilds an
    ``InboundComms`` with an EMPTY body (the queue payload deliberately does
    not carry the message text), so re-deriving from it returns ``[]`` and a
    flagged gas-leak text quietly drops to ordinary priority — sorting like a
    lease question. The envelope field exists precisely to survive that, and
    was written and never read: gotchas 41/50/51's sentence, arriving on the
    one signal where under-flagging is a life-safety outcome and over-flagging
    costs a property manager a second look.
    """
    scanned = detect_hazards(comms.body)
    if not known:
        return scanned
    merged = list(scanned)
    merged += [slug for slug in known if slug and slug not in merged]
    return merged


def _hazard_signal(hazards: list[str]) -> str | None:
    """A deterministic "What Occupella noticed" line for a flagged hazard, or
    ``None`` when nothing was flagged. Names the slugs verbatim (stable
    identifiers a PM can act on); never softens the wording."""
    if not hazards:
        return None
    return (
        f"⚠ Possible life-safety hazard flagged automatically ({hazard_label(hazards)}) — "
        "verify and respond immediately."
    )


def build_comms_enrichment(
    comms: InboundComms, company_id: str, *, known_hazards: Sequence[str] | None = None
) -> dict[str, Any]:
    """Project an :class:`InboundComms` onto the ``EnrichedEvent`` JSON shape so
    the existing enrichment → planner → summarizer pipeline treats a call/text
    like the resident request it already handles.

    Mapping:
    - ``task.title``       ← ``"Text from Josh Gil"`` / ``"Call with Josh Gil"``,
      with a short body snippet for SMS so the Inbox row is scannable.
    - ``task.description`` ← the FULL body / transcript, verbatim.
    - ``task.category``    ← ``"Text message"`` / ``"Phone call"``.
    - ``task.requested_by_first_name/last_name`` ← the resolved contact's name.
    - ``task.raw``         ← channel, direction, normalized from/to numbers, the
      Twilio SID, and the resolved contact (kind + buildium_id) so the auto-task
      path can attribute the request to the right resident and a reply can be
      routed back to the right number.

    Returns a plain dict (not the pydantic ``EnrichedEvent``) to keep this
    module import-light — same choice as ``email_triage``.
    """
    ambiguous = is_ambiguous(comms)
    name = _contact_name(comms)
    first, _, last = name.strip().partition(" ")
    label = _channel_label(comms.channel)
    preposition = "with" if comms.channel == "voice" else "from"
    title = f"{label} {preposition} {name}".strip()
    if comms.channel == "sms":
        snippet = _snippet(comms.body)
        if snippet:
            title = f"{title}: {snippet}"

    contact = comms.contact
    # Deterministic life-safety scan bumps priority so an emergency text sorts
    # to the top and the auto-task inherits urgency — never depends on the LLM.
    # ⚠ ``known_hazards`` is the FLOOR. On the self-heal rebuild the body is
    # empty by design, so a re-scan alone answers "no hazard" for a message
    # that was flagged at ingest — the urgency drops silently and a gas-leak
    # text sorts beside a lease question. The envelope carries the original
    # slugs for exactly this; see comms_hazards.
    hazards = comms_hazards(comms, known=known_hazards)
    priority = "High" if hazards else None
    return {
        "schema_version": 1,
        "event_name": _event_name(comms.channel),
        "company_id": company_id,
        "task": {
            "id": comms_synthetic_task_id(comms.sid),
            "title": title,
            "description": comms.body or "",
            "status": None,
            "category": _category(comms.channel),
            "priority": priority,
            # ⚠ EMPTY when the number is shared. These two fields are the
            # ATTRIBUTION — the auto-task path files the request under this
            # person — so filling them from a precedence tiebreak files one
            # resident's request under their partner's name. Declining to name
            # anybody is recoverable; naming the wrong resident is not
            # (gotcha 55, pointed at a person).
            "requested_by_first_name": None if ambiguous else (first or None),
            "requested_by_last_name": None if ambiguous else (last or None),
            "raw": {
                "source": comms.provider,
                "channel": comms.channel,
                "direction": comms.direction,
                "sid": comms.sid,
                "from_number": normalize_e164(comms.from_number) or comms.from_number,
                "to_number": normalize_e164(comms.to_number) or comms.to_number,
                "occurred_at": comms.occurred_at,
                "media_urls": list(comms.media_urls),
                "hazards": hazards,
                "contact": (
                    {
                        "kind": contact.kind,
                        "buildium_id": contact.buildium_id,
                        "name": contact.name,
                    }
                    if contact is not None
                    else None
                ),
                # Present ONLY when the number is shared, so its presence is
                # itself the signal. A list of one would read as an ambiguity
                # on every ordinary text.
                "contact_candidates": (
                    [
                        {"kind": c.kind, "buildium_id": c.buildium_id, "name": c.name}
                        for c in comms.candidates
                    ]
                    if ambiguous
                    else None
                ),
            },
        },
        "unit": None,
        "active_lease": None,
        "history_unit": [],
        "history_property": [],
        "errors": [],
        "fetched_at": datetime.now(UTC).isoformat(),
    }


def build_comms_action_surface(
    comms: InboundComms,
    *,
    signals: list[str] | None = None,
    draft_reply: str | None = None,
) -> dict[str, Any]:
    """Project an :class:`InboundComms` into an ``InboxActionSurface`` dict.

    The deterministic floor: even with no LLM enrichment the Inbox renders the
    full message/transcript and a reply/call-back action. When enrichment has
    run, its ``signals`` ("What Occupella noticed") and a ``draft_reply`` (for
    an SMS, reviewed — never auto-sent) layer on top.

    The reply/call-back actions are intended to flow through the write-confirm
    gate when wired to Twilio, so nothing sends or dials without the user's tap.
    """
    name = _contact_name(comms)
    label = _channel_label(comms.channel).lower()
    from_e164 = normalize_e164(comms.from_number) or comms.from_number

    # Deterministic floor: a flagged hazard is always the first "noticed" line,
    # ahead of (and independent of) any LLM-supplied signals, so an emergency is
    # never lost to a model that's down or out of budget.
    floor_signals: list[str] = []
    hazard_line = _hazard_signal(comms_hazards(comms))
    if hazard_line:
        floor_signals.append(hazard_line)
    # ⚠ SECOND, behind the hazard and ahead of everything else. A gas leak is
    # the one thing that outranks it; the transcript caveat and the shared-
    # handset line are both about how to READ the message, while this one is
    # about what the message legally IS — answer it as a routine pet or
    # parking question and the duty is already missed.
    accommodation_line = accommodation_floor_signal(comms.body)
    if accommodation_line:
        floor_signals.append(accommodation_line)
    # ⚠ A DETERMINISTIC floor line, not a hope that the model notices the "or"
    # in the title. Acting on a shared handset as though one named resident
    # sent it is the harm — a rent promise, a maintenance entry consent, a
    # lease question answered to the wrong person in the household — and it is
    # the one thing here the manager can fix in a sentence by asking.
    # ⚠ VOICE only. An SMS body is what the sender actually typed; a voice
    # "body" is an ASR guess, and the figures in it — a rent amount, a unit
    # number — are exactly what a manager acts on and what ASR gets wrong.
    # Twilio sends no confidence value to gate on (see comms/transcript), so
    # naming what to check against the recording is the available control.
    if comms.channel == "voice":
        caveat = transcript_caveat(comms.body)
        if caveat:
            floor_signals.append(caveat)
    if is_ambiguous(comms):
        floor_signals.append(
            f"This number is on file for {len(comms.candidates)} people "
            f"({name}). Confirm who you are speaking to before acting on this "
            "as any one of them."
        )
    combined_signals = floor_signals + (list(signals) if signals else [])

    if comms.channel == "voice":
        primary = {
            "tool_id": "call_back",
            "label": f"Call {name} back",
            "intent": (
                "Place an outbound call to this contact through Twilio, after "
                "you confirm. Never auto-dials."
            ),
            "parameters": {"to": from_e164},
        }
    else:
        primary = {
            "tool_id": "send_reply",
            "label": "Review & send reply",
            "intent": (
                "Open the drafted text for editing; sends through Twilio only "
                "after you confirm. Never auto-sends."
            ),
            "parameters": {"to": from_e164},
        }

    return {
        "schema_version": COMMS_SURFACE_SCHEMA_VERSION,
        "kind": "comms",
        "summary": f"{_channel_label(comms.channel)} {label} {name}".strip(),
        "comms": {
            "sid": comms.sid,
            "channel": comms.channel,
            "direction": comms.direction,
            "from_number": from_e164,
            "to_number": normalize_e164(comms.to_number) or comms.to_number,
            "occurred_at": comms.occurred_at,
            "body": comms.body or "",
            "contact": (
                {
                    "kind": comms.contact.kind,
                    "buildium_id": comms.contact.buildium_id,
                    "name": comms.contact.name,
                }
                if comms.contact is not None
                else None
            ),
            "media_urls": list(comms.media_urls),
        },
        "signals": combined_signals,
        "draft_reply": draft_reply,
        "actions": [primary],
        "freeform_placeholder": "Edit the reply or add a note…",
    }


__all__ = [
    "is_ambiguous",
    "CALL_EVENT_NAME",
    "COMMS_SURFACE_SCHEMA_VERSION",
    "SMS_EVENT_NAME",
    "InboundComms",
    "build_comms_action_surface",
    "build_comms_enrichment",
    "comms_hazards",
    "comms_synthetic_task_id",
    "has_content",
    "is_known_contact",
    "should_surface_comms",
]
