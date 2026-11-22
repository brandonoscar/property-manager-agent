"""SSE event schema — every event the backend streams to the frontend."""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EventType(StrEnum):
    # LLM streaming
    THINKING = "thinking"
    TOKEN = "token"

    # Tool activity
    TOOL_START = "tool_start"
    TOOL_RESULT = "tool_result"

    # Routing — which specialist the orchestrator handed off to. Surfaced in
    # the chat "activity trail" so the user can see the work ("→ Buildium
    # Agent"), not just a neutral "Working…".
    HANDOFF = "handoff"

    # Browser automation
    BROWSER_START = "browser_start"
    BROWSER_ACTION = "browser_action"
    BROWSER_DONE = "browser_done"

    # Memory
    MEMORY_RECALL = "memory_recall"

    # Task checklist
    TODO_UPDATE = "todo_update"

    # Confirmation gate
    CONFIRM = "confirm"

    # Deliverables
    FILE = "file"

    # Lifecycle
    DONE = "done"
    ERROR = "error"

    # The model's own REASONING, streamed as it thinks. Distinct from
    # ``THINKING`` (which is the prose it writes for the user between tool
    # calls) and from ``TOKEN`` (the answer): this is the working-out, and the
    # client renders it dimmed and collapsible so it never competes with the
    # answer. Additive — a client that doesn't know the type ignores it.
    REASONING = "reasoning"

    # A step taken INSIDE a nested specialist run. Out on the parent stream a
    # whole consult is one ``TOOL_START`` and, ninety seconds later, one
    # ``TOOL_RESULT`` — this is what happens in between. Rendered under the
    # consult's own row and NOT counted as a step: the parent took one step,
    # and reporting the specialist's fan-out as the parent's work would make
    # the trail's summary meaningless.
    SUBSTEP = "substep"

    # One sentence saying what was ASKED, published within a couple of seconds
    # of Enter — before any tool call, before the model has finished thinking.
    # The step trail, the reasoning stream and the specialist substeps all
    # arrive after the model starts; this is the only thing that can fill the
    # first few seconds, which is where "is this broken?" gets decided.
    RESTATEMENT = "restatement"

    # PR #70 — conversational engine. ``NARRATION`` is a short, italic
    # status line ("Reading Josh Gil's lease history…"); the assistant-
    # message trio (start/token/end) wraps streamed prose so the front-
    # end can render a typing cursor inside the bubble; ``SUGGESTION_CHIP``
    # is a soft pill the user can click; ``CONFIRM_GATE`` pauses the
    # thread for an explicit confirm before any write fires.
    NARRATION = "narration"
    ASSISTANT_MESSAGE_START = "assistant_message_start"
    ASSISTANT_MESSAGE_TOKEN = "assistant_message_token"
    ASSISTANT_MESSAGE_END = "assistant_message_end"
    SUGGESTION_CHIP = "suggestion_chip"
    CONFIRM_GATE = "confirm_gate"
    WATCHER_UPDATE = "watcher_update"

    # Structured visual answer. The agent can call ``show_table`` mid-turn to
    # render a clean table instead of a long text list; the orchestrator drains
    # the emission and publishes this. The frontend already renders it
    # (ConversationThread ``tabular_data`` case → TabularContent).
    TABULAR_DATA = "tabular_data"

    # Structured map answer. A location tool (``properties_near`` /
    # ``find_places_near_property``) auto-emits this when it produces geocoded
    # points, so a static map renders inline in chat WITHOUT the model having to
    # relay a URL. Drained by the orchestrator like ``tabular_data``; rendered
    # by ConversationThread's ``map`` case → MapCard.
    MAP = "map"

    # An email draft the agent composed and chose to present as a real email
    # card (To / Subject / body) instead of chat prose. ``show_email_draft``
    # queues it; drained by the orchestrator like ``tabular_data``; rendered by
    # ConversationThread's ``email_draft`` case → EmailDraftCard. Purely a
    # presentation of the model's OWN composed draft — nothing is sent, and
    # sending still passes the confirmation gate. Additive: clients that don't
    # know the type ignore it.
    EMAIL_DRAFT = "email_draft"

    # Gen-UI wave 2 (2026-08-19) — four more card types on the shared card
    # queue (``visual_tools.drain_pending_cards``). Each is the model's OWN
    # composed content rendered as a typed card; all additive — old clients
    # ignore unknown types.
    DETAIL_CARD = "detail_card"  # one record's labeled facts → DetailCard
    CHART = "chart"  # a numeric series, self-rendered SVG → ChartCard
    SCHEDULE = "schedule"  # a day's time slots → ScheduleCard
    CHECKLIST = "checklist"  # a multi-step plan with done-flags → ChecklistCard
    NEXT_STEPS = "next_steps"  # the offered follow-up moves → NextStepsCard

    # Gen-UI wave 3 (2026-09-01) — the card that ASKS instead of telling.
    # When a request is too vague to act on ("make a task for the leaky
    # faucet"), the model renders the blanks as a form rather than writing a
    # paragraph of questions. Submitting sends an ordinary chat message: this
    # is a better way of asking, NOT a write path, so every actual Buildium
    # write still goes through its own confirm gate and role floor.
    FORM = "form"  # fields the model needs filled in → FormCard

    # Downloadable artifacts a code-execution run produced. ``run_computation``
    # auto-emits this when it writes files to OUTPUT_DIR and persists them —
    # each carries a signed download URL — so a "generate this CSV / statement"
    # renders download buttons inline WITHOUT the model relaying the link.
    # Drained by the orchestrator like ``map``; rendered by ArtifactCard.
    # Additive: clients that don't know the type ignore it.
    ARTIFACTS = "artifacts"

    # Progressive disclosure (W2 S6, reasoning-loop mode only). A three-layer
    # answer envelope DERIVED server-side after the run finishes — clean answer
    # (SYNTHESIZE footer stripped) / parsed reasoning (sources, confidence,
    # unverified, specialist trail) / audit pointers (run_id, trace_id). Emitted
    # once, before ``done``. Additive: clients that don't know the type ignore
    # it (the tabular_data/map precedent; ADR 0003).
    DISCLOSURE = "disclosure"


