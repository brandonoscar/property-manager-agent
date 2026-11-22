"""Rentals — properties, units, tenants, rental owners."""

from __future__ import annotations

from typing import Any, cast

from helixis.buildium.client import BuildiumClient
from helixis.buildium.schemas import Property, RentalOwner, Tenant, Unit


class Rentals:
    def __init__(self, client: BuildiumClient) -> None:
        self._client = client

    # Properties
    async def list_properties(self, **filters: Any) -> list[Property]:
        return await self._client.list_all("/rentals", filters or None, Property)

    async def get_property(self, property_id: int) -> Property:
        return cast(
            Property,
            await self._client.request("GET", f"/rentals/{property_id}", schema=Property),
        )

    # Units
    async def list_units(self, **filters: Any) -> list[Unit]:
        return await self._client.list_all("/rentals/units", filters or None, Unit)

    async def get_unit(self, unit_id: int) -> Unit:
        return cast(
            Unit,
            await self._client.request("GET", f"/rentals/units/{unit_id}", schema=Unit),
        )

    # Tenants
    async def list_tenants(self, **filters: Any) -> list[Tenant]:
        return await self._client.list_all("/leases/tenants", filters or None, Tenant)

    async def get_tenant(self, tenant_id: int) -> Tenant:
        return cast(
            Tenant,
            await self._client.request("GET", f"/leases/tenants/{tenant_id}", schema=Tenant),
        )

    # Rental owners
    async def list_owners(self, **filters: Any) -> list[RentalOwner]:
        return await self._client.list_all("/rentals/owners", filters or None, RentalOwner)

    async def get_owner(self, owner_id: int) -> RentalOwner:
        return cast(
            RentalOwner,
            await self._client.request("GET", f"/rentals/owners/{owner_id}", schema=RentalOwner),
        )
