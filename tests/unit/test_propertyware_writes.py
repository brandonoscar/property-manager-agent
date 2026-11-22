"""``propertyware/writes.py``: what the two work-order writes send, show and
check, and the write client that may send them. Pure, so each rule is pinned
here without a gate; ``test_propertyware_tools`` drives them through it."""

from __future__ import annotations

import re
from typing import Any

import pytest

from helixis.pms.http import operation_key
from helixis.propertyware import _paths, client, writes
from tests.unit._propertyware_fake import SYSTEM_ID, fixture_rows

WORK_ORDER: dict[str, Any] = {
    "id": 990700001,
    "number": 1001,
    "buildingID": 990100001,
    "unitID": 990200003,
    "unitIDs": [990200003],
    "status": "Open",
    "priority": "HIGH",
    "type": "Service Request",
    "category": "Plumbing",
    "description": "Synthetic leak under the kitchen sink",
    "startDate": "2026-10-01",
    "customFields": [{"fieldName": "A", "value": "1"}, {"fieldName": "B", "value": "2"}],
    "lastModifiedDateTime": "2026-09-30T14:02:11Z",
    "age": 7,
}


# ── the write client ─────────────────────────────────────────────────────


def test_the_write_client_holds_the_two_writes_and_their_reads_only() -> None:
    cfg = client.write_config(SYSTEM_ID)
    assert set(cfg.operations) == {
        "POST /workorders",
        "PATCH /workorders/{workOrderID}",
        "GET /workorders/{workOrderId}",
        "GET /buildings/{buildingID}",
        "GET /units/{unitID}",
    }
    assert cfg.write_methods == frozenset({"POST", "PATCH"})
    for key, params in cfg.operations.items():
        assert params == _paths.QUERY_PARAMS[key], key
        assert key not in _paths.BETA, key
        assert _paths.PERMISSIONS[key].startswith(("WORK ORDERS", "BUILDINGS", "UNITS")), key
    # The sync's, the probe's and Phase 8's client still cannot write.
    assert client.config(SYSTEM_ID).write_methods == frozenset()
    assert cfg.base_url == client.BASE_URL and cfg.account == SYSTEM_ID
    assert (cfg.requests_per_sec, cfg.concurrency) == (client.REQUESTS_PER_SEC, client.CONCURRENCY)


@pytest.mark.parametrize(
    ("added", "why"),
    [
        (("DELETE", "/workorders/{workOrderID}"), "a delete"),
        (("POST", "/workorders/bulk"), "a bulk endpoint"),
        (("PUT", "/workorders/closeworkorder/{workOrderID}"), "beta-marked"),
        (("POST", "/leases/payments"), "payment-type"),
        (("POST", "/WorkOrders"), "not in the spec (wrong case)"),
    ],
)
def test_an_unusable_write_stops_the_write_client_being_built(
    monkeypatch: pytest.MonkeyPatch, added: tuple[str, str], why: str
) -> None:
    monkeypatch.setattr(writes, "WRITES", (*writes.WRITES, added))
    with pytest.raises(client.UnusableWriteError):
        client.write_operations()


def test_a_write_the_spec_marks_beta_is_refused_whichever_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_paths, "BETA", _paths.BETA | {"POST /workorders"})
    with pytest.raises(client.UnusableWriteError, match="beta"):
        client.write_operations()


# ── the create ───────────────────────────────────────────────────────────


def test_the_create_body_sends_both_portal_flags_false_and_only_what_was_asked() -> None:
    body = writes.create_body(building_id=990100001, description="Fan rattles", priority="LOW")
    assert body == {
        "buildingID": 990100001,
        "description": "Fan rattles",
        "priority": "LOW",
        "publishToTenantPortal": False,
        "publishToOwnerPortal": False,
    }
    full = writes.create_body(
        building_id=990100001,
        description="Fan rattles",
        priority="LOW",
        unit_id=990200001,
        work_order_type="General",
        category="Electrical",
    )
    assert full["unitIDs"] == [990200001]
    assert (full["type"], full["category"]) == ("General", "Electrical")
    # The spec's create body has these, and nothing about a status or vendor.
    spec_fields = set(_spec_fields("SaveWorkOrderDTO"))
    assert set(full) <= spec_fields
    assert not {f for f in spec_fields if "status" in f.lower() or "vendor" in f.lower()}