#: ``tool_name`` prefix for a persisted card row in ``helixis_chat_messages``.
#:
#: Cards are published on the live SSE stream and were never written down, so
#: reloading a thread replayed the prose and dropped every card. They are
#: stored as ordinary ``role='tool'`` rows — the column, the replay endpoint
#: and its response model already carried everything needed — with the card's
#: EventType value after this prefix.
#:
#: ⚠ The frontend hydrates on the same literal (`CARD_HISTORY_PREFIX` in
#: `conversation/types.ts`). A string crossing a producer/consumer boundary
#: needs a test that reads BOTH sides — see `test_card_event_types.py`.
CARD_HISTORY_PREFIX = "card:"


class HelixisEvent(BaseModel):
    """A single event streamed from the backend to the frontend via SSE."""

    type: EventType
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: float = Field(default_factory=time.time)

    def to_sse(self) -> str:
        """Format as an SSE data line."""
        return f"data: {self.model_dump_json()}\n\n"


# --- Factory helpers for clean event construction ---


def thinking_event(content: str) -> HelixisEvent:
    return HelixisEvent(type=EventType.THINKING, data={"content": content})


def restatement_event(content: str) -> HelixisEvent:
    """What the user asked, said back to them.

    ⚠ A RESTATEMENT, never an intention. Saying "I'll send the late notice"
    to somebody who asked whether one is warranted tells them the agent is
    about to do something they did not ask for, in the seconds before they can
    check — and an incorrect rationale damages trust more than none at all.
    ``agents/restatement.py`` holds the deterministic gates that enforce it;
    this is only the envelope.
    """
    return HelixisEvent(type=EventType.RESTATEMENT, data={"content": content})


def substep_event(content: str) -> HelixisEvent:
    """One step a specialist took inside its nested run.

    ⚠ The consult is the longest silent window in the product: gotcha 37
    measured 195 of 232 tool calls as nested agent runs, and out on the parent
    stream all of that is a single tool row that sits there for ninety
    seconds. This is the work, reported as it happens.

    ⚠ Scrubbed by the caller, like ``thinking_event`` and ``reasoning_event``
    — a specialist's step names carry record identifiers.
    """
    return HelixisEvent(type=EventType.SUBSTEP, data={"content": content})


