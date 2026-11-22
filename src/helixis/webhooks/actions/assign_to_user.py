"""``assign_to_user`` — v1's only action handler (PR #80).

Reads ``buildium_user_id`` off the assignment row's ``action_config``
and produces a routing decision that puts the event in the assignee's
Helixis Inbox. The Buildium user id is stored on the event for
future linkage (PR #81's ``buildium_staff_cache`` + a Buildium↔Helixis
user-mapping table); for v1, the assigned Helixis user id falls back
to the company admin so the event lands in *someone's* Inbox.
"""

from __future__ import annotations

import logging
from typing import Any

from helixis.db.repositories.property_assignments import resolve_admin_user_id
from helixis.webhooks.actions import RoutingDecision, register

logger = logging.getLogger(__name__)


@register("assign_to_user")
class AssignToUserHandler:
    """Route the event to a specific Buildium staff user.

    ``config`` shape (v1): ``{"buildium_user_id": <int>}``. Anything
    else falls through to the admin-fallback path so a half-configured
    row doesn't black-hole the event.
    """

    async def handle(
        self,
        event_name: str,
        envelope: dict[str, Any],
        config: dict[str, Any],
        company_id: str,
    ) -> RoutingDecision:
        buildium_user_id = config.get("buildium_user_id")
        if not isinstance(buildium_user_id, int):
            admin = await resolve_admin_user_id(company_id)
            return RoutingDecision(
                action_type="assign_to_user",
                assigned_buildium_user_id=None,
                assigned_helixis_user_id=admin,
                surface="inbox",
                reason="action_config missing buildium_user_id; fell back to admin",
            )

        # PR #80 v1: we don't yet have a Buildium↔Helixis user link
        # table — that ships in PR #81 alongside the
        # ``buildium_staff_cache``. Until then the Helixis Inbox owner
        # is the company admin; the Buildium user id is recorded on
        # the event so future linkage can backfill which manager
        # actually owns it.
        admin = await resolve_admin_user_id(company_id)
        return RoutingDecision(
            action_type="assign_to_user",
            assigned_buildium_user_id=buildium_user_id,
            assigned_helixis_user_id=admin,
            surface="inbox",
            reason=f"assigned to buildium_user_id={buildium_user_id} (admin Inbox for v1)",
        )


__all__ = ["AssignToUserHandler"]
