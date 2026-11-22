"""Property and unit handlers."""

from __future__ import annotations

import logging
from typing import Any

from helixis.buildium.errors import BuildiumApiError
from helixis.db.engine import get_pool
from helixis.db.repositories import ActivityEntry, write_activity
from helixis.db.repositories.entities import (
    delete_property,
    delete_unit,
    upsert_property,
    upsert_unit,
)
from helixis.webhooks.handlers._helpers import (
    envelope_account_id,
    envelope_int,
    open_api_for_company,
)

logger = logging.getLogger(__name__)


async def handle_rental_changed(company_id: str, envelope: dict[str, Any]) -> None:
    """Rental.Created / Updated / Deleted."""
    property_id = envelope_int(envelope, "PropertyId")
    if property_id is None:
        return
    account_id = envelope_account_id(envelope)
    event_name = str(envelope.get("EventName", ""))

    label: str | None = None
    if event_name.endswith(".Deleted"):
        # ⚠ The mirror must FORGET. Before this, a deleted property kept its
        # row forever and every rollup that reads helixis_properties went on
        # counting it — a confident false PRESENCE, which is worse than
        # gotcha 26's false absence: an absence makes the manager go and
        # look, a presence makes them act on a record that is not there.
        await delete_property(get_pool(), property_id, company_id=company_id)
    else:
        async with open_api_for_company(company_id) as api:
            if api is not None:
                try:
                    prop = await api.rentals.get_property(property_id)
                    label = prop.name
                    # Cockpit mirror (0038): the entity is already re-fetched
                    # for the activity label — mirror it while we hold it.
                    if account_id:
                        await upsert_property(
                            get_pool(),
                            prop,
                            account_id=account_id,
                            company_id=company_id,
                        )
                except BuildiumApiError as exc:
                    if exc.status != 404:
                        raise

    pool = get_pool()
    await write_activity(
        pool,
        ActivityEntry(
            account_id=account_id,
            company_id=company_id,
            source="webhook",
            source_label=f"Buildium · {event_name}",
            action_type=f"property_{event_name.split('.')[-1].lower()}",
            description=f"Buildium {event_name.split('.')[-1].lower()} property #{property_id}"
            + (f" — {label}" if label else ""),
            entity_type="property",
            entity_id=property_id,
            raw={"event_name": event_name, "property_id": property_id},
        ),
    )


async def handle_rental_unit_changed(company_id: str, envelope: dict[str, Any]) -> None:
    """RentalUnit.Created / Updated / Deleted."""
    unit_id = envelope_int(envelope, "UnitId")
    if unit_id is None:
        return
    account_id = envelope_account_id(envelope)
    event_name = str(envelope.get("EventName", ""))

    unit_label: str | None = None
    if event_name.endswith(".Deleted"):
        # Same reasoning as the property handler above — the mirror must
        # forget, or `helixis_units` keeps reporting a unit that is gone.
        await delete_unit(get_pool(), unit_id, company_id=company_id)
    else:
        async with open_api_for_company(company_id) as api:
            if api is not None:
                try:
                    unit = await api.rentals.get_unit(unit_id)
                    unit_label = unit.unit_number
                    # Cockpit mirror (0038) — same pattern as the property side.
                    if account_id:
                        await upsert_unit(
                            get_pool(),
                            unit,
                            account_id=account_id,
                            company_id=company_id,
                        )
                except BuildiumApiError as exc:
                    if exc.status != 404:
                        raise

    pool = get_pool()
    label_part = f" #{unit_label}" if unit_label else ""
    await write_activity(
        pool,
        ActivityEntry(
            account_id=account_id,
            company_id=company_id,
            source="webhook",
            source_label=f"Buildium · {event_name}",
            action_type=f"unit_{event_name.split('.')[-1].lower()}",
            description=f"Buildium {event_name.split('.')[-1].lower()} unit #{unit_id}{label_part}",
            entity_type="unit",
            entity_id=unit_id,
            raw={"event_name": event_name, "unit_id": unit_id},
        ),
    )


__all__ = [
    "handle_rental_changed",
    "handle_rental_unit_changed",
]
