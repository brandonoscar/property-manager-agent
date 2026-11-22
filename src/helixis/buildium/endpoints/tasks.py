"""Tasks + task history.

Read helpers (``list``, ``get``, ``list_history``) were the original
Phase 0 surface. Phase A.2 adds three write helpers — ``update``,
``create``, ``add_note`` — invoked from
:mod:`helixis.agent.handlers` under explicit human confirmation.

The Buildium task hierarchy splits into four subtype endpoints
(``/tasks/contactrequests``, ``/tasks/rentalownerrequests``,
``/tasks/residentrequests``, ``/tasks/todorequests``). The
``/tasks/{id}`` shape Phase A.1 reads from is the generic super-set;
for writes we go through ``/tasks/todorequests`` because that's the
type Helixis creates. If we add resident-request workflows later
we'll fork the write helpers into per-subtype variants.

Write-probe 2026-07-05 (rounds 2+3) grounded ALL write routes against the
Buildium spec (LeadSimple SDK ``ToDoRequestsApi`` / ``TasksApi``) and the
live sandbox:

* The update route is ``PUT /tasks/todorequests/{id}`` — there is no
  PATCH (the old PATCH 404'd on a live sandbox call). Buildium's PUT is
  a FULL REPLACE: "any field not included will be set to empty/null",
  so ``update`` does read-merge-write via ``get_todo``.
* There is NO note endpoint anywhere in the tasks API (the old
  ``POST /tasks/{id}/notes`` 404'd — the route never existed). Buildium's
  actual note mechanism is the PUT body's ``Message`` field: it is "the
  description of the task UPDATE", which lands as a TASK HISTORY entry
  (``TaskHistoryMessage.Message``) — it does NOT modify the task's
  ``Description`` (round-3 live proof: PUT with Message → 2xx, Description
  unchanged, text absent from the todo payload; history is where it goes).
  So ``add_note`` sends the note as ``Message`` and read-back is via
  ``list_history``, and the merge mapping must NEVER carry Description →
  Message (that would spam a duplicate-description history entry on every
  status flip).
"""

from __future__ import annotations

# See applicants.py: the ``list`` method shadows the builtin in PEP 563
# annotation resolution, so list-returning annotations use ``builtins.list``.
import builtins
from typing import Any, cast

from helixis.buildium.client import BuildiumClient
from helixis.buildium.schemas import Task

# UpdateToDoTaskRequest.Message cap per the Buildium spec.
_MESSAGE_MAX_CHARS = 65_500


def _put_body_from_todo(todo: dict[str, Any]) -> dict[str, Any]:
    """Map a ``GET /tasks/todorequests/{id}`` response onto the PUT body.

    The GET (``ToDoTaskMessage``) and PUT (``UpdateToDoTaskRequest``) shapes
    are asymmetric — verified against the SDK spec 2026-07-05:

      GET Category.Id        → PUT CategoryId
      GET Category.SubCategory.Id → PUT SubCategoryId
      GET Property.Id        → PUT PropertyId
      Title / UnitId / AssignedToUserId / TaskStatus / Priority / DueDate
                             → same key

    Deliberately NOT mapped: ``Description`` → ``Message``. Message is the
    update COMMENT (it becomes a task-history entry), not the description —
    carrying the description there would write a duplicate-description
    history line on every update. The task's Description is not writable
    via this PUT and survives it untouched (live-verified, round 3).

    ``None`` leaves are omitted rather than sent as null.
    """
    body: dict[str, Any] = {
        "Title": todo.get("Title"),
        "AssignedToUserId": todo.get("AssignedToUserId"),
        "TaskStatus": todo.get("TaskStatus"),
        "Priority": todo.get("Priority"),
        "UnitId": todo.get("UnitId"),
        "DueDate": todo.get("DueDate"),
    }
    category = todo.get("Category")
    if isinstance(category, dict):
        body["CategoryId"] = category.get("Id")
        sub = category.get("SubCategory")
        if isinstance(sub, dict):
            body["SubCategoryId"] = sub.get("Id")
    prop = todo.get("Property")
    if isinstance(prop, dict):
        body["PropertyId"] = prop.get("Id")
    return {k: v for k, v in body.items() if v is not None}


