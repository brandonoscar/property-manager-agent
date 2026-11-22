"""Administration / Users endpoint (PR #81).

Buildium's ``/v1/users`` lives on the AdministrationApi group, not a
dedicated UsersApi. The brief verifies the spec from the LeadSimple
SDK on 2026-05-17 — every field, casing, and pagination default is
pinned in ``UserMessage`` + ``UserMessageUserRole`` schemas in
``helixis.buildium.schemas``.

Required Buildium permission: ``Administration > Users — View``. The
``_sanitize_buildium_error`` helper (PR #76) surfaces a clean
permission message on 403; the staff-sync code path catches that
explicitly so a missing permission doesn't break the whole sync run.
"""

from __future__ import annotations

from typing import Any, cast

from helixis.buildium.client import BuildiumClient
from helixis.buildium.schemas import User


class Administration:
    """``/v1/users`` and ``/v1/users/{userId}``.

    Named after Buildium's API surface (``AdministrationApi`` in the
    OpenAPI spec) so a future PR can add other admin endpoints —
    company info, audit logs, etc. — under the same module without
    renaming.
    """

    def __init__(self, client: BuildiumClient) -> None:
        self._client = client

    async def list_users(self, **filters: Any) -> list[User]:
        """List Buildium users.

        PR #81 callers typically pass ``usertypes=["Staff"]`` and
        ``status="Active"`` to filter the routing-grid dropdown
        source. ``list_all`` paginates transparently — the default
        50-row page would silently truncate accounts with more
        staff if we relied on it.
        """
        return await self._client.list_all("/users", filters or None, User)

    async def get_user(self, user_id: int) -> User:
        """Single-user lookup. Used when we have a Buildium user id
        but need the full display record."""
        return cast(User, await self._client.request("GET", f"/users/{user_id}", schema=User))
