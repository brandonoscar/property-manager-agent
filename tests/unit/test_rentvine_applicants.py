"""Rentvine rental applications -> ``helixis_applicants``. One row per
APPLICATION (Rentvine names its applicants in one string and gives no contact).
Synthetic rows (ids 990 000 xxx)."""

from __future__ import annotations

from typing import Any

import pytest

from helixis.rentvine import mapper, sync


def _app(primary: Any = "3", lease_id: Any = None, names: Any = "Ana Ruiz, Ben Ruiz") -> dict:
    return {
        "applicationID": "990000901",
        "unitID": "990000203",
        "applicants": names,
        "status": {"primaryApplicationStatusID": primary, "name": "Screening"},
        "property": {"propertyID": "990000101"},
        "lease": {"leaseID": lease_id},
    }


def test_the_first_named_applicant_fills_the_name_and_all_names_stay_in_raw():
    row = mapper.map_applicant(_app())
    assert row["buildium_id"] == 990000901 and row["pms"] == "rentvine"
    assert (row["first_name"], row["last_name"]) == ("Ana", "Ruiz")
    assert row["raw"]["applicants"] == "Ana Ruiz, Ben Ruiz"
    assert row["property_id"] == 990000101 and row["unit_id"] == 990000203


def test_no_contact_is_invented():
    row = mapper.map_applicant(_app())
    assert row["email"] is None and row["phone"] is None and row["tenant_id"] is None


@pytest.mark.parametrize(
    ("primary", "status"),
    [
        ("1", "Draft"),
        ("2", "New"),
        ("3", "Undecided"),
        ("4", "Undecided"),
        ("5", "Deferred"),
        ("6", "Approved"),
        ("7", "Rejected"),
        ("8", "Cancelled"),
        ("99", None),
        (None, None),
    ],
)
def test_the_primary_status_maps_to_buildiums_vocabulary(primary, status):
    assert mapper.map_applicant(_app(primary=primary))["status"] == status


def test_an_application_with_a_lease_is_added_to_lease():
    assert mapper.map_applicant(_app(primary="6", lease_id="990000601"))["status"] == "AddedToLease"


def test_missing_siblings_cost_the_field_not_the_row():
    row = mapper.map_applicant({"applicationID": 990000902, "applicants": None})
    assert row["buildium_id"] == 990000902
    assert row["status"] is None and row["property_id"] is None and row["first_name"] is None


def test_a_single_word_name_has_no_last_name():
    row = mapper.map_applicant(_app(names="Prince"))
    assert (row["first_name"], row["last_name"]) == ("Prince", None)


def test_applications_are_a_paged_reference_stage():
    assert sync.SYNC_STATE_STAGE["helixis_applicants"] == "applicants"
    assert "applicants" in sync.REFERENCE_STAGES
    assert "helixis_applicants" in sync.PAGED_TABLES
