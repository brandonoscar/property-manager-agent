"""Sandbox payload-shape probe for the cockpit entity mirror (migration 0038).

Run this LOCALLY (where the Buildium sandbox creds live — remote Claude
containers can't reach apisandbox.buildium.com):

    HELIXIS_BUILDIUM__CLIENT_ID=... HELIXIS_BUILDIUM__CLIENT_SECRET=... \
        python scripts/buildium_sandbox_probe.py

Read-only. For each entity the mirror models (properties, units, tenants,
leases, outstanding balances, lease transactions, work orders) it fetches a
small sample and prints a SANITIZED shape summary — field names + inferred
types + which of our typed pydantic fields parsed non-null. NO field VALUES
are ever printed (tenant payloads carry PII), only key names, types, and
null/parsed booleans, so the output is safe to paste back into a Claude
session to verify the 0038 schema against real payloads.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from helixis.buildium.api import BuildiumApi
from helixis.buildium.client import BuildiumClient
from helixis.settings import Settings

_SAMPLE = 3  # shape sample per entity; list_all paginates fully, so we
# fetch-then-slice — the sandbox dataset is small enough that this is cheap.


def _shape(value: Any, depth: int = 0) -> Any:
    """Types-only skeleton of a payload — never echoes leaf values."""
    if depth > 3:
        return "..."
    if isinstance(value, dict):
        return {k: _shape(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_shape(value[0], depth + 1)] if value else []
    if value is None:
        return "null"
    return type(value).__name__


def _report(name: str, models: list[Any], typed_fields: list[str]) -> None:
    print(f"\n=== {name} — {len(models)} sampled ===")
    if not models:
        print("  (no rows returned)")
        return
    first = models[0]
    print("  raw shape:", _shape(first.model_dump(mode="json", by_alias=True)))
    parsed = {f: all(getattr(m, f, None) is not None for m in models) for f in typed_fields}
    print("  typed fields parsed non-null on every sample:", parsed)


async def main() -> int:
    settings = Settings()
    if not settings.buildium.client_id or not settings.buildium.client_secret:
        print(
            "Set HELIXIS_BUILDIUM__CLIENT_ID / HELIXIS_BUILDIUM__CLIENT_SECRET "
            "(sandbox creds) and re-run."
        )
        return 1
    print(f"base_url: {settings.buildium.base_url}")

    async with BuildiumClient(settings.buildium) as client:
        api = BuildiumApi(client)

        props = (await api.rentals.list_properties())[:_SAMPLE]
        _report("properties", props, ["id", "name", "rental_sub_type", "address"])

        units = (await api.rentals.list_units())[:_SAMPLE]
        _report("units", units, ["id", "property_id", "unit_number", "market_rent"])

        tenants = (await api.rentals.list_tenants())[:_SAMPLE]
        _report("tenants", tenants, ["id", "first_name", "last_name", "email", "leases"])

        leases = (await api.leases.list())[:_SAMPLE]
        _report(
            "leases",
            leases,
            ["id", "property_id", "unit_id", "status", "lease_to_date", "account_details"],
        )

        balances = (await api.leases.list_outstanding_balances())[:_SAMPLE]
        _report("outstanding_balances", balances, ["lease_id", "property_id", "total_balance"])

        if leases:
            txns = (await api.leases.list_transactions(leases[0].id))[:_SAMPLE]
            _report(
                f"lease_transactions (lease {leases[0].id})",
                txns,
                ["id", "lease_id", "date", "transaction_type", "total_amount"],
            )

        wos = (await api.work_orders.list())[:_SAMPLE]
        _report("work_orders", wos, ["id", "property_id", "title", "status"])

    print("\nDone. Paste this output back to verify the 0038 mirror schema.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
