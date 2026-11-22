"""Rental meter readings (read-only).

Buildium API group "Rental Meter Readings" — property-scoped utility readings.
The payload shape is property-specific, so these return raw dicts rather than a
pinned schema. Paths verified against developer.buildium.com + the
LeadSimple/buildium SDK on 2026-06-30.
"""

from __future__ import annotations

import builtins
from typing import Any, cast

from helixis.buildium.client import BuildiumClient


class MeterReadings:
    def __init__(self, client: BuildiumClient) -> None:
        self._client = client

    async def list(self, property_id: int, **filters: Any) -> builtins.list[dict[str, Any]]:
        """``GET /v1/rentals/{propertyId}/meterreadings``."""
        return cast(
            builtins.list[dict[str, Any]],
            await self._client.request(
                "GET",
                f"/rentals/{property_id}/meterreadings",
                query=filters or None,
            ),
        )

    async def get_summary(self, property_id: int, **filters: Any) -> Any:
        """``GET /v1/rentals/{propertyId}/meterreadings/summary``."""
        return await self._client.request(
            "GET",
            f"/rentals/{property_id}/meterreadings/summary",
            query=filters or None,
        )