def reasoning_event(content: str) -> HelixisEvent:
    """The model's working-out, streamed while it happens.

    ⚠ NOT the answer and NOT the plan. ``TOKEN`` carries the answer;
    ``THINKING`` carries the sentence the model writes for the user before a
    tool call. This is the reasoning underneath both, and it is the only signal
    that exists during the long silent windows — a specialist consult is one
    uninterrupted stretch with nothing on the wire (gotcha 37: 195 of 232 tool
    calls were nested agent runs), which is precisely the stretch that reads as
    a hang.

    ⚠ THE CALLER MUST SCRUB, exactly as ``thinking_event``'s caller does.
    Reasoning is full of raw record numbers and this text reaches the screen,
    so a call site that skips ``scrub_reply_text`` is a leak with a different
    name (gotcha 38: "the rule improved for one surface never reached the
    other"). Scrubbing is not done here because the orchestrator must buffer to
    a line boundary first — a scrubber cannot redact half an identifier split
    across two deltas. ``test_reasoning_stream.py`` AST-checks the call sites.
    """
    return HelixisEvent(type=EventType.REASONING, data={"content": content})


def token_event(content: str) -> HelixisEvent:
    return HelixisEvent(type=EventType.TOKEN, data={"content": content})


def tool_start_event(tool: str, description: str, call_id: str = "") -> HelixisEvent:
    """A step beginning.

    ⚠ ``call_id`` is the CORRELATION KEY and the reason this signature grew.
    The SDK runs a turn's tool calls CONCURRENTLY (CLAUDE.md gotcha 25 — the
    call budget had to reserve before awaiting for exactly that reason), so a
    turn can have five ``buildium_agent`` calls in flight at once. Without an
    id the client can only pair starts to results by NAME AND ARRIVAL ORDER,
    which mis-attributes the failure when calls 1, 2, 4, 5 return and 3 does
    not: the first four render resolved and the LAST one renders as the
    failure. Same defect Dify (#31247) and AG-UI (#1016) both carry.

    Every protocol that handles this uses one key and nothing else —
    Anthropic's content-block ``index``, AG-UI's ``toolCallId``. So do we.

    ⚠ Optional, and the client keeps its name+count fallback, because a
    RESUMED or stored stream replays events emitted before this existed.
    """
    return HelixisEvent(
        type=EventType.TOOL_START,
        data={"tool": tool, "description": description, "call_id": call_id},
    )


def tool_result_event(
    tool: str,
    result: str,
    duration_ms: int,
    call_id: str = "",
    status: str = "ok",
) -> HelixisEvent:
    """A step finishing, and HOW it finished.

    ⚠ ``status`` is ``ok`` | ``error`` | ``empty``, and the third value is not
    a nicety. A step that came back with no rows is NOT evidence the record
    does not exist — this product's worst failure mode is a confident absence
    (gotcha 26) — so "empty" has to be distinguishable from "ok" at the wire,
    or the client cannot render the difference and the user reads a blank as
    an answer.

    ⚠ A per-tool status is also what a run-level ``error`` event cannot give
    you: one failing tool among four successes should mark ONE row, not poison
    the turn. AG-UI has no per-tool error field at all and has to smuggle it;
    we have the room, so we use it.
    """
    return HelixisEvent(
        type=EventType.TOOL_RESULT,
        data={
            "tool": tool,
            "result": result,
            "duration_ms": duration_ms,
            "call_id": call_id,
            "status": status,
        },
    )


#: The three ways a step can end. ``empty`` is deliberately NOT folded into
#: ``ok``: a read that returned no rows and a read that returned rows are
#: different facts, and collapsing them is how a blank becomes an answer.
TOOL_STATUSES = ("ok", "error", "empty")


#: What the Agents SDK substitutes for a tool result when the tool RAISED.
#: ``default_tool_error_function`` swallows the exception and returns this
#: string as the tool's output, so by the time the orchestrator sees it there
#: is no flag left to read — the prefix IS the signal.
#:
#: ⚠ Prefixes, matched at the START, never anywhere in the body. A tool that
#: explains an upstream failure in prose ("the Buildium call returned an
#: error") is giving a real answer and must stay ``ok``; only the SDK's own
#: substitution begins with these words.
#:
#: ⚠ These are the SDK's strings, not ours, so they can drift on an upgrade
#: and drift SILENTLY — every error would quietly re-classify as ``ok``.
#: ``test_tool_result_status.py`` calls the live
#: ``agents.tool.default_tool_error_function`` and asserts what it produces
#: still matches, which is the only way to notice (gotcha 45).
_SDK_TOOL_ERROR_PREFIXES = (
    "An error occurred while running the tool.",
    "An error occurred while parsing tool arguments.",
)


