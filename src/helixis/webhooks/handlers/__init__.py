"""Webhook event handlers.

Two entry points exist for historical reasons:

  * :func:`helixis.webhooks.dispatch.dispatch_event` — the modern
    surface used by the route. Receives ``(company_id, event_name,
    envelope_dict)``. Backed by the per-category handlers under this
    package (``leases_v2``, ``maintenance``, ``properties``,
    ``finance``, ``applicants``).

  * :func:`dispatch` (here) — the worker's entry point. The worker
    claims rows from ``buildium_events`` (``ClaimedEvent`` instances)
    and expects an old-style handler with the signature
    ``(ClaimedEvent, BuildiumApi)``. This module provides that shim by
    bridging the worker's call into ``dispatch_event``.

The worker will eventually be retired once we're confident the synchronous
dispatch path covers everything in production.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from helixis.buildium.api import BuildiumApi
from helixis.webhooks.queue import ClaimedEvent

logger = logging.getLogger(__name__)

Handler = Callable[[ClaimedEvent, BuildiumApi], Awaitable[None]]


async def _bridge(event: ClaimedEvent, _api: BuildiumApi) -> None:
    """Bridge the worker's old-shape handler signature into ``dispatch_event``.

    The worker's ``BuildiumApi`` argument is ignored — handlers under
    :mod:`helixis.webhooks.dispatch` open their own per-company client
    via :func:`helixis.webhooks.handlers._helpers.open_api_for_company`.

    Imported lazily to break the circular dependency between
    :mod:`helixis.webhooks.dispatch` (which loads handler modules from
    this package) and this ``__init__``.
    """
    from helixis.webhooks.dispatch import dispatch_event

    await dispatch_event(event.company_id, event.event_name, event.payload)


def dispatch(_event_name: str) -> Handler:
    """Return the handler for an event.

    All routing now happens inside :func:`dispatch_event`, so this
    always returns the bridge — the bridge looks up the handler by
    name and calls it.
    """
    return _bridge


__all__ = ["Handler", "dispatch"]
