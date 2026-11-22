"""Lease enrichment — the fix for cards with a title and nothing else.

``Lease.`` was in the Inbox allowlist from the start but never in the
enrichment registry, so 19 ``Lease.Created`` and 7 ``Lease.MoveOut.Created``
rows rendered the deterministic floor surface — zero enriched, live-counted
2026-08-21. These pin that a lease card now carries the facts a property
manager needs to act: who, where, when, and what's still owed.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.agent.enrichment.lease_enricher import LeaseEnricher

COMPANY_ID = "11111111-1111-1111-1111-111111111111"

_LEASE = {
    "Id": 99797,
    "PropertyId": 37948,
    "UnitId": 5501,
    "LeaseFromDate": "2026-01-01",
    "LeaseToDate": "2026-12-31",
    "LeaseStatus": "Active",
    "AccountDetails": {"Rent": 1450.0, "Balance": 2300.0},
    "Tenants": [{"FirstName": "Marcus", "LastName": "Whitfield"}],
    "MoveOutData": [{"TenantId": 7, "MoveOutDate": "2026-09-15"}],
}


def _api(lease=_LEASE, unit=None, prop=None, fail: str | None = None):
    api = MagicMock()

    async def _request(_method: str, path: str, **_kw):
        if fail and fail in path:
            raise RuntimeError("buildium 502")
        if path.startswith("/leases/"):
            return lease
        if path.startswith("/rentals/units/"):
            return unit if unit is not None else {"UnitNumber": "4B"}
        if path.startswith("/rentals/"):
            return prop if prop is not None else {"Name": "Garden Row"}
        return {}

    api.client.request = AsyncMock(side_effect=_request)

    @asynccontextmanager
    async def _factory(_company_id):
        yield api

    return _factory


@pytest.mark.asyncio
async def test_a_move_out_card_names_who_where_and_when() -> None:
    enriched = await LeaseEnricher(api_factory=_api()).enrich(
        {"EventName": "Lease.MoveOut.Created", "LeaseId": 99797}, COMPANY_ID
    )
    assert enriched.task.title is not None
    assert "Marcus Whitfield" in enriched.task.title
    assert "Garden Row" in enriched.task.title and "4B" in enriched.task.title
    assert "2026-09-15" in (enriched.task.description or "")


@pytest.mark.asyncio
async def test_a_move_out_surfaces_the_outstanding_balance() -> None:
    """The load-bearing fact: money owed by someone who has already gone is
    far harder to collect. A card that omits it isn't actionable."""
    enriched = await LeaseEnricher(api_factory=_api()).enrich(
        {"EventName": "Lease.MoveOut.Created", "LeaseId": 99797}, COMPANY_ID
    )
    assert "$2,300.00" in (enriched.task.description or "")


@pytest.mark.asyncio
async def test_a_new_lease_reads_as_a_new_lease() -> None:
    enriched = await LeaseEnricher(api_factory=_api()).enrich(
        {"EventName": "Lease.Created", "LeaseId": 99797}, COMPANY_ID
    )
    assert "New lease" in (enriched.task.title or "")
    assert "moving out" not in (enriched.task.description or "")


@pytest.mark.asyncio
async def test_a_missing_lease_id_is_an_error_not_a_crash() -> None:
    enriched = await LeaseEnricher(api_factory=_api()).enrich(
        {"EventName": "Lease.Created"}, COMPANY_ID
    )
    assert enriched.errors and enriched.errors[0].field == "lease"


@pytest.mark.asyncio
async def test_a_failed_unit_fetch_still_ships_the_card() -> None:
    """Enrichment is best-effort by contract: a side fetch failing must
    degrade the card, never delete it."""
    enriched = await LeaseEnricher(api_factory=_api(fail="/rentals/units/")).enrich(
        {"EventName": "Lease.MoveOut.Created", "LeaseId": 99797}, COMPANY_ID
    )
    assert "Marcus Whitfield" in (enriched.task.title or "")
    assert any(e.field == "unit" for e in enriched.errors)


def test_lease_events_are_registered_for_enrichment() -> None:
    """The enricher is inert unless the registry points at it — that gap is
    the entire bug this fixes."""
    from helixis.webhooks.handlers.enrichment_registry import ENRICHMENT_HANDLERS

    assert "Lease.Created" in ENRICHMENT_HANDLERS
    assert "Lease.MoveOut.Created" in ENRICHMENT_HANDLERS
