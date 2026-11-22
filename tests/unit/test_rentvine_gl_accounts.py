"""Rentvine chart of accounts -> ``helixis_gl_accounts``, and the ``isActive``
fix that came with it. Synthetic rows (ids 990 000 xxx)."""

from __future__ import annotations

import pytest

from helixis.rentvine import mapper, sync


def _account(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "accountID": "990000851",
        "accountTypeID": "9",
        "number": "6100",
        "name": "Synthetic Repairs",
        "isActive": "1",
        "parentAccountID": None,
    }
    row.update(over)
    return row


def test_an_expense_account_maps_to_buildiums_type_vocabulary():
    row = mapper.map_gl_account(_account())
    assert row["buildium_id"] == 990000851 and row["pms"] == "rentvine"
    assert row["type"] == "Expense" and row["sub_type"] == "Expense"
    assert row["account_number"] == "6100" and row["name"] == "Synthetic Repairs"
    assert row["is_active"] is True and row["is_bank_account"] is False


@pytest.mark.parametrize(
    ("type_id", "kind"),
    [(1, "Asset"), (2, "Liability"), (3, "Asset"), (6, "Equity"), (8, "Income"), (10, "Expense")],
)
def test_every_documented_type_has_a_buildium_type(type_id, kind):
    assert mapper.map_gl_account(_account(accountTypeID=type_id))["type"] == kind


def test_only_type_3_is_a_bank_account():
    bank = mapper.map_gl_account(_account(accountTypeID=3))
    assert bank["is_bank_account"] is True and bank["sub_type"] == "Bank"
    assert mapper.map_gl_account(_account(accountTypeID=4))["is_bank_account"] is False


def test_an_unknown_type_is_unknown_not_not_a_bank():
    row = mapper.map_gl_account(_account(accountTypeID=42))
    assert row["type"] is None and row["sub_type"] is None and row["is_bank_account"] is None


def test_the_parent_account_is_carried():
    assert mapper.map_gl_account(_account(parentAccountID="990000850"))["parent_gl_account_id"] == (
        990000850
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("0", False),
        (1, True),
        (0, False),
        (True, True),
        (False, False),
        ("true", True),
        ("false", False),
        (None, None),
        ("", None),
        ("yes", None),
    ],
)
def test_a_rentvine_flag_is_read_not_truthiness(value, expected):
    """Measured 2026-10-02: ``isActive`` arrives as the string "1". ``bool("0")``
    is True, which would have mirrored every inactive record as active."""
    assert mapper._flag(value) is expected


@pytest.mark.parametrize("fn", [mapper.map_property, mapper.map_vendor, mapper.map_gl_account])
def test_an_inactive_record_is_inactive(fn):
    assert fn({"isActive": "0"})["is_active"] is False


def test_the_chart_of_accounts_is_a_paged_source_stamped_as_reference_data():
    assert ("helixis_gl_accounts", "/accounting/accounts") in sync.SOURCES
    assert sync.SYNC_STATE_STAGE["helixis_gl_accounts"] == "gl_accounts"
    assert "gl_accounts" in sync.REFERENCE_STAGES
    assert "helixis_gl_accounts" in sync.PAGED_TABLES
