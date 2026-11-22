"""Shared types and models used across the Helixis backend."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ToolCategory(StrEnum):
    BROWSER = "browser"
    API = "api"
    MEMORY = "memory"
    FILESYSTEM = "filesystem"


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class AgentRequest(BaseModel):
    """Incoming request to run the agent."""

    task: str
    company_id: str
    session_id: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    # PR #141 — entry-point tag used by orchestrator_runs telemetry.
    # Currently emitted as: 'chat' (POST /agent/run from the chat UI),
    # 'inbox_freeform' (POST /buildium/events/{id}/freeform), or
    # 'system' (cron / webhook-triggered runs). None defaults to
    # 'chat' at the orchestrator boundary.
    source: str | None = None


class ConfirmationRequest(BaseModel):
    """Agent pauses and asks user to approve an action."""

    confirm_id: str
    action: str
    title: str
    details: str
    items: list[str] = Field(default_factory=list)


class ConfirmationResponse(BaseModel):
    """User's response to a confirmation request.

    ``reason`` is optional free text captured on a decline (why the human
    said no) — recorded on the durable row for later proposal tuning; never
    required, never blocks the resolution.
    """

    confirm_id: str
    approved: bool
    reason: str | None = None
    # edit-before-approve: the user's edited values for fields the gate
    # declared editable ({field: new_value}). Honored only on an approval and
    # only for declared-editable fields — confirm_or_abort filters against the
    # gate's own allowlist, so an unexpected key here is simply discarded.
    edits: dict[str, str] | None = None


class TodoItem(BaseModel):
    """A step in the agent's task plan."""

    description: str
    status: TaskStatus = TaskStatus.PENDING
