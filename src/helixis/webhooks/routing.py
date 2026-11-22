"""Routing pipeline that maps a Buildium event to a ``RoutingDecision`` (PR #80).

Pulls together the four pieces of PR #80's foundation:

1. ``categorize`` — event name → one of the five Brandon-designed
   categories.
2. ``extract_property_id`` — envelope → Buildium property id or
   ``None`` (no property tie).
3. ``load_property_assignment`` — Postgres lookup for the cell in
   the property × roles grid.
4. ``HANDLERS[action_type]`` — dispatch to the action handler the
   property manager configured for the cell.

The router wires this in via :func:`route_event`; the agent and the
tests both also call it directly to introspect routing without
running a real webhook.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from helixis.db.repositories.property_assignments import (
    PropertyAssignment,
    load_property_assignment,
    resolve_admin_user_id,
)
from helixis.webhooks.actions import HANDLERS, RoutingDecision
from helixis.webhooks.categorization import categorize
from helixis.webhooks.property_resolver import extract_property_id

logger = logging.getLogger(__name__)


async def route_event(
    pool: asyncpg.Pool,
    *,
    event_name: str,
    envelope: dict[str, Any],
    company_id: str,
) -> RoutingDecision:
    """Resolve an event to a routing decision.

    Lookup chain (each step falls through to the next on miss):

    1. ``property_assignments`` row matching
       ``(company_id, property_id, category)``.
    2. ``property_assignments`` row matching
       ``(company_id, NULL, category)`` — the "(no property)" default.
    3. Admin user_id fallback — every event lands in *someone's*
       Inbox; the admin owns the company-wide default.

    Steps 1-2 are skipped when the table doesn't exist yet (migration
    0014 not applied) — the loader handles ``UndefinedTableError``
    and returns ``None``, so the router falls cleanly to the admin
    fallback during the migration window.
    """
    category = categorize(event_name)
    property_id = extract_property_id(event_name, envelope)

    assignment = await _load_with_fallback(
        pool,
        company_id=company_id,
        buildium_property_id=property_id,
        category=category,
    )

    if assignment is None:
        admin = await resolve_admin_user_id(company_id, pool=pool)
        return RoutingDecision(
            action_type="assign_to_user",
            assigned_buildium_user_id=None,
            assigned_helixis_user_id=admin,
            surface="inbox",
            reason=(
                f"no property_assignment for category={category} "
                f"property_id={property_id}; admin fallback"
            ),
        )

    handler = HANDLERS.get(assignment.action_type)
    if handler is None:
        # Forward-compat: the database may carry an action_type we
        # don't have a handler for yet (e.g. ``automate`` from a
        # future PR). Log + fall back to admin so the event still
        # lands.
        admin = await resolve_admin_user_id(company_id, pool=pool)
        logger.warning(
            "No handler for action_type=%s (assignment id=%s); admin fallback.",
            assignment.action_type,
            assignment.id,
        )
        return RoutingDecision(
            action_type="assign_to_user",
            assigned_buildium_user_id=None,
            assigned_helixis_user_id=admin,
            surface="inbox",
            reason=f"no handler for action_type={assignment.action_type}; admin fallback",
        )

    return await handler.handle(
        event_name=event_name,
        envelope=envelope,
        config=assignment.action_config,
        company_id=company_id,
    )


async def _load_with_fallback(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    buildium_property_id: int | None,
    category: str,
) -> PropertyAssignment | None:
    """Property-specific row first, then the company-wide default row.

    ``buildium_property_id=None`` skips step 1 — the event already
    has no property tie, so only the default-row lookup is relevant.
    """
    if buildium_property_id is not None:
        row = await load_property_assignment(
            pool,
            company_id=company_id,
            buildium_property_id=buildium_property_id,
            category=category,
        )
        if row is not None:
            return row

    return await load_property_assignment(
        pool,
        company_id=company_id,
        buildium_property_id=None,
        category=category,
    )


__all__ = ["route_event"]
