"""Eval-only stub tools for the Composio-backed connector agents.

The Gmail / Calendar / Drive specialists carry tools that come from
Composio at runtime (``composio_client.get_tools``). Composio is DISABLED
in evals (``settings.composio.api_key == ""``), so those agents normally
boot with ZERO tools — which makes the happy-path ``agent_flow`` cases
(#298) impossible to write: there is no ``*_impl`` to mock, and no tool
for the agent to call.

This module provides lightweight stand-ins: one read tool per connector,
named after the real Composio slug the agent's prompt references, each
backed by a patchable module-level ``*_impl`` coroutine. The ``agent_flow``
harness (:mod:`helixis.evals.agent_flow`) injects these into the connector
agent for happy-path cases and patches the ``*_impl`` via ``tool_mocks``,
exactly like the Buildium read tools.

These are NEVER used in production — only the eval harness imports them.
Each tool returns a plain string (mirrors the Buildium tools), so the
harness's ``_build_string_return`` factory applies.

Naming: the public tool name (what shows up in the agent's tool list and
the captured tool-call trail) matches the real Composio slug via
``name_override`` so the connector prompts — which reference those exact
slugs — recognise the tool and one-shot it on live Gemini. The unmocked
``*_impl`` returns a marker string; cases override it through ``tool_mocks``.
"""

from __future__ import annotations

from agents import function_tool

# Returned by an unmocked stub impl. Cases override the impl via tool_mocks;
# this only surfaces if a case wires a stub tool but forgets to mock it.
_UNMOCKED = "(eval stub: not wired — provide a tool_mocks entry for this tool)"


# ── Gmail ────────────────────────────────────────────────────────


async def _gmail_fetch_emails_impl(query: str) -> str:
    return _UNMOCKED


@function_tool(name_override="GMAIL_FETCH_EMAILS")
async def gmail_fetch_emails(query: str) -> str:
    """Fetch the user's recent Gmail messages.

    Pass a search query (sender, subject, or keywords) to filter, or an
    empty string to get the most recent inbox messages. Returns a short
    list with sender, subject, date, and snippet.
    """
    return await _gmail_fetch_emails_impl(query)


async def _gmail_send_email_impl(recipient: str, subject: str, body: str) -> str:
    return _UNMOCKED


@function_tool(name_override="GMAIL_SEND_EMAIL")
async def gmail_send_email(recipient: str, subject: str, body: str) -> str:
    """Send an email via Gmail. ``recipient`` is the destination address,
    ``subject`` the subject line, ``body`` the message text. Returns a
    confirmation once the message is sent. (Eval stub for the WRITE path —
    #359. In production this Composio write is confirm-gated.)
    """
    return await _gmail_send_email_impl(recipient, subject, body)


async def _gmail_reply_to_thread_impl(thread_id: str, message_body: str) -> str:
    return _UNMOCKED


@function_tool(name_override="GMAIL_REPLY_TO_THREAD")
async def gmail_reply_to_thread(thread_id: str, message_body: str) -> str:
    """Reply to an existing Gmail thread. ``thread_id`` identifies the thread
    (obtain it from a prior fetch/search), ``message_body`` is the reply text.
    Returns a confirmation once the reply is sent. (Eval stub for the WRITE
    path — same GMAIL_REPLY_TO_THREAD slug the production reply_to_email
    handler uses; confirm-gated in production.)
    """
    return await _gmail_reply_to_thread_impl(thread_id, message_body)


# ── Calendar ─────────────────────────────────────────────────────


async def _googlecalendar_events_list_impl(time_range: str) -> str:
    return _UNMOCKED


@function_tool(name_override="GOOGLECALENDAR_EVENTS_LIST")
async def googlecalendar_events_list(time_range: str) -> str:
    """List Google Calendar events for a time range (e.g. "today",
    "this week"). Returns title, date, time, location, and attendees for
    each event.
    """
    return await _googlecalendar_events_list_impl(time_range)


async def _googlecalendar_create_event_impl(
    title: str, start: str, end: str = "", location: str = ""
) -> str:
    return _UNMOCKED


@function_tool(name_override="GOOGLECALENDAR_CREATE_EVENT")
async def googlecalendar_create_event(
    title: str, start: str, end: str = "", location: str = ""
) -> str:
    """Create a Google Calendar event. ``title`` names it, ``start`` /
    ``end`` are ISO datetimes, ``location`` is optional. Returns the
    created event's confirmation. (Eval stub for the WRITE path — #359.
    In production this Composio write is confirm-gated.)
    """
    return await _googlecalendar_create_event_impl(title, start, end, location)


async def _googlecalendar_find_event_impl(query: str) -> str:
    return _UNMOCKED


