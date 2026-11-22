"""Vendors + vendor categories (PR #77)."""

from __future__ import annotations

# See applicants.py: the ``list`` method shadows the builtin in PEP 563
# annotation resolution, so list-returning annotations use ``builtins.list``.
import builtins
from typing import Any, cast

from helixis.buildium.client import BuildiumClient
from helixis.buildium.schemas import Vendor, VendorCategory


class Vendors:
    def __init__(self, client: BuildiumClient) -> None:
        self._client = client

    async def list(self, **filters: Any) -> list[Vendor]:
        return await self._client.list_all("/vendors", filters or None, Vendor)

    async def get(self, vendor_id: int) -> Vendor:
        return cast(
            Vendor,
            await self._client.request("GET", f"/vendors/{vendor_id}", schema=Vendor),
        )

    async def list_categories(self, **filters: Any) -> builtins.list[VendorCategory]:
        """List vendor categories (e.g. ``Plumbing``, ``HVAC``, ``Electrical``).

        PR #77 — exposes ``GET /v1/vendors/categories`` so the agent
        can resolve a free-text category like "plumbers" into the
        exact Buildium ``Category.Name`` for this account before
        filtering vendors.
        """
        return await self._client.list_all(
            "/vendors/categories",
            filters or None,
            VendorCategory,
        )

    async def add_note(self, vendor_id: int, *, note: str) -> dict[str, Any]:
        """POST /v1/vendors/{vendorId}/notes — a REAL note-create endpoint
        (unlike tasks/work orders; spec-grounded 2026-07-05 against the
        LeadSimple SDK VendorsApi). Body reuses the shared note shape:
        ``{"Note": <text>}``, 65,535-char cap.
        """
        return cast(
            dict[str, Any],
            await self._client.request(
                "POST",
                f"/vendors/{vendor_id}/notes",
                body={"Note": note[:65_535]},
            ),
        )

    async def list_notes(self, vendor_id: int) -> builtins.list[dict[str, Any]]:
        """GET /v1/vendors/{vendorId}/notes — the note read-back (paginated).

        Notes accumulate over a vendor's lifetime, so this pages through all
        of them rather than truncating at Buildium's first page.
        """
        return cast(
            "builtins.list[dict[str, Any]]",
            await self._client.list_all(f"/vendors/{vendor_id}/notes", None),
        )