class Tasks:
    def __init__(self, client: BuildiumClient) -> None:
        self._client = client

    async def list(self, **filters: Any) -> list[Task]:
        return await self._client.list_all("/tasks", filters or None, Task)

    async def get(self, task_id: int) -> Task:
        return cast(Task, await self._client.request("GET", f"/tasks/{task_id}", schema=Task))

    async def get_todo(self, task_id: int) -> dict[str, Any]:
        """GET the to-do-subtype view of a task — the read half of the
        read-merge-write PUT. 404s for non-to-do task subtypes."""
        return cast(
            dict[str, Any],
            await self._client.request("GET", f"/tasks/todorequests/{task_id}"),
        )

    async def download_history_file_url(
        self, task_id: int, history_id: int, file_id: int
    ) -> dict[str, Any]:
        """Request a presigned download URL for a file attached to a task-history
        entry (e.g. a tenant's photo on a work order).

        Buildium spec (LeadSimple ref): ``POST /v1/tasks/{taskId}/history/
        {taskHistoryId}/files/{fileId}/downloadrequest`` → ``FileDownloadMessage``
        (``{"DownloadUrl": "..."}``, a transient URL that expires after 5 min).
        Task attachments live under HISTORY entries, not the task itself — the
        entry's ``FileIds`` list gives the file ids to pass here.
        """
        return cast(
            dict[str, Any],
            await self._client.request(
                "POST",
                f"/tasks/{task_id}/history/{history_id}/files/{file_id}/downloadrequest",
            ),
        )

    async def list_history(self, task_id: int) -> builtins.list[dict[str, Any]]:
        """GET /v1/tasks/{taskId}/history — paginated.

        A churny task can accumulate more history entries than one Buildium
        page holds, so page through all of them rather than truncating.
        """
        return cast(
            builtins.list[dict[str, Any]],
            await self._client.list_all(f"/tasks/{task_id}/history", None),
        )

    async def update(self, task_id: int, *, fields: dict[str, Any]) -> dict[str, Any]:
        """Update a to-do task with new fields (partial semantics preserved).

        ``fields`` is the Buildium-shaped partial body, e.g.
        ``{"TaskStatus": "Completed"}``. Buildium's only update route is a
        full-replace ``PUT /tasks/todorequests/{id}`` (no PATCH exists), so
        this reads the current task, merges ``fields`` over it, and PUTs the
        complete body — omitted fields keep their current values instead of
        being nulled by the API.
        """
        todo = await self.get_todo(task_id)
        body = _put_body_from_todo(todo)
        body.update(fields)
        return cast(
            dict[str, Any],
            await self._client.request(
                "PUT",
                f"/tasks/todorequests/{task_id}",
                body=body,
            ),
        )

    async def create(self, *, fields: dict[str, Any]) -> dict[str, Any]:
        """POST a new to-do task. Returns the created task dict.

        Buildium requires Title, Priority, AssignedToUserId on creation
        and the caller must supply those — the planner's
        ``create_followup_task`` tool catalog already constrains the
        parameter set, so the handler doesn't re-validate here.
        """
        return cast(
            dict[str, Any],
            await self._client.request(
                "POST",
                "/tasks/todorequests",
                body=fields,
            ),
        )

    async def add_note(self, task_id: int, *, note: str) -> dict[str, Any]:
        """Add a staff note to a to-do task — as a task-history update.

        Buildium's note mechanism (probe rounds 2+3, 2026-07-05): the PUT
        body's ``Message`` is the description of THE UPDATE and lands as a
        task-history entry (``TaskHistoryMessage.Message``) attributed to the
        API user with its own timestamp — exactly what the Buildium UI shows
        as a note. It does NOT touch the task's Description. Read-back is via
        ``list_history``.
        """
        todo = await self.get_todo(task_id)
        body = _put_body_from_todo(todo)
        body["Message"] = note[:_MESSAGE_MAX_CHARS]
        return cast(
            dict[str, Any],
            await self._client.request(
                "PUT",
                f"/tasks/todorequests/{task_id}",
                body=body,
            ),
        )