def _spec_fields(model: str) -> list[str]:
    import json

    spec = json.loads((writes_spec_path()).read_text(encoding="utf-8"))
    return list(spec["components"]["schemas"][model]["properties"])


def writes_spec_path() -> Any:
    from pathlib import Path

    return (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "propertyware"
        / "vendor"
        / "app.propertyware.com"
        / "openapi.json"
    )


def _created(**over: Any) -> dict[str, Any]:
    record = {
        "id": 990700101,
        "buildingID": 990100001,
        "description": "Fan rattles\n",
        "priority": "low",
        "unitIDs": [990200001],
        "publishToTenantPortal": False,
        "publishToOwnerPortal": False,
    }
    return {**record, **over}


@pytest.mark.parametrize(
    ("over", "took"),
    [
        ({}, True),
        ({"description": "Fan  rattles"}, True),  # whitespace is not the text
        ({"buildingID": 990100002}, False),
        ({"description": "Something else"}, False),
        ({"priority": "HIGH"}, False),
        ({"unitIDs": []}, False),
        ({"unitIDs": "990200001"}, False),  # a shape that cannot say
        ({"publishToTenantPortal": True}, False),
        ({"publishToOwnerPortal": True}, False),
    ],
)
def test_a_create_reads_back_as_sent_or_it_did_not_take(over: dict[str, Any], took: bool) -> None:
    body = writes.create_body(
        building_id=990100001, description="Fan rattles", priority="LOW", unit_id=990200001
    )
    assert writes.create_took(body, _created(**over)) is took


def test_the_new_id_comes_from_the_201_body() -> None:
    assert writes.created_id({"id": 990700101}) == 990700101
    assert writes.created_id({"id": "990700101"}) == 990700101
    assert writes.created_id([{"id": 1}]) is None
    assert writes.created_id(None) is None


# ── the edit ─────────────────────────────────────────────────────────────


def test_a_patch_carries_only_what_changes() -> None:
    body = writes.patch_body(
        WORK_ORDER,
        {
            "description": "  Synthetic leak under the kitchen   sink ",  # same text
            "priority": "high",  # same priority
            "startDate": "2026-10-01",  # same day
            "unitIDs": [990200003],  # same units
            "category": "Electrical",  # changed
        },
    )
    assert body == {"category": "Electrical"}


def test_a_patch_never_carries_null_or_a_field_outside_the_six() -> None:
    with pytest.raises(ValueError, match="null"):
        writes.patch_body(WORK_ORDER, {"description": None})
    for field in ("status", "assignedVendors", "buildingID", "publishToTenantPortal"):
        with pytest.raises(ValueError, match="editable"):
            writes.patch_body(WORK_ORDER, {field: "x"})
    assert {f.api for f in writes.EDITABLE} <= set(_spec_fields("PatchWorkOrderDTO"))


def test_units_are_compared_as_a_set_and_sent_sorted() -> None:
    assert writes.patch_body(WORK_ORDER, {"unitIDs": [990200002, 990200001]}) == {
        "unitIDs": [990200001, 990200002]
    }
    assert writes.patch_body({**WORK_ORDER, "unitIDs": [2, 1]}, {"unitIDs": [1, 2]}) == {}
    # The list is the units; the single unitID is read only without it.
    assert writes.units_of({"unitIDs": [2], "unitID": 1}) == (2,)


@pytest.mark.parametrize(
    ("record", "units"),
    [
        ({"unitIDs": [1, 2], "unitID": 1}, (1, 2)),
        ({"unitIDs": [2, 2]}, (2,)),
        ({"unitID": 3}, (3,)),
        ({"unitIDs": [], "unitID": None}, ()),
        ({"unitIDs": {"items": [1]}}, None),  # a shape the spec leaves open
        ({"unitIDs": ["x"]}, None),
        ({}, None),  # says nothing: unknown, never "no units"
    ],
)
def test_a_work_orders_units_are_read_or_unknown(
    record: dict[str, Any], units: tuple[int, ...] | None
) -> None:
    assert writes.units_of(record) == units


def test_the_read_back_names_a_field_that_did_not_take() -> None:
    body = {"priority": "LOW"}
    after = {**WORK_ORDER, "priority": "LOW", "lastModifiedDateTime": "later", "age": 8}
    assert writes.patch_took(WORK_ORDER, body, after) == []
    assert writes.patch_took(WORK_ORDER, body, WORK_ORDER) == ["priority"]


