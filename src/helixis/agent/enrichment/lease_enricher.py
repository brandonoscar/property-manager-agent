"""Enricher for ``Lease.*`` Buildium webhooks (new lease, move-out).

``Lease.`` has been in the Inbox allowlist since the filter was written,
but it was never in the enrichment registry and no enricher existed — so
every lease card rendered the deterministic floor surface and nothing
else. Live count when this was found (2026-08-21): 19 ``Lease.Created``
and 7 ``Lease.MoveOut.Created`` rows, **zero** with an enrichment. That
is the "the description value is not happening" report: a card with a
title, one flat sentence, and a Dismiss button, where the design calls
for a summary, "What Occupella noticed", and suggested actions.

Same shape as the applicant enricher: map the lease onto the
``EnrichedEvent`` task shape so the existing planner/summarizer produce
the real card. A lease genuinely is a unit-attached entity, so unlike an
applicant it can carry the property/unit and the resident names — which
is exactly what makes a move-out card actionable ("who is leaving, from
where, when, and is the deposit clock running").

Degrades the way every enricher must: any failed fetch becomes an
``EnrichmentError`` and the card still ships with whatever was gathered.
"""

from __future__ import annotations

import logging
from typing import Any

from helixis.agent.enrichment.base import EventEnricher
from helixis.agent.enrichment.buildium_task_enricher import (
    _open_api_for_company,
    _to_int,
)
from helixis.agent.enrichment.models import (
    EnrichedEvent,
    EnrichedTask,
    EnrichmentError,
)

logger = logging.getLogger(__name__)


def _resident_names(lease_raw: dict[str, Any]) -> list[str]:
    """Tenant display names on the lease, best-effort."""
    names: list[str] = []
    for tenant in lease_raw.get("Tenants") or []:
        if not isinstance(tenant, dict):
            continue
        full = f"{tenant.get('FirstName') or ''} {tenant.get('LastName') or ''}".strip()
        if full:
            names.append(full)
    return names


def _first_resident_name_parts(lease_raw: dict[str, Any]) -> tuple[str | None, str | None]:
    """``(FirstName, LastName)`` of the first named tenant on the lease.

    ⚠ This exists because the caller used to re-SPLIT the display string
    ``_resident_names`` had just JOINED — ``residents[0].split(" ")[0]`` — and
    threw the surname away, so ``requested_by_last_name`` was null on every
    lease event while the other two enrichers (applicant, buildium task) both
    fill it. The planner prompt tells the model that field is *"the SENDER's
    name"* and ``summarizer/quality`` reads it as a grounding token, so a
    renewal or move-out draft addressed the resident by first name only.

    That is gotcha 41's prescription applied literally: **when a value is
    being reconstructed by parsing prose, delete the parse and read the
    fields.** The structured pair was one dict away the whole time, and the
    split could never have recovered a two-word forename ("Mary Ann") or a
    two-word surname ("Van Dyke") that these fields carry correctly.
    """
    for tenant in lease_raw.get("Tenants") or []:
        if not isinstance(tenant, dict):
            continue
        first = str(tenant.get("FirstName") or "").strip() or None
        last = str(tenant.get("LastName") or "").strip() or None
        if first or last:
            return first, last
    return None, None


def _money(value: Any) -> str | None:
    return f"${float(value):,.2f}" if isinstance(value, (int, float)) else None


