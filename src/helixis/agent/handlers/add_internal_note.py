"""Handler: ``add_internal_note`` — append a staff-only Task Note.

Tool catalog parameter shape:
  note_body: string (required, <=4000 chars)

Posts to Buildium's ``POST /tasks/{taskId}/notes`` endpoint with
``{"Note": note_body}``. Notes are visible only to the property
management staff — never to tenants. The planner is prompted to use
``send_tenant_message`` for tenant-facing communication so a mis-
proposed internal note is bounded to internal visibility.
"""

from __future__ import annotations

import logging
from typing import Any

from helixis.agent.handlers.base import Handler, HandlerError, HandlerResult
from helixis.buildium.client import BuildiumClient
from helixis.buildium.errors import BuildiumApiError

logger = logging.getLogger(__name__)

_MAX_NOTE_LENGTH = 4000


class AddInternalNoteHandler(Handler):
    tool_id = "add_internal_note"

    async def execute(
        self,
        client: BuildiumClient,
        params: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
    ) -> HandlerResult:
        task_id = (context or {}).get("event_task_id")
        if not isinstance(task_id, int):
            raise HandlerError(
                # ⚠ This sentence NOW REACHES A SCREEN. It read "Internal:
                # missing event_task_id in context" — developer text, prefixed
                # that way by an author who knew it was — and was safe only
                # because `apiFetch` discarded every object `detail` and showed
                # a status fallback instead. Fixing that discard makes every
                # handler's words visible, which is the point; it also means a
                # sentence written for a log is a defect rather than a
                # curiosity. The state is real and reachable (an email-derived
                # card has no Buildium task behind it), and it does not clear
                # on a retry — so the message says so. The diagnostic is not
                # lost: the execute route logs the tool id and event id beside
                # it (`handler %s failed for event %s`).
                "This card isn't linked to a Buildium task, so there's nothing "
                "to attach a note to. Add it on the record in Buildium.",
                tool_id=self.tool_id,
            )

        note_body = params.get("note_body")
        if not isinstance(note_body, str) or not note_body.strip():
            raise HandlerError(
                "note_body is required and must be a non-empty string",
                tool_id=self.tool_id,
                status_code=400,
            )
        if len(note_body) > _MAX_NOTE_LENGTH:
            raise HandlerError(
                f"note_body exceeds {_MAX_NOTE_LENGTH} characters",
                tool_id=self.tool_id,
                status_code=400,
            )

        logger.info(
            "executing add_internal_note",
            extra={
                "tool_id": self.tool_id,
                "task_id": task_id,
                "note_length": len(note_body),
            },
        )

        try:
            created = await client.request(
                "POST",
                f"/tasks/{task_id}/notes",
                body={"Note": note_body.strip()},
            )
        except BuildiumApiError as exc:
            raise HandlerError(
                f"Buildium rejected the note: {exc}",
                tool_id=self.tool_id,
            ) from exc

        note_id: Any = None
        if isinstance(created, dict):
            note_id = created.get("Id")

        preview = note_body.strip()
        if len(preview) > 80:
            preview = preview[:77] + "..."

        return {
            "buildium_id": task_id,
            "buildium_note_id": note_id,
            "summary": f'Added internal note to task #{task_id}: "{preview}"',
        }
