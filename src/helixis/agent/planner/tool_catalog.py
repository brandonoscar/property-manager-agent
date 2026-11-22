"""The set of human-confirmable actions the planner can propose.

These four tools cover the day-one Task.* surface. Each entry has a
description written **for the LLM** explaining when to propose it —
not human help text. The parameter schemas constrain what the LLM
fills in so the eventual Phase A.2 executor has a typed handle on
every field.

This is intentionally a flat module-level constant (not a registry
class) — Phase A.2 will add the executor side, but lookup will still
key off ``ToolSpec.id``. Adding a tool = adding an entry here + an
executor in Phase A.2.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from helixis.agent.planner.models import ToolParameterSchema
from helixis.pms import registry


class ToolSpec(BaseModel):
    id: str
    label: str
    """Default button label. The LLM may override per call via
    ``ProposedAction.label`` to inject context (e.g.
    'Raise priority to Medium' instead of 'Update task')."""

    description: str
    """Prompt-grade description: when SHOULD the LLM pick this tool?"""

    parameters: list[ToolParameterSchema]
    destructive: bool = False
    """Future: surfaces a stronger confirmation modal in the UI."""

    email_only: bool = False
    """Email-ingest tools (reply_to_email). Rendered ONLY into the email
    planner prompt — excluded from the Buildium prompt so Buildium event
    planning is byte-for-byte unchanged (no eval impact). Still in
    TOOL_IDS so the email path's output validates."""

    pms: str | None = "buildium"
    """The property-management system this tool WRITES to, or ``None`` when
    it writes to none (an email reply goes out through Gmail). A company is
    on exactly one PMS (ADR 0010 a1), so a tool is offered only to a company
    on its PMS (:func:`tool_catalog_as_prompt`), and an action planned for
    another one is dropped, marked or refused (:func:`unavailable_reason`)."""


def _p(
    name: str,
    type: str,
    *,
    required: bool = True,
    description: str | None = None,
    enum: list[str] | None = None,
    max_length: int | None = None,
) -> ToolParameterSchema:
    return ToolParameterSchema(
        name=name,
        type=type,
        required=required,
        description=description,
        enum=enum,
        max_length=max_length,
    )


TOOL_CATALOG: list[ToolSpec] = [
    ToolSpec(
        id="update_buildium_task",
        label="Update task",
        description=(
            "Modify the existing Buildium task that triggered this webhook. "
            "Pick this when the task itself needs a property change — "
            "raise priority, change category, assign to a vendor, push the "
            "due date out, or mark complete. At least ONE parameter must "
            "be filled in; do not propose this with an empty parameters "
            "object."
        ),
        parameters=[
            _p(
                "priority",
                "enum",
                required=False,
                description=(
                    "New priority. Only include when the LLM is recommending a priority change."
                ),
                enum=["Low", "Normal", "High"],
            ),
            _p(
                "category",
                "string",
                required=False,
                description="New task category name (e.g. 'Plumbing', 'HVAC').",
                max_length=80,
            ),
            _p(
                "assigned_to_user_id",
                "integer",
                required=False,
                description="Buildium user id to assign the task to.",
            ),
            _p(
                "due_date",
                "date",
                required=False,
                description="New due date in YYYY-MM-DD.",
            ),
            _p(
                "status",
                "enum",
                required=False,
                description="New task status.",
                enum=["New", "InProgress", "Completed", "Deferred", "Closed"],
            ),
        ],
        destructive=False,
    ),
    ToolSpec(
        id="add_internal_note",
        label="Add internal note",
        description=(
            "Append a Task Note in Buildium visible only to staff. Pick "
            "this when the planner has context worth surfacing on the "
            "task record — a pattern observed in the 90-day history, a "
            "tenant communication summary, or a heads-up for the next "
            "person to touch the task. Never use this to talk to the "
            "tenant — use send_tenant_message for that."
        ),
        parameters=[
            _p(
                "note_body",
                "string",
                required=True,
                description="The note text. Plain text, no markdown.",
                max_length=4000,
            ),
        ],
    ),
    ToolSpec(
        id="send_tenant_message",
        label="Message tenant",
        description=(
            "Send a Resident Center Communication to a tenant on the "
            "active lease for this task's unit. Pick this when the next "
            "right step is asking the tenant for more detail, "
            "confirming a service window, or letting them know a vendor "
            "is on the way. ``tenant_id`` MUST be one of the tenant ids "
            "from the enriched event's active_lease.tenants. Do not "
            "propose this tool when active_lease is null or the tenant "
            "list is empty."
        ),
        parameters=[
            _p(
                "tenant_id",
                "integer",
                required=True,
                description="Buildium tenant id — pick from enriched_event.active_lease.tenants.",
            ),
            _p(
                "subject",
                "string",
                required=True,
                description="Email-style subject line. Keep concise.",
                max_length=120,
            ),
            _p(
                "body",
                "string",
                required=True,
                description="Plain text body. Address the tenant by first name where appropriate.",
                max_length=4000,
            ),
        ],
        destructive=False,
    ),
    ToolSpec(
        id="create_followup_task",
        label="Create follow-up task",
        description=(
            "Create a NEW Buildium task — distinct from the one this "
            "webhook is about. Pick this when the planner spots a "
            "pattern in the 90-day history that warrants a scheduled "
            "follow-up (e.g. inspection in 30 days after a third "
            "plumbing complaint, or a vendor walkthrough after a "
            "recurring HVAC failure). Default ``unit_id`` to the same "
            "unit as the current task unless the pattern argues for a "
            "different unit on the same property."
        ),
        parameters=[
            _p(
                "title",
                "string",
                required=True,
                description="Short imperative title — e.g. 'Schedule plumbing inspection'.",
                max_length=120,
            ),
            _p(
                "description",
                "string",
                required=True,
                description="Why this follow-up exists. Reference the pattern from history.",
                max_length=4000,
            ),
            _p(
                "due_date",
                "date",
                required=True,
                description="Target completion date in YYYY-MM-DD.",
            ),
            _p(
                "category",
                "string",
                required=False,
                description="Task category name (Plumbing, HVAC, Inspection, etc.).",
                max_length=80,
            ),
            _p(
                "unit_id",
                "integer",
                required=False,
                description="Defaults to the current task's unit when omitted.",
            ),
            _p(
                "priority",
                "enum",
                required=False,
                description="Priority for the follow-up task.",
                enum=["Low", "Normal", "High"],
            ),
        ],
        destructive=False,
    ),
    ToolSpec(
        id="reply_to_email",
        label="Reply to email",
        description=(
            "Draft a reply to the inbound email this event is about, sent "
            "through the property manager's Gmail to the original sender on "
            "the same thread. This is the ONLY action to propose for an email "
            "event. Put a complete, ready-to-send reply in the action's "
            "primary_action.draft — address the sender by name, answer or "
            "acknowledge what they wrote, and propose concrete next steps. "
            "The reply is shown to the manager for review/edit and is NEVER "
            "sent without their explicit confirmation. Also fill "
            "parameters.body with the same draft text."
        ),
        parameters=[
            _p(
                "body",
                "string",
                required=True,
                description="The complete drafted reply, plain text. Same as primary_action.draft.",
                max_length=8000,
            ),
        ],
        destructive=False,
        email_only=True,
        pms=None,
    ),
]


