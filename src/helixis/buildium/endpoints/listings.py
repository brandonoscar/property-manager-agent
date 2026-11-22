"""Listings — rental unit listings (read-only).

Buildium exposes listings under the rentals/units namespace, not a top-level
``/listings`` collection. Paths verified against developer.buildium.com +
the LeadSimple/buildium SDK on 2026-06-29:

* ``GET /v1/rentals/units/listings``        — all listings
* ``GET /v1/rentals/units/{unitId}/listing`` — the listing for one unit
"""

from __future__ import annotations

import builtins
from typing import Any, cast

from helixis.buildium.client import BuildiumClient
from helixis.buildium.schemas import Listing


class Listings:
    def __init__(self, client: BuildiumClient) -> None:
        self._client = client

    async def list(self, **filters: Any) -> builtins.list[Listing]:
        """List rental listings (``GET /v1/rentals/units/listings``)."""
        return await self._client.list_all("/rentals/units/listings", filters or None, Listing)

    async def get_for_unit(self, unit_id: int) -> Listing:
        """The listing for a single unit (``GET /v1/rentals/units/{unitId}/listing``)."""
        return cast(
            Listing,
            await self._client.request("GET", f"/rentals/units/{unit_id}/listing", schema=Listing),
        )
