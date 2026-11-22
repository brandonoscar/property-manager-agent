"""PR #124 — enricher rejects leases whose UnitId doesn't match the
queried unit.

Production bug we're pinning:

The Buildium sandbox returned the same active lease (id=24188) for
queries against 3 different units (30663 / 30687 / 30698) belonging to
3 different properties (33 Garden Row, 100 Main Ave, 160 East End Ave).
That surfaced as "Clifton Munoz" appearing on the HVAC ticket at 33
Garden Row even though Clifton lives at 100 Main Ave — the planner saw
``active_lease.tenants = [Clifton, Virginia]`` in every row's
enrichment and proposed "Send to Clifton" everywhere.

Either Buildium's ``unitids`` filter on ``GET /leases`` silently
ignores invalid combos, or the sandbox's only active lease is returned
as a fallback when the queried unit has none. Either way the fix is
the same: validate the returned lease's own ``UnitId`` matches what
we asked for, and treat mismatched responses as "no active lease".
The planner sees ``active_lease: None`` and stops naming tenants.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from helixis.agent.enrichment.buildium_task_enricher import (
    BuildiumTaskEnricher,
)
from helixis.agent.enrichment.models import EnrichedLease

_ENRICHER_LOGGER = "helixis.agent.enrichment.buildium_task_enricher"


def _api_with_lease_response(lease_body: list[dict[str, Any]] | None) -> Any:
    """Build a fake BuildiumApi whose ``.client.request`` returns
    ``lease_body`` for a GET /leases call.

    Returns the mock ``api`` object the enricher expects. Tests do
    NOT exercise the full enrich() path — they call
    ``_fetch_active_lease`` directly, which is the layer we're
    pinning.
    """
    api = AsyncMock()
    api.client = AsyncMock()
    api.client.request = AsyncMock(return_value=lease_body)
    return api


def _lease(
    *,
    lease_id: int,
    unit_id: int | None,
    tenants: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a Buildium-shaped lease body. ``unit_id=None`` produces a
    lease record missing the UnitId field entirely — the edge case
    the validator should also reject."""
    body: dict[str, Any] = {
        "Id": lease_id,
        "LeaseStatus": "Active",
        "LeaseFromDate": "2025-09-01T00:00:00",
        "LeaseToDate": "2026-08-31T00:00:00",
        "CurrentTenants": tenants
        or [
            {"Id": 70287, "FirstName": "Clifton", "LastName": "Munoz"},
            {"Id": 70307, "FirstName": "Virginia", "LastName": "Gonzalez"},
        ],
    }
    if unit_id is not None:
        body["UnitId"] = unit_id
    return body


class TestFetchActiveLeaseValidation:
    @pytest.mark.asyncio
    async def test_returns_lease_when_unit_id_matches(self) -> None:
        api = _api_with_lease_response([_lease(lease_id=24188, unit_id=30687)])
        enricher = BuildiumTaskEnricher()
        result = await enricher._fetch_active_lease(api, 30687)
        assert isinstance(result, EnrichedLease)
        assert result.id == 24188
        assert result.unit_id == 30687
        assert len(result.tenants) == 2
        assert result.tenants[0].first_name == "Clifton"

    @pytest.mark.asyncio
    async def test_returns_none_when_unit_id_mismatches(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The production bug repro: Buildium returns lease 24188
        (which belongs to unit 30687) when we query for unit 30663.
        The validator must drop it."""
        api = _api_with_lease_response([_lease(lease_id=24188, unit_id=30687)])
        enricher = BuildiumTaskEnricher()
        with caplog.at_level(logging.WARNING, logger=_ENRICHER_LOGGER):
            result = await enricher._fetch_active_lease(api, 30663)
        assert result is None
        # Pin the log signal so ops can find this in Render logs.
        assert "active-lease unit mismatch" in caplog.text
        assert "30663" in caplog.text  # queried unit
        assert "24188" in caplog.text  # returned lease id

    @pytest.mark.asyncio
    async def test_returns_none_when_lease_has_no_unit_id(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Edge case: lease body missing the UnitId field entirely.
        We don't know what unit it's for; safer to drop than to
        attribute the tenants to the queried unit."""
        api = _api_with_lease_response([_lease(lease_id=24188, unit_id=None)])
        enricher = BuildiumTaskEnricher()
        with caplog.at_level(logging.WARNING, logger=_ENRICHER_LOGGER):
            result = await enricher._fetch_active_lease(api, 30663)
        assert result is None
        assert "active-lease unit mismatch" in caplog.text

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_lease_list(self) -> None:
        """Pre-existing behavior — no leases returned → no active
        lease. Pin it stays intact after PR #124."""
        api = _api_with_lease_response([])
        enricher = BuildiumTaskEnricher()
        result = await enricher._fetch_active_lease(api, 30663)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_null_lease_response(self) -> None:
        api = _api_with_lease_response(None)
        enricher = BuildiumTaskEnricher()
        result = await enricher._fetch_active_lease(api, 30663)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_lease_body_is_not_a_dict(self) -> None:
        """Defensive — Buildium docs say lease records are objects,
        but if the response ever included a non-object the existing
        path returned None. Pin that behavior stays."""
        api = _api_with_lease_response(["not-a-dict-payload"])  # type: ignore[list-item]
        enricher = BuildiumTaskEnricher()
        result = await enricher._fetch_active_lease(api, 30663)
        assert result is None


class TestEnrichedLeaseModel:
    def test_unit_id_field_round_trips(self) -> None:
        """PR #124 added the unit_id field. Pin it round-trips
        through model_dump so the persisted enrichment carries the
        cross-check value for any downstream consumer (e.g. a future
        debugger query)."""
        lease = EnrichedLease(id=24188, unit_id=30687)
        dumped = lease.model_dump()
        assert dumped["id"] == 24188
        assert dumped["unit_id"] == 30687

    def test_unit_id_defaults_to_none_for_back_compat(self) -> None:
        """Existing serialized rows (before PR #124) won't have
        unit_id. The field must be optional so they deserialize."""
        lease = EnrichedLease(id=1)
        assert lease.unit_id is None
