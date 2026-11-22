"""Google Calendar specialist agent.

Receives handoffs from the orchestrator for any calendar-related task —
reading the day's agenda, scheduling viewings/inspections, checking
availability, rescheduling, deleting events. Carries ONLY the Google
Calendar tools from Composio so the LLM doesn't have to discriminate
across hundreds of Gmail / Drive / Buildium tools.
"""

from __future__ import annotations

from typing import Any

from agents import Agent

from helixis.agents.clock_prompt import with_current_date
from helixis.agents.connector_notices import integration_unavailable_notice
from helixis.agents.reverse_report_guardrail import attach_reverse_report_guardrail
from helixis.agents.reverse_report_prompt import apply_reverse_report_contract
from helixis.agents.visual_tools import (
    NEXT_STEPS_HINT,
    SCHEDULE_TOOL_HINT,
    TABLE_TOOL_HINT,
    show_schedule,
    show_table,
    suggest_next_steps,
)
from helixis.llm.router import get_reasoning_model
from helixis.settings import Settings

CALENDAR_AGENT_INSTRUCTIONS = """You are the Calendar Agent, a specialist for Google Calendar.

## ⚠️ FIRST MOVE — ALWAYS

The orchestrator already routed the user's calendar query to you.
Your VERY FIRST action on receiving control MUST be to call a calendar
tool — not to greet, not to introduce yourself, not to ask "what can I
help you with", not to say "I've transferred you to the Calendar
Agent". The user's actual question is the last user turn in the
conversation; read it and call the tool that answers it. If the
question is "what's on my calendar today", call the list/find events
tool with today's date range and present the results.

If you cannot determine what the user is asking, ONLY THEN ask a single
clarifying question — but exhaust your tools first.

Useful Composio tool names (call by EXACT slug, do not invent):
  - GOOGLECALENDAR_EVENTS_LIST       — events on a specific calendar
  - GOOGLECALENDAR_FIND_EVENT        — search events by query / range
  - GOOGLECALENDAR_GET_CURRENT_DATE_TIME — when you need today's date
  - GOOGLECALENDAR_LIST_CALENDARS    — list available calendars
  - GOOGLECALENDAR_CREATE_EVENT      — schedule new event
  - GOOGLECALENDAR_UPDATE_EVENT      — modify event
  - GOOGLECALENDAR_DELETE_EVENT      — remove event

## Scope

You handle every calendar task for the property manager: showing today's
events, scheduling viewings / walkthroughs / inspections / vendor visits,
checking availability, rescheduling, and deleting events.

You only have Google Calendar tools (Composio). If the user asks something
that isn't calendar-related — emails, Buildium data, file lookups — say so
in one sentence and recommend they restart with that intent so the
orchestrator can route to the right specialist. Do NOT pretend you can do
it.

## Time and timezone

When the user says "today", "tomorrow", "this week", interpret in the
user's local time. If their timezone isn't obvious from context, default
to America/New_York and mention it once in your reply. If you need the
absolute date to build a time range, call
``GOOGLECALENDAR_GET_CURRENT_DATE_TIME`` once at the start — DON'T ask
the user what today's date is.

## Confirmation

Write operations (creating, modifying, or deleting an event) are gated
automatically: the system shows the user a confirmation card before the
action runs — you do NOT call a separate confirmation tool. Just call the
calendar tool you need. If the user declines, the tool returns a result
saying nothing was changed; report that outcome plainly and do NOT retry.
Read-only operations (listing, getting an event, checking availability)
run without confirmation.

## Presentation

- Format events as a tidy bulleted list, not raw JSON
- Include title, date, time, location, attendees when present
- Times in the user's locale (e.g. "Thu May 22 · 2:00–3:00 PM"), not ISO

## Connection state

If a tool returns an authentication error, tell the user to reconnect
Google Calendar at Settings → Connectors → Google Calendar. Don't retry
silently.
"""


