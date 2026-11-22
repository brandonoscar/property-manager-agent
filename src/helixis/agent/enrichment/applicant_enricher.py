"""Enricher for Applicant.* Buildium webhooks.

Maps an applicant onto the ``EnrichedEvent`` task shape (the same trick
the email ingest uses): applicant name → requested_by, a synthesized
title ("Rental application — Jane Doe"), the application statuses in the
description, PropertyId carried through. No unit/lease/history fan-out —
an applicant isn't attached to a unit yet — but the planner still gets
enough to write a real summary and suggest real actions instead of the
bare floor card.

Without this registration every Applicant.* row fell to the floor
surface ("Applicant Created — assigned to the admin", zero actions) —
part of the thin-webhook-cards report, 2026-07-07.
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


def _full_name(raw: dict[str, Any]) -> str:
    first = str(raw.get("FirstName") or "").strip()
    last = str(raw.get("LastName") or "").strip()
    return f"{first} {last}".strip()


class ApplicantEnricher(EventEnricher):
    """Applicant.Created / Applicant.Updated → task-shaped enrichment."""

    def __init__(self, *, api_factory: Any | None = None) -> None:
        self._api_factory = api_factory

    async def enrich(self, event: dict[str, Any], company_id: str) -> EnrichedEvent:
        event_name = str(event.get("EventName") or "Applicant.Unknown")
        applicant_id = _to_int(event.get("ApplicantId"))
        if applicant_id is None:
            return EnrichedEvent(
                event_name=event_name,
                company_id=company_id,
                task=EnrichedTask(id=0),
                errors=[
                    EnrichmentError(
                        field="applicant",
                        message="Webhook envelope had no ApplicantId.",
                    )
                ],
            )

        errors: list[EnrichmentError] = []
        applicant_raw: dict[str, Any] | None = None
        applications: list[dict[str, Any]] = []

        if self._api_factory is not None:
            api_cm = self._api_factory(company_id)
        else:
            api_cm = _open_api_for_company(company_id)
        async with api_cm as api:
            if api is None:
                return EnrichedEvent(
                    event_name=event_name,
                    company_id=company_id,
                    task=EnrichedTask(id=applicant_id),
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
                raw = await api.client.request("GET", f"/applicants/{applicant_id}")
                if isinstance(raw, dict):
                    applicant_raw = raw
                else:
                    errors.append(
                        EnrichmentError(
                            field="applicant",
                            message=(
                                f"GET /applicants/{applicant_id} returned "
                                f"non-dict {type(raw).__name__}"
                            ),
                        )
                    )
            except Exception as exc:  # noqa: BLE001 — degrade, never raise
                errors.append(
                    EnrichmentError(
                        field="applicant",
                        message=f"GET /applicants/{applicant_id} failed: {exc}",
                    )
                )
            if applicant_raw is not None:
                try:
                    apps = await api.client.request(
                        "GET", f"/applicants/{applicant_id}/applications"
                    )
                    if isinstance(apps, list):
                        applications = [a for a in apps if isinstance(a, dict)]
                except Exception as exc:  # noqa: BLE001 — enhancement only
                    errors.append(
                        EnrichmentError(
                            field="applications",
                            message=f"GET applications failed: {exc}",
                        )
                    )

        if applicant_raw is None:
            return EnrichedEvent(
                event_name=event_name,
                company_id=company_id,
                task=EnrichedTask(id=applicant_id),
                errors=errors,
            )

        name = _full_name(applicant_raw) or f"applicant #{applicant_id}"
        verb = "updated" if event_name.endswith("Updated") else "received"
        statuses = [
            str(a.get("ApplicationStatus")) for a in applications if a.get("ApplicationStatus")
        ]
        desc_parts = [f"Rental application {verb} for {name}."]
        email = applicant_raw.get("Email")
        if email:
            desc_parts.append(f"Email: {email}.")
        if statuses:
            desc_parts.append(f"Application status: {', '.join(statuses)}.")

        task = EnrichedTask(
            id=applicant_id,
            title=f"Rental application — {name}",
            description=" ".join(desc_parts),
            status=statuses[0] if statuses else "New",
            category="Applicant",
            requested_by_first_name=str(applicant_raw.get("FirstName") or "") or None,
            requested_by_last_name=str(applicant_raw.get("LastName") or "") or None,
            property_id=_to_int(applicant_raw.get("PropertyId")),
            raw={"Applicant": applicant_raw, "Applications": applications},
        )
        return EnrichedEvent(
            event_name=event_name,
            company_id=company_id,
            task=task,
            errors=errors,
        )


__all__ = ["ApplicantEnricher"]