@function_tool(name_override="GOOGLECALENDAR_FIND_EVENT")
async def googlecalendar_find_event(query: str) -> str:
    """Find a specific Google Calendar event by title/keyword and (optionally)
    date. Returns the matching event's title, date, time, location, and
    event id. (Eval stub — the real Composio Calendar toolkit exposes
    FIND_EVENT alongside EVENTS_LIST, and Sonnet legitimately reaches for it
    to resolve an event id before an update; without this stub that choice
    crashed the run with "Tool not found" — first seen red-gating the
    2026-07-22 main merge on case 058.)
    """
    return await _googlecalendar_find_event_impl(query)


async def _googlecalendar_update_event_impl(
    event_id: str, start: str = "", end: str = "", title: str = ""
) -> str:
    return _UNMOCKED


@function_tool(name_override="GOOGLECALENDAR_UPDATE_EVENT")
async def googlecalendar_update_event(
    event_id: str, start: str = "", end: str = "", title: str = ""
) -> str:
    """Modify an existing Google Calendar event. ``event_id`` identifies the
    event (obtain it from a prior events-list call); pass whichever of
    ``start`` / ``end`` / ``title`` are changing. Returns the updated event's
    confirmation. (Eval stub for the WRITE path — #359 follow-up. In
    production this Composio write is confirm-gated.)
    """
    return await _googlecalendar_update_event_impl(event_id, start, end, title)


# ── Outlook ──────────────────────────────────────────────────────


async def _outlook_list_messages_impl(query: str) -> str:
    return _UNMOCKED


@function_tool(name_override="OUTLOOK_LIST_MESSAGES")
async def outlook_list_messages(query: str) -> str:
    """List the user's recent Microsoft Outlook / Office 365 messages.

    Pass a search query (sender, subject, or keywords) to filter, or an
    empty string to get the most recent inbox messages. Returns a short
    list with sender, subject, date, and snippet. (Eval stub — #386. The
    real Outlook tools come from Composio, disabled in evals.)
    """
    return await _outlook_list_messages_impl(query)


# ── Drive ────────────────────────────────────────────────────────


async def _googledrive_find_file_impl(query: str) -> str:
    return _UNMOCKED


@function_tool(name_override="GOOGLEDRIVE_FIND_FILE")
async def googledrive_find_file(query: str) -> str:
    """Search Google Drive for files by name or content. Returns title,
    folder, and last-modified for each match.
    """
    return await _googledrive_find_file_impl(query)


# ── registry consumed by the agent_flow harness ──────────────────

# agent name -> (factory keyword for its tools, list of eval stub tools).
# Read + write stubs per connector: the write tools (#359) let happy-path
# WRITE cases exercise the confirm-gated Composio send/create path, and the
# Outlook stub (#386) gives the Outlook specialist a read tool to call.
CONNECTOR_EVAL_TOOLS: dict[str, tuple[str, list]] = {
    "Gmail Agent": ("gmail_tools", [gmail_fetch_emails, gmail_send_email, gmail_reply_to_thread]),
    "Calendar Agent": (
        "calendar_tools",
        [
            googlecalendar_events_list,
            googlecalendar_find_event,
            googlecalendar_create_event,
            googlecalendar_update_event,
        ],
    ),
    "Drive Agent": ("drive_tools", [googledrive_find_file]),
    "Outlook Agent": ("outlook_tools", [outlook_list_messages]),
}

# slug -> dotted path of the patchable *_impl (added to _TOOL_MOCK_TARGETS).
CONNECTOR_MOCK_IMPL_PATHS: dict[str, str] = {
    "GMAIL_FETCH_EMAILS": "helixis.evals.connector_stub_tools._gmail_fetch_emails_impl",
    "GMAIL_SEND_EMAIL": "helixis.evals.connector_stub_tools._gmail_send_email_impl",
    "GMAIL_REPLY_TO_THREAD": "helixis.evals.connector_stub_tools._gmail_reply_to_thread_impl",
    "GOOGLECALENDAR_EVENTS_LIST": (
        "helixis.evals.connector_stub_tools._googlecalendar_events_list_impl"
    ),
    "GOOGLECALENDAR_FIND_EVENT": (
        "helixis.evals.connector_stub_tools._googlecalendar_find_event_impl"
    ),
    "GOOGLECALENDAR_CREATE_EVENT": (
        "helixis.evals.connector_stub_tools._googlecalendar_create_event_impl"
    ),
    "GOOGLECALENDAR_UPDATE_EVENT": (
        "helixis.evals.connector_stub_tools._googlecalendar_update_event_impl"
    ),
    "GOOGLEDRIVE_FIND_FILE": "helixis.evals.connector_stub_tools._googledrive_find_file_impl",
    "OUTLOOK_LIST_MESSAGES": "helixis.evals.connector_stub_tools._outlook_list_messages_impl",
}


__all__ = [
    "CONNECTOR_EVAL_TOOLS",
    "CONNECTOR_MOCK_IMPL_PATHS",
    "gmail_fetch_emails",
    "gmail_reply_to_thread",
    "gmail_send_email",
    "googlecalendar_create_event",
    "googlecalendar_events_list",
    "googlecalendar_update_event",
    "googledrive_find_file",
    "outlook_list_messages",
]