def classify_tool_result(result: str) -> str:
    """Did this step come back with anything?

    ⚠ Conservative on purpose. It only says ``empty`` for shapes that are
    unambiguously nothing — an empty string, ``[]``, ``{}``, ``null``. Prose
    that happens to contain the word "none" is NOT empty, because a tool that
    explains why it found nothing is giving a real answer, and marking that
    ``empty`` would hide the explanation behind a dimmed row.

    ``error`` is only ever the SDK's own substituted failure text (above), so
    it is a fact rather than a reading of the prose.
    """
    stripped = (result or "").strip()
    if not stripped:
        return "empty"
    if stripped in ("[]", "{}", "null", "None", '""'):
        return "empty"
    if stripped.startswith(_SDK_TOOL_ERROR_PREFIXES):
        return "error"
    return "ok"


def handoff_event(agent: str) -> HelixisEvent:
    """The orchestrator routed to a specialist. ``agent`` is the display name
    ("Buildium Agent")."""
    return HelixisEvent(type=EventType.HANDOFF, data={"agent": agent})


def browser_start_event(task: str, url: str | None = None) -> HelixisEvent:
    return HelixisEvent(
        type=EventType.BROWSER_START,
        data={"task": task, "url": url},
    )


def browser_action_event(
    action: str, description: str, screenshot: str | None = None
) -> HelixisEvent:
    return HelixisEvent(
        type=EventType.BROWSER_ACTION,
        data={"action": action, "description": description, "screenshot": screenshot},
    )


def browser_done_event(summary: str) -> HelixisEvent:
    return HelixisEvent(type=EventType.BROWSER_DONE, data={"summary": summary})


def memory_recall_event(query: str, results_found: int) -> HelixisEvent:
    return HelixisEvent(
        type=EventType.MEMORY_RECALL,
        data={"query": query, "results_found": results_found},
    )


def todo_update_event(
    tasks: list[dict[str, str]],
) -> HelixisEvent:
    return HelixisEvent(type=EventType.TODO_UPDATE, data={"tasks": tasks})


def confirm_event(
    confirm_id: str,
    action: str,
    title: str,
    details: str,
    items: list[str] | None = None,
) -> HelixisEvent:
    return HelixisEvent(
        type=EventType.CONFIRM,
        data={
            "confirm_id": confirm_id,
            "action": action,
            "title": title,
            "details": details,
            "items": items or [],
        },
    )


def file_event(name: str, file_type: str, download_url: str) -> HelixisEvent:
    return HelixisEvent(
        type=EventType.FILE,
        data={"name": name, "file_type": file_type, "download_url": download_url},
    )


def done_event(summary: str, *, truncated: bool = False) -> HelixisEvent:
    """A finished turn. ``truncated=True`` marks a partial-answer SALVAGE —
    the run hit its deadline and this is what it had, not what it meant to say.

    The flag exists because a salvage is indistinguishable from a real answer
    on the wire otherwise. Battery v3's Q5 died on the wall clock with 190
    characters of mid-thought narration ("Now let me check open maintenance at
    each of these units…"), shipped it as ``done``, and every scorer downstream
    counted it as an answered question. A measurement that inflates itself is
    worse than a missing one.
    """
    data: dict[str, Any] = {"summary": summary}
    if truncated:
        data["truncated"] = True
    return HelixisEvent(type=EventType.DONE, data=data)


def error_event(message: str, recoverable: bool = False) -> HelixisEvent:
    return HelixisEvent(
        type=EventType.ERROR,
        data={"message": message, "recoverable": recoverable},
    )


def watcher_update_event(count: int) -> HelixisEvent:
    """Presence-lite (live-watch PR): how many teammates are watching this
    session, published by ``GET /agent/watch`` on join and leave. ``count``
    excludes the driver. Clients that predate this event ignore unknown
    types, so it's additive."""
    return HelixisEvent(type=EventType.WATCHER_UPDATE, data={"count": count})


# ── PR #70 — conversational engine helpers ──────────────────────


