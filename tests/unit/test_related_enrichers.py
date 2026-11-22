"""WorkOrder + Applicant enrichers — the thin-webhook-cards fix.

Pins: a WorkOrder.* event resolves its linked task and gets the full
task fan-out (unit / lease / history) plus the vendor work details; an
Applicant.* event maps onto the task shape with a real title and the
application statuses; missing ids / API failures degrade to an
enrichment with errors, never an exception; and the registry actually
serves handlers for all five newly-supported event names.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.agent.enrichment import ApplicantEnricher, WorkOrderEnricher
from helixis.webhooks.handlers.enrichment_registry import get_enrichment_handler

COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"

_TASK_RAW = {
    "Id": 21862,
    "Title": "AC not cooling — unit 4B",
    "Description": "Tenant reports warm air.",
    "TaskStatus": "New",
    "Priority": "High",
    "UnitId": None,
    "PropertyId": None,
}


def _fake_api(routes: dict[str, Any]) -> MagicMock:
    """A BuildiumApi stand-in whose client.request routes by path."""

    async def request(method: str, path: str, **_kw: Any) -> Any:
        if path in routes:
            value = routes[path]
            if isinstance(value, Exception):
                raise value
            return value
        return []

    api = MagicMock()
    api.client.request = AsyncMock(side_effect=request)
    return api


def _factory(routes: dict[str, Any]):
    @asynccontextmanager
    async def _open(_company_id: str):
        yield _fake_api(routes)

    return _open


# ── WorkOrderEnricher ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_work_order_resolves_linked_task_and_grafts_work_details() -> None:
    enricher = WorkOrderEnricher(
        api_factory=_factory(
            {
                "/workorders/501": {
                    "Id": 501,
                    "Task": {"Id": 21862},
                    "WorkDetails": "Replace condenser fan motor.",
                    "VendorId": 55,
                },
                "/tasks/21862": _TASK_RAW,
            }
        )
    )

    enriched = await enricher.enrich(
        {"EventName": "WorkOrder.Created", "WorkOrderId": 501}, COMPANY_ID
    )

    assert enriched.event_name == "WorkOrder.Created"
    assert enriched.task.id == 21862
    assert enriched.task.title == "AC not cooling — unit 4B"
    assert "Replace condenser fan motor." in (enriched.task.description or "")
    assert enriched.task.raw["WorkOrder"]["VendorId"] == 55
    assert enriched.errors == []


@pytest.mark.asyncio
async def test_work_order_missing_id_and_failed_fetch_degrade() -> None:
    enricher = WorkOrderEnricher(api_factory=_factory({}))
    no_id = await enricher.enrich({"EventName": "WorkOrder.Created"}, COMPANY_ID)
    assert no_id.task.id == 0
    assert any("WorkOrderId" in e.message for e in no_id.errors)

    failing = WorkOrderEnricher(api_factory=_factory({"/workorders/501": RuntimeError("boom")}))
    failed = await failing.enrich(
        {"EventName": "WorkOrder.Updated", "WorkOrderId": 501}, COMPANY_ID
    )
    assert failed.task.id == 0
    assert any(e.field == "work_order" for e in failed.errors)


# ── ApplicantEnricher ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_applicant_maps_onto_task_shape_with_statuses() -> None:
    enricher = ApplicantEnricher(
        api_factory=_factory(
            {
                "/applicants/9001": {
                    "Id": 9001,
                    "FirstName": "Jane",
                    "LastName": "Doe",
                    "Email": "jane@example.com",
                    "PropertyId": 77,
                },
                "/applicants/9001/applications": [
                    {"Id": 1, "ApplicationStatus": "Undecided"},
                ],
            }
        )
    )

    enriched = await enricher.enrich(
        {"EventName": "Applicant.Created", "ApplicantId": 9001}, COMPANY_ID
    )

    assert enriched.task.title == "Rental application — Jane Doe"
    assert enriched.task.requested_by_first_name == "Jane"
    assert enriched.task.property_id == 77
    assert enriched.task.status == "Undecided"
    assert "jane@example.com" in (enriched.task.description or "")
    assert enriched.errors == []


@pytest.mark.asyncio
async def test_applicant_failures_degrade_with_errors() -> None:
    enricher = ApplicantEnricher(api_factory=_factory({"/applicants/9001": RuntimeError("502")}))
    enriched = await enricher.enrich(
        {"EventName": "Applicant.Updated", "ApplicantId": 9001}, COMPANY_ID
    )
    assert enriched.task.id == 9001
    assert any(e.field == "applicant" for e in enriched.errors)


# ── registry coverage ────────────────────────────────────────────────


def test_registry_serves_the_newly_supported_events() -> None:
    for name in (
        "Task.Created",
        "Task.Updated",
        "WorkOrder.Created",
        "WorkOrder.Updated",
        "Applicant.Created",
        "Applicant.Updated",
    ):
        assert get_enrichment_handler(name) is not None, name
    # Deliberately unregistered — history sub-events would duplicate the
    # Task.Updated card.
    assert get_enrichment_handler("Task.History.Created") is None