def tool_catalog_as_prompt(*, email: bool = False, pms: str = "buildium") -> list[dict[str, Any]]:
    """Render the tool catalog as a JSON-serialisable list the LLM
    can read inside its system prompt. Excludes runtime-only flags
    like ``destructive`` from the public surface; those are for the
    eventual executor.

    ``email``: when True, render ONLY the email-ingest tools
    (``reply_to_email``); when False (default — the Buildium path),
    render only the Buildium tools, so Buildium prompts are unchanged
    by the email feature.

    ``pms``: the company's PMS. A tool that writes to another one is left
    out, so a Rentvine company's event prompt offers no Buildium write
    (each of which could only fail there). The default keeps the Buildium
    prompt byte-for-byte unchanged.
    """
    out: list[dict[str, Any]] = []
    for spec in TOOL_CATALOG:
        if spec.email_only != email or spec.pms not in (None, pms):
            continue
        out.append(
            {
                "id": spec.id,
                "label": spec.label,
                "description": spec.description,
                "parameters": [p.model_dump(exclude_none=True) for p in spec.parameters],
            }
        )
    return out


TOOL_IDS: frozenset[str] = frozenset(spec.id for spec in TOOL_CATALOG)
"""Whitelist for response validation — every ProposedAction.tool_id
must be in this set."""

_BY_ID: dict[str, ToolSpec] = {spec.id: spec for spec in TOOL_CATALOG}

#: The key :func:`mark_unavailable` sets on an action that cannot run here.
#: The Inbox (``frontend/src/components/InboxPage.tsx``) hides an action that
#: carries it, by this name.
UNAVAILABLE_KEY = "unavailable_reason"


def unavailable_reason(tool_id: str, pms: str) -> str | None:
    """Why ``tool_id`` cannot run for a company on ``pms``, or ``None`` when it
    can. The text names what the user can do instead (CLAUDE.md rule 16): a
    Rentvine workspace cannot add Buildium credentials, so "configure
    Buildium" would be advice nobody can take."""
    spec = _BY_ID.get(tool_id)
    if spec is None or spec.pms is None or spec.pms == pms:
        return None
    # Names from ``pms.registry`` ("Rent Manager", never "Rentmanager").
    writes_to = registry.name_of(spec.pms) or spec.pms.title()
    connected_to = registry.name_of(pms) or pms.title()
    return (
        f"This writes to {writes_to}, and this workspace is connected to "
        f"{connected_to}. Make this change in {connected_to}."
    )


def mark_unavailable(surface: Any, pms: str) -> Any:
    """``surface`` with every action that cannot run for ``pms`` marked with
    :data:`UNAVAILABLE_KEY`.

    ⚠ **MARKED, NEVER REMOVED.** The executor addresses an action by its
    POSITION in the stored list (``/actions/{i}/execute``), so dropping one
    would shift every action after it and a click would run a different
    action than the one on screen. Cards planned before the planner knew the
    company's PMS still carry Buildium writes, which is why this runs on read.
    """
    if not isinstance(surface, dict) or not isinstance(surface.get("actions"), list):
        return surface
    actions: list[Any] = []
    for action in surface["actions"]:
        tool_id = action.get("tool_id") if isinstance(action, dict) else None
        reason = unavailable_reason(tool_id, pms) if isinstance(tool_id, str) else None
        actions.append({**action, UNAVAILABLE_KEY: reason} if reason else action)
    return {**surface, "actions": actions}