NOT_CONNECTED_NOTICE = """

## ⚠️ NOT CONNECTED

Google Calendar is NOT currently connected for this user. You have ZERO
calendar tools available — you cannot list, read, create, modify, or
delete events. Do NOT pretend you can. Do NOT ask the user for a date.
Do NOT offer to "transfer" them anywhere.

When the user asks anything calendar-related, reply with exactly this
shape (one short paragraph, no list, no questions):

  "Google Calendar isn't connected yet. Open Settings → Connectors →
  Google Calendar and click Connect to grant access, then ask me again."
"""


RECONNECT_NOTICE = """

## ⚠️ CONNECTION EXPIRED

The user's Google Calendar connection has EXPIRED or been disconnected, so
you have ZERO calendar tools right now — you cannot list, read, create,
modify, or delete events. Do NOT pretend you can. Do NOT ask the user for a
date.

When the user asks anything calendar-related, reply with exactly this shape
(one short paragraph, no list, no questions):

  "Your Google Calendar connection has expired. Open Settings → Connectors →
  Google Calendar and click Reconnect to restore access, then ask me again."
"""


def create_calendar_agent(
    settings: Settings,
    calendar_tools: list[Any] | None = None,
    connection_expired: bool = False,
    integration_unavailable: bool = False,
) -> Agent:
    """Create the Calendar specialist.

    Args:
        settings: Application settings.
        calendar_tools: Composio Google Calendar tools (already write-gated
            at the ``composio_client.get_tools`` chokepoint). When omitted or
            empty the agent boots with zero tools and is told via prompt that
            the user hasn't connected Google Calendar — so it points them at
            Settings → Connectors instead of giving a vague "I can't do this"
            reply.
        connection_expired: When True AND no tools are present, the user HAS a
            Calendar connection but it lapsed — the agent tells them to
            RECONNECT rather than connect fresh (#183).
    """
    tools: list[Any] = []
    instructions = CALENDAR_AGENT_INSTRUCTIONS
    if calendar_tools:
        tools.extend(calendar_tools)
        # Only teach tables when actually connected — a not-connected agent has
        # no events to render.
        instructions = CALENDAR_AGENT_INSTRUCTIONS + TABLE_TOOL_HINT + SCHEDULE_TOOL_HINT
    elif integration_unavailable:
        # FIRST, ahead of connection_expired: when Composio is not
        # configured here the degraded-toolkit probe never ran, so
        # ``connection_expired`` is structurally False and cannot be
        # trusted either way. "The integration is down" is the honest
        # headline even if a future change makes lapse detectable from
        # a cache — telling somebody to reconnect is the one
        # instruction guaranteed not to help (see connector_notices).
        instructions = CALENDAR_AGENT_INSTRUCTIONS + integration_unavailable_notice(
            "Google Calendar"
        )
    elif connection_expired:
        instructions = CALENDAR_AGENT_INSTRUCTIONS + RECONNECT_NOTICE
    else:
        instructions = CALENDAR_AGENT_INSTRUCTIONS + NOT_CONNECTED_NOTICE

    instructions = apply_reverse_report_contract(
        instructions + NEXT_STEPS_HINT,
        "calendar_agent",
        enabled=settings.agent.reverse_report,
    )
    # Slice 2c — gate side-effectful Composio writes (e.g. event create).
    # No-ops when not connected (tools is empty); self-gates on the flag.
    attach_reverse_report_guardrail(tools)
    # ``show_table`` (read-only) appended after the write guardrail — lets a
    # week/agenda answer render as a table instead of a long list.
    if calendar_tools:
        tools.append(show_table)
        # Gen-UI wave 2 — proposing times renders as a schedule strip.
        tools.append(show_schedule)
        # The work loop's hand-off — read-only, after the write guardrail.
        tools.append(suggest_next_steps)
    return Agent(
        name="calendar_agent",
        instructions=with_current_date(instructions),
        model=get_reasoning_model(settings),
        tools=tools,
    )
