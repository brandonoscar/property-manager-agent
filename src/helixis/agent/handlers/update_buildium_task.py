"""Handler: ``update_buildium_task`` — modify the task the webhook fired on.

Tool catalog parameter shape (planner-side):
  priority?:           Low | Normal | High
  category?:           string (Buildium category name)
  assigned_to_user_id?: int
  due_date?:           YYYY-MM-DD
  status?:             New | InProgress | Completed | Deferred | Closed

At least one parameter must be set — the catalog description tells
the planner not to propose this action with an empty parameters
object, but we re-check at the handler boundary so a hand-rolled
``parameter_overrides`` payload that empties out can't fire an empty
PATCH against Buildium.
"""

from __future__ import annotations

import logging
from typing import Any

from helixis.agent.handlers.base import Handler, HandlerError, HandlerResult
from helixis.buildium.client import BuildiumClient
from helixis.buildium.errors import BuildiumApiError

logger = logging.getLogger(__name__)


_ALLOWED_PRIORITIES = {"Low", "Normal", "High"}
_ALLOWED_STATUSES = {"New", "InProgress", "Completed", "Deferred", "Closed"}


class UpdateBuildiumTaskHandler(Handler):
    tool_id = "update_buildium_task"

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
                # ⚠ Same sentence, same reason, same file pair — see the note
                # in `add_internal_note.py`. It was developer text that a
                # client-side discard kept off the screen; now that the client
                # shows what a route wrote, it has to be written for the person
                # reading it, and it has to say that retrying cannot help.
                "This card isn't linked to a Buildium task, so there's nothing "
                "to update. Make the change on the record in Buildium.",
                tool_id=self.tool_id,
            )

        fields: dict[str, Any] = {}

        priority = params.get("priority")
        if priority is not None:
            if priority not in _ALLOWED_PRIORITIES:
                raise HandlerError(
                    f"priority must be one of {sorted(_ALLOWED_PRIORITIES)}",
                    tool_id=self.tool_id,
                    status_code=400,
                )
            fields["Priority"] = priority

        category = params.get("category")
        if category is not None:
            # Buildium accepts either ``CategoryId: int`` or
            # ``Category: {Name: str}``; the planner produces names so
            # we send the named form. Buildium resolves it server-side.
            fields["Category"] = {"Name": str(category)}

        assigned = params.get("assigned_to_user_id")
        if assigned is not None:
            fields["AssignedToUserId"] = int(assigned)

        due_date = params.get("due_date")
        if due_date is not None:
            fields["DueDate"] = str(due_date)

        status = params.get("status")
        if status is not None:
            if status not in _ALLOWED_STATUSES:
                raise HandlerError(
                    f"status must be one of {sorted(_ALLOWED_STATUSES)}",
                    tool_id=self.tool_id,
                    status_code=400,
                )
            fields["TaskStatus"] = status

        if not fields:
            raise HandlerError(
                "update_buildium_task requires at least one parameter "
                "(priority, category, assigned_to_user_id, due_date, status)",
                tool_id=self.tool_id,
                status_code=400,
            )

        logger.info(
            "executing update_buildium_task",
            extra={
                "tool_id": self.tool_id,
                "task_id": task_id,
                "fields": sorted(fields.keys()),
            },
        )

        try:
            updated = await client.request(
                "PATCH",
                f"/tasks/todorequests/{task_id}",
                body=fields,
            )
        except BuildiumApiError as exc:
            raise HandlerError(
                f"Buildium rejected the task update: {exc}",
                tool_id=self.tool_id,
            ) from exc

        changes = ", ".join(f"{k}={v}" for k, v in sorted(fields.items()))
        return {
            "buildium_id": task_id,
            "summary": f"Updated task #{task_id} ({changes})",
            "updated_task": updated if isinstance(updated, dict) else None,
        }