class LeaseEnricher(EventEnricher):
    """``Lease.Created`` / ``Lease.MoveOut.Created`` → task-shaped enrichment."""

    def __init__(self, *, api_factory: Any | None = None) -> None:
        self._api_factory = api_factory

    async def enrich(self, event: dict[str, Any], company_id: str) -> EnrichedEvent:
        event_name = str(event.get("EventName") or "Lease.Unknown")
        lease_id = _to_int(event.get("LeaseId"))
        if lease_id is None:
            return EnrichedEvent(
                event_name=event_name,
                company_id=company_id,
                task=EnrichedTask(id=0),
                errors=[EnrichmentError(field="lease", message="Webhook envelope had no LeaseId.")],
            )

        errors: list[EnrichmentError] = []
        lease_raw: dict[str, Any] | None = None
        unit_raw: dict[str, Any] | None = None
        property_raw: dict[str, Any] | None = None

        api_cm = (
            self._api_factory(company_id)
            if self._api_factory is not None
            else _open_api_for_company(company_id)
        )
        async with api_cm as api:
            if api is None:
                return EnrichedEvent(
                    event_name=event_name,
                    company_id=company_id,
                    task=EnrichedTask(id=lease_id),
                    errors=[
                        EnrichmentError(
                            field="credentials",
                            message=(
                                "No Buildium credentials configured for this "
                                "company — cannot enrich."
                            ),
                        )
                    ],
                )
            try:
                raw = await api.client.request("GET", f"/leases/{lease_id}")
                if isinstance(raw, dict):
                    lease_raw = raw
                else:
                    errors.append(
                        EnrichmentError(
                            field="lease",
                            message=(
                                f"GET /leases/{lease_id} returned non-dict {type(raw).__name__}"
                            ),
                        )
                    )
            except Exception as exc:  # noqa: BLE001 — degrade, never raise
                errors.append(
                    EnrichmentError(field="lease", message=f"GET /leases/{lease_id} failed: {exc}")
                )

            # Property/unit are what turn "a lease ended" into "Unit 4B at
            # Garden Row is coming empty" — worth one extra call each, and
            # each failure is an error line rather than a dead card.
            if lease_raw is not None:
                unit_id = _to_int(lease_raw.get("UnitId"))
                property_id = _to_int(lease_raw.get("PropertyId"))
                if unit_id is not None:
                    try:
                        got = await api.client.request("GET", f"/rentals/units/{unit_id}")
                        unit_raw = got if isinstance(got, dict) else None
                    except Exception as exc:  # noqa: BLE001 — enhancement only
                        errors.append(
                            EnrichmentError(field="unit", message=f"GET unit failed: {exc}")
                        )
                if property_id is not None:
                    try:
                        got = await api.client.request("GET", f"/rentals/{property_id}")
                        property_raw = got if isinstance(got, dict) else None
                    except Exception as exc:  # noqa: BLE001 — enhancement only
                        errors.append(
                            EnrichmentError(field="property", message=f"GET property failed: {exc}")
                        )

        if lease_raw is None:
            return EnrichedEvent(
                event_name=event_name,
                company_id=company_id,
                task=EnrichedTask(id=lease_id),
                errors=errors,
            )

        is_move_out = "MoveOut" in event_name
        residents = _resident_names(lease_raw)
        resident_first, resident_last = _first_resident_name_parts(lease_raw)
        who = ", ".join(residents) if residents else "the resident"
        where_bits = [
            str(property_raw.get("Name")) if property_raw and property_raw.get("Name") else "",
            f"Unit {unit_raw.get('UnitNumber')}" if unit_raw and unit_raw.get("UnitNumber") else "",
        ]
        where = " — ".join(b for b in where_bits if b) or "the unit"

        if is_move_out:
            title = f"Move-out filed — {who} at {where}"
        else:
            title = f"New lease signed — {who} at {where}"

        parts: list[str] = []
        if is_move_out:
            parts.append(f"{who} is moving out of {where}.")
            for tenant in lease_raw.get("MoveOutData") or []:
                if isinstance(tenant, dict) and tenant.get("MoveOutDate"):
                    parts.append(f"Move-out date on file: {tenant['MoveOutDate']}.")
                    break
        else:
            parts.append(f"A new lease has been created for {who} at {where}.")
        for label, key in (("Term", "LeaseFromDate"), ("through", "LeaseToDate")):
            if lease_raw.get(key):
                parts.append(f"{label} {lease_raw[key]}.")
        rent = _money(lease_raw.get("Rent") or (lease_raw.get("AccountDetails") or {}).get("Rent"))
        if rent:
            parts.append(f"Rent {rent}.")
        balance = lease_raw.get("AccountDetails", {}).get("Balance")
        if isinstance(balance, (int, float)) and balance:
            # The load-bearing fact on a move-out: money still owed once the
            # resident is gone is far harder to collect.
            parts.append(f"Outstanding balance {_money(balance)}.")

        return EnrichedEvent(
            event_name=event_name,
            company_id=company_id,
            task=EnrichedTask(
                id=lease_id,
                title=title,
                description=" ".join(parts),
                status=str(lease_raw.get("LeaseStatus") or "") or None,
                category="Lease",
                requested_by_first_name=resident_first,
                requested_by_last_name=resident_last,
                raw=lease_raw,
            ),
            errors=errors,
        )
