"""Request-kind mapping — owner / contact requests into the Inbox.

Resident, rental-owner, and contact requests all arrive as the same
``Task.Created`` webhook and are read via the ``GET /tasks/{id}``
super-set. The only thing distinguishing them once enriched is
``RequestedByUserEntity.Type``. These tests pin that the enricher
captures the Type and that the pure label helper maps it (both offline,
no live Buildium).
"""

from __future__ import annotations

import pytest

from helixis.agent.enrichment.buildium_task_enricher import _task_from_raw
from helixis.agent.enrichment.models import request_kind_label


def _task_raw(requested_type: str | None) -> dict:
    entity: dict = {"Id": 5501, "FirstName": "Jane", "LastName": "Doe"}
    if requested_type is not None:
        entity["Type"] = requested_type
    return {"Id": 42, "Title": "A request", "RequestedByUserEntity": entity}


@pytest.mark.parametrize(
    ("raw_type", "expected"),
    [
        ("RentalOwner", "Owner request"),
        ("rentalowner", "Owner request"),  # case-insensitive
        ("Owner", "Owner request"),
        ("Contact", "Contact request"),
        ("Tenant", "Resident request"),
        ("Association", "Association request"),
    ],
)
def test_enricher_captures_type_and_label_maps(raw_type: str, expected: str) -> None:
    task = _task_from_raw(_task_raw(raw_type))
    assert task.requested_by_type == raw_type
    assert request_kind_label(task.requested_by_type) == expected


def test_absent_type_degrades_to_none() -> None:
    # Staff todorequests have no RequestedByUserEntity.Type — no wrong badge.
    task = _task_from_raw(_task_raw(None))
    assert task.requested_by_type is None
    assert request_kind_label(task.requested_by_type) is None


def test_unknown_type_is_unlabelled() -> None:
    assert request_kind_label("SomethingNew") is None
    assert request_kind_label("") is None
    assert request_kind_label(None) is None


def test_requester_name_still_captured_for_owner_request() -> None:
    # Owner requests must still surface the requester (the owner), not just
    # the kind — the card's Reporter cell reads these.
    task = _task_from_raw(_task_raw("RentalOwner"))
    assert task.requested_by_first_name == "Jane"
    assert task.requested_by_last_name == "Doe"
    assert task.requested_by_tenant_id == 5501
