"""Applicants + applications."""

from __future__ import annotations

# ``builtins`` is imported so list-returning annotations below can spell the
# builtin as ``builtins.list``: the ``list`` *method* on this class shadows the
# builtin name when mypy resolves PEP 563 annotation strings in class scope.
import builtins
from typing import Any, cast

from helixis.buildium.client import BuildiumClient
from helixis.buildium.schemas import Applicant


class Applicants:
    def __init__(self, client: BuildiumClient) -> None:
        self._client = client

    async def list(self, **filters: Any) -> list[Applicant]:
        return await self._client.list_all("/applicants", filters or None, Applicant)

    async def get(self, applicant_id: int) -> Applicant:
        return cast(
            Applicant,
            await self._client.request("GET", f"/applicants/{applicant_id}", schema=Applicant),
        )

    async def list_applications(self, applicant_id: int) -> builtins.list[dict[str, Any]]:
        """GET /v1/applicants/{applicantId}/applications — paginated."""
        return cast(
            builtins.list[dict[str, Any]],
            await self._client.list_all(f"/applicants/{applicant_id}/applications", None),
        )

    async def get_application(self, applicant_id: int, application_id: int) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await self._client.request(
                "GET", f"/applicants/{applicant_id}/applications/{application_id}"
            ),
        )

    # Settable application statuses. Spec-grounded 2026-07-06 against the
    # LeadSimple SDK ``update_application_request.rb``: the PUT body is
    # exactly {"ApplicationStatus": <enum>} and the enum is these five.
    # There is NO delete operation on applications in the API — the
    # product rule ("Helixis never deletes applications") is structural.
    APPLICATION_STATUSES = ("Undecided", "Approved", "Rejected", "Cancelled", "Deferred")

    async def update_application_status(
        self, applicant_id: int, application_id: int, status: str
    ) -> dict[str, Any]:
        """PUT /v1/applicants/{applicantId}/applications/{applicationId}.

        Buildium spec: ``UpdateApplicationRequest`` carries ONE required
        field, ``ApplicationStatus``.
        """
        if status not in self.APPLICATION_STATUSES:
            raise ValueError(
                f"Invalid application status {status!r}; "
                f"must be one of {', '.join(self.APPLICATION_STATUSES)}"
            )
        return cast(
            dict[str, Any],
            await self._client.request(
                "PUT",
                f"/applicants/{applicant_id}/applications/{application_id}",
                body={"ApplicationStatus": status},
            ),
        )
