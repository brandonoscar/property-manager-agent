"""What the company has already told prospects — remembered, and recalled.

Every text and email in a lead's thread has been stored since migration 0029:
``crm_lead_messages`` keeps the full body both directions, idempotent on the
Twilio SID. Nothing read it back. So the drafting assist saw the CURRENT
lead's thread and the company's uploaded documents, and knew nothing about the
forty replies the manager had already approved — the pet policy they actually
quote, the application fee they actually charge, the tour times they actually
offer. It could not get better from use, because nothing fed use back.

Two functions close that loop:

    remember_approved_reply()   after a reply goes out
    recall_for_lead()           before the next draft is written

⚠ **OUTBOUND ONLY, and that is the load-bearing decision.** An inbound message
is a stranger's question; storing it as a company fact would fill memory with
other people's asks and with the personal data of people who never became
customers. An OUTBOUND reply is different in kind: a human approved it, so it
is the company saying what it does. That is the only half worth learning from.

⚠ **An enhancement, never load-bearing** (gotchas 7 and 17). Both functions
swallow ``(Exception, SystemExit)`` and degrade to no-op / empty string. A
failed memory write must never cost a reply that already went out, and a failed
read must never block a drawer from opening. ``SystemExit`` is in there because
a spaCy-backed transitive path can call ``sys.exit`` and it is a
``BaseException`` that slips past ``except Exception``.

⚠ **Company-scoped, always** (gotcha 47). ``company_id`` is passed on every
call. Recalling one landlord's approved replies while drafting for another is
the cross-tenant leak this product's worst failures are made of.
"""

from __future__ import annotations

import logging

from helixis.db.repositories.leads import LeadRow, MessageRow
from helixis.memory.manager import get_memory_manager
from helixis.security.pii import redact_for_memory

logger = logging.getLogger(__name__)

#: How many past replies to bring back. Small on purpose: this rides into the
#: draft prompt alongside the company profile and the thread itself, and a
#: dozen half-relevant lines would crowd out the lead's actual question.
_RECALL_TOP_K = 4

#: Below this, a reply is an acknowledgement ("Sounds good — see you then")
#: rather than a fact about how the company operates. Storing those teaches
#: the model nothing and costs an extraction call each.
_MIN_REPLY_CHARS = 40


async def remember_approved_reply(
    *,
    company_id: str,
    lead: LeadRow | None,
    body: str,
    channel: str,
    user_id: str = "",
) -> None:
    """Store an approved outbound reply as something the company says.

    Called from the one shared tail both channels pass through, so a new
    channel cannot quietly skip it.
    """
    text = (body or "").strip()
    if not company_id or len(text) < _MIN_REPLY_CHARS:
        return
    try:
        manager = get_memory_manager()
        if manager is None:
            return
        unit = getattr(lead, "unit_label", None) or ""
        # The unit is prepended as CONTEXT, not stored as prose the model might
        # quote back: "we allow cats" means something different at Maple Court
        # than portfolio-wide, and a fact recalled without its property is the
        # confident-false-claim shape this repo keeps re-learning (gotcha 41).
        content = f"Reply sent about {unit}: {text}" if unit else f"Reply sent to a lead: {text}"
        await manager.add(
            # ⚠ redact_for_memory, NOT redact. The strong one strips names,
            # emails and phone numbers — the operational facts memory exists to
            # recall — and would leave "Reply sent about : " behind. This one
            # removes only the never-store classes (SSN, card, IBAN).
            redact_for_memory(content),
            company_id,
            stored_by=user_id,
            category="leasing",
            extra_metadata={
                "source": "crm_reply",
                "channel": channel,
                "lead_id": getattr(lead, "id", "") or "",
            },
        )
    except (Exception, SystemExit):  # noqa: BLE001 - never costs a sent reply
        logger.exception(
            "Failed to remember an approved reply (company=%s lead=%s)",
            company_id,
            getattr(lead, "id", None),
        )


def _latest_inbound(messages: list[MessageRow]) -> str:
    for message in reversed(messages):
        if message.direction == "inbound" and (message.body or "").strip():
            return message.body.strip()
    return ""


async def recall_for_lead(
    *,
    company_id: str,
    lead: LeadRow | None,
    messages: list[MessageRow],
) -> str:
    """Past approved replies relevant to what this lead is asking.

    Returns a markdown block for the draft prompt, or ``""`` when there is
    nothing to add — which is the common case early on, and is why the caller
    must treat an empty string as normal rather than as a failure.
    """
    if not company_id:
        return ""
    # ⚠ Query on what the lead ASKED, not on the lead's name. A name matches
    # every past reply that greeted anyone and none of the ones that answer
    # this question — it would return the most confidently irrelevant four
    # lines available.
    query = _latest_inbound(messages) or (getattr(lead, "unit_label", None) or "")
    if not query.strip():
        return ""
    try:
        manager = get_memory_manager()
        if manager is None:
            return ""
        hits = await manager.search(query, company_id, top_k=_RECALL_TOP_K)
    except (Exception, SystemExit):  # noqa: BLE001 - never blocks the drawer
        logger.exception("Failed to recall past replies (company=%s)", company_id)
        return ""

    lines = [str(h.get("memory") or h.get("text") or "").strip() for h in hits or []]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    # ⚠ Labelled as PAST replies, and hedged. These are retrieved by semantic
    # similarity, so a close-but-wrong hit is expected — the prompt has to be
    # able to ignore one. Presenting them as established policy is how a
    # neighbouring property's pet rule becomes this property's answer.
    body = "\n".join(f"- {line}" for line in lines)
    return (
        "How this company has answered similar questions before "
        "(past approved replies — use only if clearly relevant):\n" + body
    )
