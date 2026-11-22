"""Google Workspace files specialist agent — Drive, Docs and Sheets.

Receives handoffs from the orchestrator for any stored-document task:
finding SOPs / lease docs / property notes, reading file contents, listing
folders, searching by filename or query, and reading or drafting the Docs
and Sheets that live in that Drive.

⚠ THREE Composio toolkits, ONE specialist (2026-08-24). Docs and Sheets are
separate OAuth grants but feed this same agent, because the files live in
Drive and "find my SOP" / "read that rent roll" is a single user intent.
Splitting them into three agents would re-create the specialist fan-out that
gotchas 25/32/34 took four PRs to bound. Each toolkit contributes its tools
independently, so a user who connected only Drive is unaffected.
"""

from __future__ import annotations

from typing import Any

from agents import Agent

from helixis.agents.clock_prompt import with_current_date
from helixis.agents.connector_notices import integration_unavailable_notice
from helixis.agents.drive_lookup_tools import drive_find_files
from helixis.agents.reverse_report_guardrail import attach_reverse_report_guardrail
from helixis.agents.reverse_report_prompt import apply_reverse_report_contract
from helixis.agents.visual_tools import (
    NEXT_STEPS_HINT,
    TABLE_TOOL_HINT,
    show_table,
    suggest_next_steps,
)
from helixis.llm.router import get_reasoning_model
from helixis.settings import Settings

DRIVE_AGENT_INSTRUCTIONS = """You are the Drive Agent, a specialist for the
user's Google Workspace files — Drive, Docs and Sheets.

You help the property manager find and read documents — SOPs, lease
agreements, vendor contracts, inspection reports, property notes, rent
rolls and owner statements — and quote them in chat so they don't have to
leave Occupella to look something up.

## Tools

You have Google Drive, Google Docs and Google Sheets tools (Composio).
Which of the three you actually have depends on what the user connected:
work with the tools you were given and never assume a toolkit is present.
If the user asks for something none of them covers — calendar, email,
Buildium data — say so in one sentence and recommend they restart so the
orchestrator can route to the right specialist.

## Finding a file: ONE search, then open

``drive_find_files`` searches a cached index of EVERY file in the connected
Drive. It costs no Drive API call and answers instantly.

**Start there for any "find / where is / do we have" question, call it ONCE,
and use the words that matter.** Do not enumerate folders, do not run a
second search to narrow the first, and do not open files to find out what
they are — the index already gives you every title with its kind, folder and
modified date. Repeated searching is the single most expensive thing you can
do: a turn that consults you takes minutes when it is spent walking Drive,
and seconds when it is one indexed search plus one read.

Then open ONLY the file you actually need, by ``file_id``, with your
Drive/Docs/Sheets tools.

Two rules about what the index does and doesn't tell you:

- It searches TITLES, not contents. An empty result means no title matched —
  it is NEVER evidence the document doesn't exist. Say what you searched.
- A title flagged as superseded/old is listed last for a reason. Prefer the
  live copy, and never quote a superseded policy as current. If the only
  match is superseded, say so rather than quoting it as if it stands.

## Spreadsheets

A spreadsheet range you did not read is not data you have. Read the range
before you summarise it, state which sheet and range a number came from,
and never extrapolate a total from a partial read — say what you read and
offer to read the rest. A truncated read is never evidence of absence.

## Confirmation

Read operations (search, list, get file content) run without
confirmation — just do them. Write operations (upload, move, delete,
share) are gated automatically: the system shows the user a confirmation
card before the action runs — you do NOT call a separate confirmation
tool. Just call the Drive tool you need. If the user declines, the tool
returns a result saying nothing was changed; report that outcome plainly
and do NOT retry.

## Presentation

- Show file results as a tidy list: title, folder/path, last modified
- When quoting a document's contents, label which document it came from
  ("From 'Smith Lease 2026.pdf': …")
- Don't dump entire files — quote the relevant passage and offer to
  fetch more if the user asks
- Skip raw IDs and URLs in user-facing replies

## Connection state

If a tool returns an authentication error, tell the user to reconnect the
Google service whose tool failed — Settings → Connectors → Google Drive
for a Drive tool, and the Google Docs or Google Sheets row for those.
Name the one that failed rather than all three. Don't retry silently.
"""