def test_the_read_back_names_any_other_field_that_moved() -> None:
    """PW-18: a PATCH that acted as a full replace clears what it was not sent."""
    body = {"priority": "LOW"}
    replaced = {"id": WORK_ORDER["id"], "priority": "LOW"}
    wrong = writes.patch_took(WORK_ORDER, body, replaced)
    assert {"description", "category", "type", "unitIDs", "startDate", "buildingID"} <= set(wrong)
    # A list read back in another order has not moved.
    reordered = {**WORK_ORDER, "priority": "LOW", "customFields": WORK_ORDER["customFields"][::-1]}
    assert writes.patch_took(WORK_ORDER, body, reordered) == []


def test_a_units_change_may_move_the_units_own_echoes() -> None:
    body = {"unitIDs": [990200001]}
    after = {**WORK_ORDER, "unitIDs": [990200001], "unitID": 990200001, "location": "M-101"}
    assert writes.patch_took(WORK_ORDER, body, after) == []


def test_the_card_shows_each_changed_field_before_and_after() -> None:
    body = {"priority": "LOW", "unitIDs": [990200001], "scheduledEndDate": "2026-10-09"}
    items = writes.update_items(WORK_ORDER, body, {990200001: "M-101", 990200003: "M-103"})
    assert items[:3] == (
        "Priority: HIGH → LOW",
        "Scheduled end date: (none) → 2026-10-09",
        "Units: M-103 → M-101",
    )
    assert "Status and vendor: unchanged" in items[-1]
    long = writes.update_items({"description": "x" * 900}, {"description": "y"}, {})[0]
    assert len(long) < 400 and long.endswith("→ y")


def test_moved_on_card_reads_only_the_fields_the_card_showed() -> None:
    body = {"priority": "LOW"}
    assert writes.moved_on_card(WORK_ORDER, {**WORK_ORDER, "category": "Other"}, body) == []
    assert writes.moved_on_card(WORK_ORDER, {**WORK_ORDER, "priority": "MEDIUM"}, body) == [
        "priority"
    ]


# ── words ────────────────────────────────────────────────────────────────


def test_a_403_is_worded_by_the_operations_beta_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    plain = writes.forbidden_reply("POST", writes.CREATE_PATH)
    assert "lacks WORK ORDERS - Write" in plain and "Create a key in Propertyware" in plain
    assert "write beta" not in plain
    monkeypatch.setattr(_paths, "BETA", _paths.BETA | {operation_key("POST", writes.CREATE_PATH)})
    beta = writes.forbidden_reply("POST", writes.CREATE_PATH)
    assert "write beta" in beta and "ask Propertyware support" in beta
    assert "lacks" not in beta
    remembered = writes.forbidden_reply("PATCH", writes.UPDATE_PATH, remembered=True)
    assert remembered.startswith("Propertyware refused this change the last time")


def test_each_reply_names_an_action_and_only_propertyware() -> None:
    """Rule 16, and no other system's name."""
    for text in (
        writes.CANT_STATUS_OR_VENDOR,
        writes.STATUS_AND_VENDOR_LINE,
        writes.forbidden_reply("POST", writes.CREATE_PATH),
    ):
        assert "Propertyware" in text
        for other in ("Buildium", "Rentvine", "Rent Manager"):
            assert other not in text


@pytest.mark.parametrize(
    ("word", "value"),
    [("High", "HIGH"), (" medium ", "MEDIUM"), ("LOW", "LOW"), ("urgent", None), ("", None)],
)
def test_priority_words(word: str, value: str | None) -> None:
    assert writes.priority_value(word) == value


@pytest.mark.parametrize(
    ("text", "value"),
    [("2026-10-06", "2026-10-06"), ("2026-02-30", None), ("10/06/2026", None), (None, None)],
)
def test_dates(text: str | None, value: str | None) -> None:
    assert writes.date_value(text) == value


def test_the_fixtures_work_orders_are_readable_as_records() -> None:
    """Re-pointed (rule 19). It pinned a ``#`` in front of the display, which
    put Propertyware's number behind the ``#`` that means Occupella's id on the
    same run (eval 530). Now every ``#`` in it is the record's id, and
    Propertyware's own number leads, bare."""
    for row in fixture_rows("/workorders"):
        ident = writes.record_id(row)
        assert ident is not None
        shown = writes.display_number(row)
        assert re.findall(r"#(\d+)", shown) == [str(ident)], shown
        number = row.get("number")
        if number is not None:
            assert shown.startswith(f"{int(number)} ("), shown
