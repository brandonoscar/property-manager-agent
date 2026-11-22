"""Action handlers for property_assignments rows (PR #80).

The webhook router resolves a webhook event to a ``property_assignments``
row (or the company-wide default), reads the row's ``action_type``,
and dispatches to the handler registered for that type. The handler
returns a ``RoutingDecision`` the router records on the event row.

v1 ships ONE handler — ``assign_to_user``. The registry exists so
adding ``automate``, ``ignore``, ``escalate``, or ``route_to_team``
in a future PR is a new module + a ``@register("foo")`` decorator —
zero schema migrations, zero router engine changes.

A handler that doesn't exist for a row's ``action_type`` is NOT a
crash. The router logs a warning and falls back to admin so a
forward-leaning row in the database doesn't take down inbound
webhooks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class RoutingDecision:
    """Outcome of evaluating a ``property_assignments`` row against an event.

    ``surface`` controls which UI surfaces receive the event:
    ``inbox``, ``activity``, ``both``, ``none``. v1 only emits
    ``inbox`` / ``activity`` / ``both`` (no event is silently
    discarded), but ``none`` is part of the shape so a future
    ``ignore`` action_type can opt out.
    """

    action_type: str
    assigned_buildium_user_id: int | None
    assigned_helixis_user_id: str | None
    surface: str  # "inbox" | "activity" | "both" | "none"
    reason: str


class ActionHandler(Protocol):
    """Protocol every action handler implements.

    Stateless by convention — handlers are constructed once at import
    time and reused across events. Per-request state (db pool, http
    client) is captured via the function parameters, not instance
    attributes.
    """

    async def handle(
        self,
        event_name: str,
        envelope: dict[str, Any],
        config: dict[str, Any],
        company_id: str,
    ) -> RoutingDecision: ...


# Registry. Keys are ``action_type`` values; values are
# instantiated handler objects. ``register`` is the decorator
# every concrete handler module uses.
HANDLERS: dict[str, ActionHandler] = {}


def register(action_type: str):  # type: ignore[no-untyped-def]
    """Class decorator — instantiate the handler and register it.

    ``@register("assign_to_user")`` on a class makes that class's
    no-argument constructor the singleton handler for the
    ``assign_to_user`` action type.
    """

    def _wrap(cls: type[ActionHandler]) -> type[ActionHandler]:
        HANDLERS[action_type] = cls()  # type: ignore[call-arg]
        return cls

    return _wrap


def get_handler(action_type: str) -> ActionHandler | None:
    """Return the handler for ``action_type`` or ``None`` if none is
    registered. ``None`` is the signal the router uses to fall back
    to admin instead of crashing."""
    return HANDLERS.get(action_type)


# Side-effect import so ``HANDLERS`` is populated when any caller
# imports this module. Without this the first time the router
# resolves an action_type, the handler list is empty and EVERY
# event falls to the admin-fallback branch.
from helixis.webhooks.actions import assign_to_user  # noqa: E402, F401

__all__ = [
    "ActionHandler",
    "HANDLERS",
    "RoutingDecision",
    "get_handler",
    "register",
]
