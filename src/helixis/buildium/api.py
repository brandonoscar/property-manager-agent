"""Top-level facade — hands out endpoint wrappers from a single client."""

from __future__ import annotations

from typing import Any, cast

from helixis.buildium.client import BuildiumClient
from helixis.buildium.endpoints import (
    Administration,
    Applicants,
    BankAccounts,
    Bills,
    Communications,
    Files,
    GeneralLedger,
    Leases,
    Listings,
    MeterReadings,
    Rentals,
    Requests,
    Tasks,
    Vendors,
    WorkOrders,
)


class BuildiumApi:
    """Resource-grouped access to the Buildium API."""

    def __init__(self, client: BuildiumClient) -> None:
        self.client = client
        self.rentals = Rentals(client)
        self.leases = Leases(client)
        self.work_orders = WorkOrders(client)
        self.tasks = Tasks(client)
        self.vendors = Vendors(client)
        self.bills = Bills(client)
        self.bank_accounts = BankAccounts(client)
        self.files = Files(client)
        self.applicants = Applicants(client)
        # Request task subtypes (read-only): resident / rental-owner /
        # contact / to-do requests. Verified paths from
        # developer.buildium.com + LeadSimple/buildium SDK on 2026-06-29.
        self.requests = Requests(client)
        # General Ledger group (read-only): chart of accounts, balances,
        # transactions. Verified paths from developer.buildium.com +
        # LeadSimple/buildium SDK on 2026-06-29.
        self.general_ledger = GeneralLedger(client)
        # Communications group (read-only): announcements, sent-email logs,
        # phone logs, templates. Verified paths from developer.buildium.com +
        # LeadSimple/buildium SDK on 2026-06-29.
        self.communications = Communications(client)
        # Listings group (read-only): rental unit listings. Verified paths
        # from developer.buildium.com + LeadSimple/buildium SDK on 2026-06-29.
        self.listings = Listings(client)
        # Meter readings (read-only). Verified paths from
        # developer.buildium.com + LeadSimple/buildium SDK on 2026-06-30.
        # ⚠ THE APPLIANCE SURFACE WAS RETIRED HERE ON 2026-09-22, four weeks
        # ahead of Buildium's own 2026-10-19 cut-off, by founder decision.
        # `Appliances`, `RentalAppliance` and `buildium_list_appliances` are
        # gone — do NOT re-add them against the old `/rentals/appliances`
        # paths, which stop answering on that date. Re-introducing the
        # capability means grounding the REPLACEMENT routes on
        # developer.buildium.com first (gotcha 79) and registering a fresh
        # tool. Record: docs/compliance/buildium-api.md.
        self.meter_readings = MeterReadings(client)
        # PR #81 — Administration group (Users API). Verified spec from
        # LeadSimple/buildium SDK on 2026-05-17.
        self.administration = Administration(client)

    async def account_info(self) -> dict[str, Any]:
        """Lightweight health-check call used by /api/v1/health/ready.

        Buildium's spec calls this ``/administration/account`` — the
        ``accountinfo`` variant doesn't exist and 404s.
        """
        return cast(
            dict[str, Any],
            await self.client.request("GET", "/administration/account"),
        )
