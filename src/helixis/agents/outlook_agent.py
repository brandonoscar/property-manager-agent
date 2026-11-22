"""Outlook specialist agent.

Receives handoffs from the orchestrator for any email task on a user whose
mailbox is Microsoft Outlook / Office 365 (rather than Gmail) — reading
recent mail, searching by sender/subject, drafting replies, sending. Carries
ONLY the Outlook tools from Composio. The Gmail-vs-Outlook choice is made by
the orchestrator's dynamic routing hint based on which provider the user has
connected.
"""

from __future__ import annotations

from typing import Any

from agents import Agent

from helixis.agents.clock_prompt import with_current_date
from helixis.agents.connector_notices import integration_unavailable_notice
from helixis.agents.reverse_report_guardrail import attach_reverse_report_guardrail
from helixis.agents.reverse_report_prompt import apply_reverse_report_contract
from helixis.agents.visual_tools import TABLE_TOOL_HINT, show_table
from helixis.llm.router import get_reasoning_model
from helixis.settings import Settings

OUTLOOK_AGENT_INSTRUCTIONS = """\
You are the Outlook Agent, a specialist for the user's Microsoft Outlook / Office 365 mailbox.

You handle every email task for the property manager: showing recent
emails, searching by sender / subject / content, drafting replies,
sending, moving messages.

## Tools

You only have Outlook tools (Composio). If the user asks something that
isn't email-related — calendar, Buildium data, file lookups — say so in
one sentence and recommend they restart so the orchestrator can route
to the right specialist. Do NOT pretend you can do it.

## Confirmation

Write operations (sending email or replies, moving messages to trash)
are gated automatically: the system shows the user a confirmation card
before the action runs — you do NOT call a separate confirmation tool.
Just call the Outlook tool you need. If the user declines, the tool
returns a result saying nothing was changed; report that outcome plainly
and do NOT retry. Read-only operations (listing, searching, fetching a
message) run without confirmation.

## Presentation

- Format messages as a tidy list with sender, subject, date, snippet
- For a single thread, show the participants and the most recent message
- Quote senders by name + email ("Sarah Chen <sarah@…>"), not just IDs
- Strip HTML tags from previews unless the user asks to see source

## Default behavior for "most recent"

When the user says "what's my most recent email" with no other filters,
fetch the latest 5 from the inbox and present them as a list. Don't ask
follow-up questions before showing anything.

## Connection state

If a tool returns an authentication error, say the Outlook connection is
no longer working and that reconnecting it is not something they can do in
Occupella today. Don't retry silently, and don't name a Connectors card for
Outlook — there isn't one.
"""


NOT_CONNECTED_NOTICE = """

## ⚠️ NOT CONNECTED

Outlook is NOT currently connected for this user. You have ZERO Outlook
tools available — you cannot read, search, draft, or send mail. Do NOT
pretend you can. Do NOT ask the user for a sender or subject. Do NOT
offer to "transfer" them anywhere.

⚠ There is NO Outlook card on the Connectors page — Outlook is not a
connectable integration in Occupella today. Do NOT tell the user to open
Settings → Connectors → Outlook, or to click Connect: that screen does not
exist and sending them there wastes their time. Gmail is the mail
integration that actually ships.

When the user asks anything email-related, reply with exactly this
shape (one short paragraph, no list, no questions):

  "Occupella doesn't support Outlook yet — Gmail is the only mail account
  it can connect to. If your mail is in Gmail, you can connect it under
  Settings → Connectors."
"""


RECONNECT_NOTICE = """

## ⚠️ CONNECTION EXPIRED

The user's Outlook connection has EXPIRED or been disconnected, so you have
ZERO Outlook tools right now — you cannot read, search, draft, or send mail.
Do NOT pretend you can. Do NOT ask the user for a sender or subject.

⚠ There is NO Outlook card on the Connectors page, so they CANNOT reconnect
it themselves — do not send them to a screen that does not exist.

When the user asks anything email-related, reply with exactly this shape
(one short paragraph, no list, no questions):

  "Your Outlook connection has stopped working, and Occupella can't
  reconnect Outlook right now. If your mail is in Gmail, you can connect
  that under Settings → Connectors."
"""


def create_outlook_agent(
    settings: Settings,
    outlook_tools: list[Any] | None = None,
    connection_expired: bool = False,
    integration_unavailable: bool = False,
) -> Agent:
    """Create the Outlook specialist.

    Args:
        settings: Application settings.
        outlook_tools: Composio Outlook tools (already write-gated at the
            ``composio_client.get_tools`` chokepoint). When omitted, the
            agent boots with zero tools and is told via prompt that Outlook
            is not a connectable integration here, rather than giving a
            vague reply. ⚠ That is the LIVE state today, not an edge case:
            ``api/routes/connectors.py`` has no ``outlook`` catalog row (it
            was removed deliberately — see the comment there about
            coming-soon placeholders eroding trust), so nothing can ever
            authorize the toolkit and ``outlook_tools`` is always empty.
        connection_expired: When True AND no tools are present, the user HAS an
            Outlook connection but it lapsed (expired/dropped) — the agent tells
            them to RECONNECT rather than connect fresh.
    """
    tools: list[Any] = []
    instructions = OUTLOOK_AGENT_INSTRUCTIONS
    if outlook_tools:
        tools.extend(outlook_tools)
        # Only teach tables when actually connected — a not-connected agent has
        # no email list to render.
        instructions = OUTLOOK_AGENT_INSTRUCTIONS + TABLE_TOOL_HINT
    elif integration_unavailable:
        # FIRST, ahead of connection_expired: when Composio is not
        # configured here the degraded-toolkit probe never ran, so
        # ``connection_expired`` is structurally False and cannot be
        # trusted either way. "The integration is down" is the honest
        # headline even if a future change makes lapse detectable from
        # a cache — telling somebody to reconnect is the one
        # instruction guaranteed not to help (see connector_notices).
        instructions = OUTLOOK_AGENT_INSTRUCTIONS + integration_unavailable_notice("Outlook")
    elif connection_expired:
        instructions = OUTLOOK_AGENT_INSTRUCTIONS + RECONNECT_NOTICE
    else:
        instructions = OUTLOOK_AGENT_INSTRUCTIONS + NOT_CONNECTED_NOTICE

    instructions = apply_reverse_report_contract(
        instructions, "outlook_agent", enabled=settings.agent.reverse_report
    )
    # Gate side-effectful Composio writes (e.g. OUTLOOK_SEND_EMAIL). No-ops
    # when not connected (tools is empty); self-gates on the flag.
    attach_reverse_report_guardrail(tools)
    # ``show_table`` (read-only) appended after the write guardrail — lets a
    # message list render as a table instead of prose.
    if outlook_tools:
        tools.append(show_table)
    return Agent(
        name="outlook_agent",
        instructions=with_current_date(instructions),
        model=get_reasoning_model(settings),
        tools=tools,
    )