def narration_event(content: str) -> HelixisEvent:
    """A short italic status line. e.g. "Reading the lease record."""
    return HelixisEvent(type=EventType.NARRATION, data={"content": content})


def assistant_message_start_event(turn_id: str) -> HelixisEvent:
    """Open a streamed assistant turn. ``turn_id`` lets the frontend
    correlate subsequent token events with the same bubble."""
    return HelixisEvent(
        type=EventType.ASSISTANT_MESSAGE_START,
        data={"turn_id": turn_id},
    )


def assistant_message_token_event(turn_id: str, content: str) -> HelixisEvent:
    """A token inside an in-flight assistant turn."""
    return HelixisEvent(
        type=EventType.ASSISTANT_MESSAGE_TOKEN,
        data={"turn_id": turn_id, "content": content},
    )


def assistant_message_end_event(turn_id: str) -> HelixisEvent:
    """Close a streamed assistant turn."""
    return HelixisEvent(
        type=EventType.ASSISTANT_MESSAGE_END,
        data={"turn_id": turn_id},
    )


def suggestion_chip_event(
    action_index: int,
    label: str,
    parameter_patch: dict[str, Any] | None = None,
    is_destructive: bool = False,
) -> HelixisEvent:
    """A clickable pill in the thread. ``action_index`` ties back to
    the planner's ``ActionSurface.actions`` array so the executor can
    resolve which tool to run on click."""
    return HelixisEvent(
        type=EventType.SUGGESTION_CHIP,
        data={
            "action_index": action_index,
            "label": label,
            "parameter_patch": parameter_patch or {},
            "is_destructive": is_destructive,
        },
    )


def tabular_data_event(data: dict[str, Any]) -> HelixisEvent:
    """A structured table the agent chose to render (``data`` is the frontend
    ``TabularData`` shape: ``{kind, title?, columns, rows, footnote?}``). The
    frontend accepts the payload directly (no wrapper)."""
    return HelixisEvent(type=EventType.TABULAR_DATA, data=data)


def map_event(data: dict[str, Any]) -> HelixisEvent:
    """A static map a location tool chose to render (``data`` is the frontend
    ``MapData`` shape: ``{url, caption?, markers}`` — ``url`` is our key-free
    ``/api/v1/maps/static`` proxy path, never the raw Google URL). The frontend
    renders it inline via MapCard."""
    return HelixisEvent(type=EventType.MAP, data=data)


def email_draft_event(data: dict[str, Any]) -> HelixisEvent:
    """An email draft the agent composed, rendered as a card (``data`` is the
    frontend ``EmailDraftData`` shape: ``{to, cc, subject, body}``). Presentation
    only — nothing sends without the send tool's confirmation gate."""
    return HelixisEvent(type=EventType.EMAIL_DRAFT, data=data)


def artifacts_event(data: dict[str, Any]) -> HelixisEvent:
    """Downloadable files a code-execution run produced (``data`` is
    ``{artifacts: [{name, size, url}]}`` — ``url`` is a short-lived signed link).
    ``run_computation`` auto-queues this; the frontend renders download buttons
    via ArtifactCard, so the model never has to relay the URL."""
    return HelixisEvent(type=EventType.ARTIFACTS, data=data)


def disclosure_event(data: dict[str, Any]) -> HelixisEvent:
    """The reasoning loop's three-layer answer envelope (``data`` is the
    ``DisclosureEnvelope.to_payload()`` shape: ``{answer, reasoning, audit}``).
    Built by ``agents/disclosure.py`` from the finished run — the model never
    emits it. Loop mode only; published once, before ``done``."""
    return HelixisEvent(type=EventType.DISCLOSURE, data=data)


def confirm_gate_event(
    action_index: int,
    summary: str,
    payload_preview: dict[str, Any] | None = None,
    confirm_label: str = "Execute",
    cancel_label: str = "Cancel",
) -> HelixisEvent:
    """Pause the thread for explicit confirm before a write fires.
    ``payload_preview`` is the JSON the executor would post to Buildium
    — surfaced behind a "Show raw" toggle on the frontend."""
    return HelixisEvent(
        type=EventType.CONFIRM_GATE,
        data={
            "action_index": action_index,
            "summary": summary,
            "payload_preview": payload_preview or {},
            "confirm_label": confirm_label,
            "cancel_label": cancel_label,
        },
    )