NOT_CONNECTED_NOTICE = """

## ⚠️ NOT CONNECTED

No Google file service is connected for this user — not Drive, not Docs,
not Sheets. You have ZERO file tools available: you cannot search, read, or
list anything. Do NOT pretend you can. Do NOT ask the user for a filename.
Do NOT offer to "transfer" them anywhere.

When the user asks anything file-related, reply with exactly this
shape (one short paragraph, no list, no questions):

  "Google Drive isn't connected yet. Open Settings → Connectors →
  Google Drive and click Connect to grant access, then ask me again."
"""


RECONNECT_NOTICE = """

## ⚠️ CONNECTION EXPIRED

The user's Google file connections have EXPIRED or been disconnected, so you
have ZERO file tools right now — you cannot search, read, or list anything. Do
NOT pretend you can. Do NOT ask the user for a filename.

When the user asks anything file-related, reply with exactly this shape
(one short paragraph, no list, no questions):

  "Your Google Drive connection has expired. Open Settings → Connectors →
  Google Drive and click Reconnect to restore access, then ask me again."
"""


def create_drive_agent(
    settings: Settings,
    drive_tools: list[Any] | None = None,
    connection_expired: bool = False,
    integration_unavailable: bool = False,
) -> Agent:
    """Create the Drive specialist.

    ``drive_tools`` are Composio tools already write-gated at the
    ``composio_client.get_tools`` chokepoint. ``connection_expired`` (True AND
    no tools) means the user's Drive grant lapsed — the agent tells them to
    RECONNECT rather than connect fresh (#183).
    """
    tools: list[Any] = []
    instructions = DRIVE_AGENT_INSTRUCTIONS
    if drive_tools:
        tools.extend(drive_tools)
        # The indexed search (drive_lookup_tools). Registered ONLY alongside a
        # live connection: with no Composio tools the agent cannot open what
        # the index finds, and a search that can only ever end in "I can't read
        # it" is worse than the not-connected notice below.
        #
        # Read-only and DB-backed, so it is appended before the write guardrail
        # like show_table — there is nothing here for the confirm gate to hold.
        tools.append(drive_find_files)
        # Only teach tables when actually connected — a not-connected agent has
        # no file list to render.
        instructions = DRIVE_AGENT_INSTRUCTIONS + TABLE_TOOL_HINT
    elif integration_unavailable:
        # FIRST, ahead of connection_expired: when Composio is not
        # configured here the degraded-toolkit probe never ran, so
        # ``connection_expired`` is structurally False and cannot be
        # trusted either way. "The integration is down" is the honest
        # headline even if a future change makes lapse detectable from
        # a cache — telling somebody to reconnect is the one
        # instruction guaranteed not to help (see connector_notices).
        instructions = DRIVE_AGENT_INSTRUCTIONS + integration_unavailable_notice("Google Drive")
    elif connection_expired:
        instructions = DRIVE_AGENT_INSTRUCTIONS + RECONNECT_NOTICE
    else:
        instructions = DRIVE_AGENT_INSTRUCTIONS + NOT_CONNECTED_NOTICE

    instructions = apply_reverse_report_contract(
        instructions + NEXT_STEPS_HINT,
        "drive_agent",
        enabled=settings.agent.reverse_report,
    )
    # Slice 2c — gate side-effectful Composio writes (e.g. file create/upload).
    # No-ops when not connected (tools is empty); self-gates on the flag.
    attach_reverse_report_guardrail(tools)
    # ``show_table`` (read-only) appended after the write guardrail — lets a
    # file-search result render as a table (name / owner / modified).
    if drive_tools:
        tools.append(show_table)
        # The work loop's hand-off. Drive is LOOKUP-only by contract, so the
        # steps it offers must stay lookups ("open the signed lease") — never
        # the advice the routing prose forbids it from giving.
        tools.append(suggest_next_steps)
    return Agent(
        name="drive_agent",
        instructions=with_current_date(instructions),
        model=get_reasoning_model(settings),
        tools=tools,
    )
