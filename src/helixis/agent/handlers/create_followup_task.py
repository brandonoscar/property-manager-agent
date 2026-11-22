"""Handler: ``create_followup_task`` — create a new Buildium task.

Tool catalog parameter shape:
  title:        string (required, <=120 chars)
  description:  string (required, <=4000 chars)
  due_date:     YYYY-MM-DD (required)
  category?:    string
  unit_id?:     int — defaults to ``context["event_unit_id"]``
  priority?:    Low | Normal | High (default Normal)

Buildium requires an ``AssignedToUserId`` on task creation. The
handler reads ``context["assigned_to_user_id"]`` — the executor
populates it from the original task's ``AssignedToUserId`` so a
follow-up gets routed to the same person by default. The user can
override via ``parameter_overrides`` in the confirmation modal.
"""

from __future__ import annotations

import logging
from typing import Any

from helixis.agent.handlers.base import Handler, HandlerError, HandlerResult
from helixis.buildium.client import BuildiumClient
from helixis.buildium.errors import BuildiumApiError

logger = logging.getLogger(__name__)

_ALLOWED_PRIORITIES = {"Low", "Normal", "High"}


class CreateFollowupTaskHandler(Handler):
    tool_id = "create_followup_task"

    async def execute(
        self,
        client: BuildiumClient,
        params: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
    ) -> HandlerResult:
        ctx = context or {}

        title = params.get("title")
        description = params.get("description")
        due_date = params.get("due_date")
        category = params.get("category")
        priority = params.get("priority", "Normal")
        unit_id = params.get("unit_id", ctx.get("event_unit_id"))
        assigned_to = params.get("assigned_to_user_id", ctx.get("assigned_to_user_id"))

        if not isinstance(title, str) or not title.strip():
            raise HandlerError(
                "title is required and must be a non-empty string",
                tool_id=self.tool_id,
                status_code=400,
            )
        if not isinstance(description, str) or not description.strip():
            raise HandlerError(
                "description is required and must be a non-empty string",
                tool_id=self.tool_id,
                status_code=400,
            )
        if not isinstance(due_date, str) or not due_date.strip():
            raise HandlerError(
                "due_date is required (YYYY-MM-DD)",
                tool_id=self.tool_id,
                status_code=400,
            )
        if priority not in _ALLOWED_PRIORITIES:
            raise HandlerError(
                f"priority must be one of {sorted(_ALLOWED_PRIORITIES)}",
                tool_id=self.tool_id,
                status_code=400,
            )

        fields: dict[str, Any] = {
            "Title": title.strip(),
            "Description": description.strip(),
            "DueDate": due_date.strip(),
            "Priority": priority,
            # Buildium's create-task endpoint requires BOTH Priority AND
            # TaskStatus ("missing or invalid" — the API's own words; gotcha 20).
            # This legacy handler omitted TaskStatus, so every webhook-action
            # execute of this handler 422'd → surfaced as a 502. "New" matches
            # the live, probed value in agents/buildium_tools._create_to_do_task_impl.
            "TaskStatus": "New",
        }
        if category is not None:
            fields["Category"] = {"Name": str(category)}
        if isinstance(unit_id, int):
            fields["UnitId"] = unit_id
        if isinstance(assigned_to, int):
            fields["AssignedToUserId"] = assigned_to

        logger.info(
            "executing create_followup_task",
            extra={
                "tool_id": self.tool_id,
                "unit_id": unit_id,
                "due_date": due_date,
                "priority": priority,
            },
        )

        try:
            created = await client.request(
                "POST",
                "/tasks/todorequests",
                body=fields,
            )
        except BuildiumApiError as exc:
            raise HandlerError(
                f"Buildium rejected the follow-up task: {exc}",
                tool_id=self.tool_id,
            ) from exc

        new_id: Any = None
        if isinstance(created, dict):
            new_id = created.get("Id")

        return {
            "buildium_id": new_id,
            "summary": (
                f'Created follow-up task "{title.strip()}" '
                f"(due {due_date}, priority {priority})"
                + (f" on unit #{unit_id}" if isinstance(unit_id, int) else "")
            ),
        }
