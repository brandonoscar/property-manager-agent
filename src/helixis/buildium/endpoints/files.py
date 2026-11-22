"""Files.

Upload contract (spec-grounded 2026-07-05 against the LeadSimple SDK
``FilesApi`` / ``CreateUploadFileRequestAsyncRequest`` /
``FileUploadTicketMessage``):

1. ``POST /files/uploadrequests`` with ``{EntityType, EntityId?, FileName,
   Title, Description?, CategoryId}`` → a ticket ``{BucketUrl, FormData,
   PhysicalFileName}`` (expires in 5 minutes).
2. Multipart POST to ``BucketUrl`` (AWS S3 presigned POST): the ``FormData``
   fields IN THE ORDER RECEIVED, then a ``file`` field with the binary
   content LAST.
3. Buildium ingests the S3 object ASYNCHRONOUSLY — the file record appears
   in ``GET /files`` (filter ``physicalfilenames``) seconds later, so the
   id resolution polls (same eventual-consistency class as task history).

Sharing: ``GET /files/{id}/sharing`` returns sections for every entity type
but only the file's own entity-type sections are POPULATED (the rest null);
``PUT`` takes the same per-section shapes (e.g. Lease: {Tenants,
RentalOwners} booleans).
"""

from __future__ import annotations

import asyncio

# See applicants.py: the ``list`` method shadows the builtin in PEP 563
# annotation resolution, so list-returning annotations use ``builtins.list``.
import builtins
from typing import Any, cast

import httpx

from helixis.buildium.client import BuildiumClient
from helixis.buildium.errors import BuildiumApiError
from helixis.buildium.schemas import File

# CreateUploadFileRequestAsyncRequest.EntityType enum, verbatim from the SDK.
UPLOAD_ENTITY_TYPES: tuple[str, ...] = (
    "Account",
    "Association",
    "AssociationOwner",
    "AssociationUnit",
    "Lease",
    "OwnershipAccount",
    "PublicAsset",
    "Rental",
    "RentalOwner",
    "RentalUnit",
    "Tenant",
    "Vendor",
)

# How long to poll GET /files for the ingested record (attempts x delay).
_RESOLVE_ATTEMPTS = 8
_RESOLVE_DELAY_SECONDS = 1.5


class Files:
    def __init__(self, client: BuildiumClient) -> None:
        self._client = client

    async def list(self, **filters: Any) -> list[File]:
        return await self._client.list_all("/files", filters or None, File)

    async def get(self, file_id: int) -> File:
        return cast(File, await self._client.request("GET", f"/files/{file_id}", schema=File))

    async def download_url(self, file_id: int) -> dict[str, Any]:
        """Request a presigned download URL for a Buildium file.

        Buildium spec: ``POST /v1/files/{fileId}/downloadrequest``.
        The endpoint mints a short-lived URL — that's a server-side
        mutation, so POST is the only allowed method (GET 405s).
        """
        return cast(
            dict[str, Any],
            await self._client.request("POST", f"/files/{file_id}/downloadrequest"),
        )

    async def list_categories(self) -> builtins.list[dict[str, Any]]:
        """All file categories (paginated) — accounts have a handful, and the
        uploader only needs one to satisfy the required ``CategoryId``. Routed
        through ``list_all`` so no collection read bypasses pagination."""
        return cast(
            "builtins.list[dict[str, Any]]",
            await self._client.list_all("/files/categories", None),
        )

    async def create_upload_ticket(
        self,
        *,
        entity_type: str,
        file_name: str,
        title: str,
        category_id: int,
        entity_id: int | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """POST /files/uploadrequests — mint the presigned S3 ticket."""
        if entity_type not in UPLOAD_ENTITY_TYPES:
            raise BuildiumApiError(
                status=422,
                message=f"Unknown file EntityType {entity_type!r} — must be one of "
                f"{', '.join(UPLOAD_ENTITY_TYPES)}",
            )
        body: dict[str, Any] = {
            "EntityType": entity_type,
            "FileName": file_name,
            "Title": title,
            "CategoryId": category_id,
        }
        if entity_id is not None:
            body["EntityId"] = entity_id
        if description:
            body["Description"] = description
        return cast(
            dict[str, Any],
            await self._client.request("POST", "/files/uploadrequests", body=body),
        )

    async def upload(
        self,
        *,
        entity_type: str,
        file_name: str,
        title: str,
        category_id: int,
        content: bytes,
        entity_id: int | None = None,
        description: str | None = None,
    ) -> dict[str, Any] | None:
        """Full two-step upload; returns the ingested file record, or ``None``
        when the S3 POST succeeded but Buildium hadn't ingested the record
        within the resolution window (it usually appears moments later).
        """
        ticket = await self.create_upload_ticket(
            entity_type=entity_type,
            file_name=file_name,
            title=title,
            category_id=category_id,
            entity_id=entity_id,
            description=description,
        )
        bucket_url = ticket.get("BucketUrl")
        form_data = ticket.get("FormData") or {}
        physical_name = ticket.get("PhysicalFileName")
        if not bucket_url or not physical_name or not isinstance(form_data, dict):
            raise BuildiumApiError(
                status=502,
                message="Buildium upload ticket was missing BucketUrl/FormData/"
                "PhysicalFileName — cannot complete the upload",
            )

        # Presigned S3 POST: NO Buildium auth headers, form fields in the
        # order received (JSON object order is preserved through parsing and
        # httpx emits `data=` fields in dict order), `file` field last.
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as http:
                resp = await http.post(
                    str(bucket_url),
                    data={str(k): str(v) for k, v in form_data.items()},
                    files={"file": (file_name, content)},
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise BuildiumApiError(
                status=502, message=f"S3 upload transport failure: {exc}"
            ) from exc
        if resp.status_code >= 300:
            raise BuildiumApiError(
                status=resp.status_code,
                message=f"S3 upload rejected: {resp.text[:200]}",
            )

        # Resolve the ingested record — eventual consistency, so poll.
        for attempt in range(_RESOLVE_ATTEMPTS):
            if attempt:
                await asyncio.sleep(_RESOLVE_DELAY_SECONDS)
            found = cast(
                list[dict[str, Any]],
                await self._client.request(
                    "GET", "/files", query={"physicalfilenames": physical_name}
                ),
            )
            if found:
                return found[0]
        return None

    async def get_sharing(self, file_id: int) -> dict[str, Any]:
        """GET /files/{fileId}/sharing — only the file's own entity-type
        sections are populated; the rest are null."""
        return cast(
            dict[str, Any],
            await self._client.request("GET", f"/files/{file_id}/sharing"),
        )

    async def put_sharing(self, file_id: int, body: dict[str, Any]) -> dict[str, Any]:
        """PUT /files/{fileId}/sharing with per-section share settings."""
        return cast(
            dict[str, Any],
            await self._client.request("PUT", f"/files/{file_id}/sharing", body=body),
        )
