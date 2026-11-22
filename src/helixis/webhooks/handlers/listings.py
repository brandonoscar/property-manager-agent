"""Listing handlers — rental listing created / updated / deleted.

Previously ``Listing.*`` was in ``KNOWN_IGNORED`` (dropped). We now log each
change to the activity feed so a new advertised vacancy surfaces, mirroring
the finance / bank-transaction handlers. The Buildium listing payload is unit-
keyed; we record whichever id is present.
"""

from __future__ import annotations

import logging
from typing import Any

from helixis.db.engine import get_pool
from helixis.db.repositories import ActivityEntry, write_activity
from helixis.webhooks.handlers._helpers import envelope_account_id, envelope_int

logger = logging.getLogger(__name__)


async def handle_listing_changed(company_id: str, envelope: dict[str, Any]) -> None:
    """Listing.Created / Updated / Deleted."""
    listing_id = envelope_int(envelope, "ListingId")
    unit_id = envelope_int(envelope, "UnitId")
    entity_id = listing_id if listing_id is not None else unit_id
    if entity_id is None:
        return
    account_id = envelope_account_id(envelope)
    event_name = str(envelope.get("EventName", ""))
    op = event_name.split(".")[-1].lower()
    where = f" for unit #{unit_id}" if unit_id is not None else f" #{entity_id}"

    pool = get_pool()
    await write_activity(
        pool,
        ActivityEntry(
            account_id=account_id,
            company_id=company_id,
            source="webhook",
            source_label=f"Buildium · {event_name}",
            action_type=f"listing_{op}",
            description=f"Buildium {op} listing{where}",
            entity_type="listing",
            entity_id=entity_id,
            raw={
                "event_name": event_name,
                "listing_id": listing_id,
                "unit_id": unit_id,
            },
        ),
    )


__all__ = ["handle_listing_changed"]
