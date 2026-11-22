"""Request tasks — resident / rental-owner / contact / to-do (read-only).

Buildium models "requests" as four task subtypes, each with its own
collection under ``/tasks``:

* ``/tasks/residentrequests``      — tenant-submitted requests
* ``/tasks/rentalownerrequests``   — owner-submitted requests
* ``/tasks/contactrequests``       — prospect / general contact requests
* ``/tasks/todorequests``          — internal to-dos

The generic ``/tasks/{id}`` read (see tasks.py) returns the super-set
shape for a single task, but it can't list a single subtype — that's what
these helpers add. Writes (POST/PUT) are intentionally omitted here; task
creation still goes through ``Tasks.create`` (``/tasks/todorequests``).

Paths verified against developer.buildium.com + the LeadSimple/buildium
SDK on 2026-06-29.
"""

from __future__ import annotations

# See applicants.py: ``list`` shadows the builtin under PEP 563, so
# list-returning annotations use ``builtins.list``.
import builtins
from typing import Any, cast

from helixis.buildium.client import BuildiumClient
from helixis.buildium.schemas import RequestTask


class Requests:
    def __init__(self, client: BuildiumClient) -> None:
        self._client = client

    # ── Resident requests ───────────────────────────────────────────
    async def list_resident(self, **filters: Any) -> builtins.list[RequestTask]:
        return await self._client.list_all("/tasks/residentrequests", filters or None, RequestTask)

    async def get_resident(self, task_id: int) -> RequestTask:
        return cast(
            RequestTask,
            await self._client.request(
                "GET", f"/tasks/residentrequests/{task_id}", schema=RequestTask
            ),
        )

    # ── Rental-owner requests ───────────────────────────────────────
    async def list_rental_owner(self, **filters: Any) -> builtins.list[RequestTask]:
        return await self._client.list_all(
            "/tasks/rentalownerrequests", filters or None, RequestTask
        )

    async def get_rental_owner(self, task_id: int) -> RequestTask:
        return cast(
            RequestTask,
            await self._client.request(
                "GET", f"/tasks/rentalownerrequests/{task_id}", schema=RequestTask
            ),
        )

    # ── Contact requests ────────────────────────────────────────────
    async def list_contact(self, **filters: Any) -> builtins.list[RequestTask]:
        return await self._client.list_all("/tasks/contactrequests", filters or None, RequestTask)

    async def get_contact(self, task_id: int) -> RequestTask:
        return cast(
            RequestTask,
            await self._client.request(
                "GET", f"/tasks/contactrequests/{task_id}", schema=RequestTask
            ),
        )

    # ── To-do requests ──────────────────────────────────────────────
    async def list_to_do(self, **filters: Any) -> builtins.list[RequestTask]:
        return await self._client.list_all("/tasks/todorequests", filters or None, RequestTask)

    async def get_to_do(self, task_id: int) -> RequestTask:
        return cast(
            RequestTask,
            await self._client.request("GET", f"/tasks/todorequests/{task_id}", schema=RequestTask),
        )
