"""Round-trip tests for the expanded Buildium pydantic schemas.

The agent's "Buildium expert" tools depend on a richer set of fields
than we previously typed (e.g. ``CurrentTenants`` on a lease object).
Lock those in here so a regression in the schema can't silently strip
the data the tools need.
"""

from __future__ import annotations

from helixis.buildium.schemas import (
    Lease,
    LeaseAccountDetails,
    LeaseMoveOut,
    OutstandingBalance,
    Tenant,
    Unit,
    WorkOrder,
)


def test_lease_message_round_trip_includes_current_tenants_and_cosigners() -> None:
    payload = {
        "Id": 5678,
        "PropertyId": 100,
        "UnitId": 200,
        "UnitNumber": "4B",
        "LeaseFromDate": "2025-10-01T00:00:00",
        "LeaseToDate": "2026-09-30T00:00:00",
        "LeaseStatus": "Active",
        "LeaseType": "Fixed",
        "TermType": "Fixed",
        "Rent": 1850.00,
        "IsEvictionPending": False,
        "CurrentTenants": [
            {
                "Id": 12345,
                "FirstName": "Sarah",
                "LastName": "Chen",
                "Email": "sarah@example.com",
            },
            {
                "Id": 12346,
                "FirstName": "John",
                "LastName": "Chen",
                "Email": "john@example.com",
            },
        ],
        "Cosigners": [
            {"Id": 99999, "FirstName": "Mom", "LastName": "Chen"},
        ],
        "Tenants": [
            {"Id": 12345, "Status": "Active", "MoveInDate": "2025-10-01T00:00:00"},
        ],
        "CurrentNumberOfOccupants": 2,
        "PaymentDueDay": 1,
    }
    lease = Lease.model_validate(payload)

    assert lease.id == 5678
    assert lease.unit_number == "4B"
    assert lease.lease_type == "Fixed"
    assert lease.term_type == "Fixed"
    assert lease.is_eviction_pending is False
    assert lease.current_number_of_occupants == 2
    assert lease.payment_due_day == 1

    assert lease.current_tenants is not None
    assert len(lease.current_tenants) == 2
    assert lease.current_tenants[0].id == 12345
    assert lease.current_tenants[0].first_name == "Sarah"
    assert lease.current_tenants[0].email == "sarah@example.com"

    assert lease.cosigners is not None
    assert lease.cosigners[0].first_name == "Mom"

    assert lease.tenants is not None
    assert lease.tenants[0].status == "Active"


def test_tenant_message_round_trip_includes_phones_and_alt_email_and_leases() -> None:
    payload = {
        "Id": 12345,
        "FirstName": "Sarah",
        "LastName": "Chen",
        "Email": "sarah@example.com",
        "AlternateEmail": "sarah.alt@example.com",
        "PhoneNumbers": [
            {"Number": "(555) 123-4567", "Type": "Mobile"},
            {"Number": "(555) 765-4321", "Type": "Home"},
        ],
        "Address": {
            "AddressLine1": "123 Main St",
            "City": "Austin",
            "State": "TX",
            "PostalCode": "78701",
        },
        "Leases": [
            {"Id": 5678, "Status": "Active"},
        ],
    }
    tenant = Tenant.model_validate(payload)

    assert tenant.id == 12345
    assert tenant.email == "sarah@example.com"
    assert tenant.alternate_email == "sarah.alt@example.com"

    assert tenant.phone_numbers is not None
    assert tenant.phone_numbers[0].number == "(555) 123-4567"
    assert tenant.phone_numbers[0].type == "Mobile"

    assert tenant.address is not None
    assert tenant.address.address_line1 == "123 Main St"
    assert tenant.address.city == "Austin"

    assert tenant.leases is not None
    assert tenant.leases[0].id == 5678
    assert tenant.leases[0].status == "Active"


def test_outstanding_balance_round_trip_legacy_payload() -> None:
    """Pre-PR-#78 shape (synthesized — the OpenAPI spec doesn't list
    ``UnitNumber``/``Status``/``DaysLate``/``RentAmount``). Kept as
    a back-compat round-trip so any cached fixture / replay using
    the old aliases still parses without raising."""
    payload = {
        "LeaseId": 10,
        "PropertyId": 100,
        "UnitId": 200,
        "UnitNumber": "4B",
        "Status": "Active",
        "DaysLate": 15,
        "RentAmount": 1850.0,
        "OutstandingBalance": 1850.0,  # synthesized — not in spec
    }
    bal = OutstandingBalance.model_validate(payload)
    assert bal.lease_id == 10
    assert bal.unit_number == "4B"
    assert bal.days_late == 15
    # The "OutstandingBalance" alias is no longer typed (PR #78);
    # extra="allow" on the base lets it round-trip but it's not a
    # populated field on the model.
    assert bal.total_balance is None


def test_outstanding_balance_round_trip_spec_payload() -> None:
    """PR #78 — the real Buildium ``LeaseOutstandingBalanceMessage``
    shape per the published OpenAPI spec. ``TotalBalance`` is the
    actual field name; pre-PR-#78 we typed an alias for
    ``OutstandingBalance`` which doesn't exist on the wire, so
    every parsed row had ``outstanding_balance is None`` and the
    downstream "balance > 0" filter dropped them all silently."""
    payload = {
        "LeaseId": 10,
        "PropertyId": 100,
        "UnitId": 200,
        "TotalBalance": 1850.0,
        "Balance0To30Days": 0.0,
        "Balance31To60Days": 1850.0,
        "Balance61To90Days": 0.0,
        "BalanceOver90Days": 0.0,
        "Balances": [
            {"GLAccountId": 4001, "Balance": 1850.0},
        ],
        "PastDueEmailSentDate": "2026-05-15T08:00:00",
        "EvictionPendingDate": "2026-05-20T00:00:00",
        "IsNoticeGiven": True,
    }
    bal = OutstandingBalance.model_validate(payload)
    assert bal.lease_id == 10
    assert bal.property_id == 100
    assert bal.unit_id == 200
    # The previously-misnamed field now lands.
    assert bal.total_balance == 1850.0
    assert bal.balance_31_to_60_days == 1850.0
    assert bal.balance_over_90_days == 0.0
    assert bal.is_notice_given is True
    # GL breakdown round-trips as a permissive list.
    assert bal.balances is not None
    assert len(bal.balances) == 1


def test_outstanding_balance_prefers_total_balance_over_legacy_alias() -> None:
    """If a payload carries BOTH the spec name and the synthesized
    legacy name, the spec name wins because that's what the
    production Buildium API actually emits."""
    payload = {
        "LeaseId": 11,
        "TotalBalance": 2400.0,
        "OutstandingBalance": 999.0,  # legacy / unused
    }
    bal = OutstandingBalance.model_validate(payload)
    assert bal.total_balance == 2400.0


# ── PR #71 — loosened "required" fields that Buildium can omit ────────


def test_work_order_accepts_missing_property_id() -> None:
    """Regression for the Vertex / Buildium sandbox bug Brandon hit:
    Buildium returned a work order without a ``PropertyId`` and the
    whole ``list_open_work_orders`` call 500'd. Loosening the field
    means one odd row no longer poisons the entire batch."""
    wo = WorkOrder.model_validate({"Id": 1, "Title": "Leak under sink", "Status": "Open"})
    assert wo.id == 1
    assert wo.property_id is None
    assert wo.title == "Leak under sink"


def test_work_order_still_accepts_property_id_when_present() -> None:
    """The loosened field still typecasts present values — no regression
    for the happy path that already works."""
    wo = WorkOrder.model_validate({"Id": 2, "PropertyId": 99, "Title": "HVAC"})
    assert wo.property_id == 99


def test_unit_accepts_missing_property_id() -> None:
    """Same shape as the WorkOrder fix — units in the sandbox can have
    PropertyId omitted on rare draft / soft-deleted rows."""
    unit = Unit.model_validate({"Id": 412, "UnitNumber": "3B"})
    assert unit.id == 412
    assert unit.property_id is None
    assert unit.unit_number == "3B"


def test_unit_still_accepts_property_id_when_present() -> None:
    unit = Unit.model_validate({"Id": 412, "PropertyId": 31, "UnitNumber": "3B"})
    assert unit.property_id == 31


# ── PR #74 — Lease.rent reads from AccountDetails ────────────────────


def test_lease_rent_reads_from_account_details_nested_field() -> None:
    """Pre-PR-#74 ``Lease.rent`` was a top-level ``Field(alias='Rent')``
    that silently parsed as None against every real Buildium response
    (the field lives inside ``AccountDetails``, not at the top level).
    That bug rendered ``$0.00/mo`` for 21 active leases in Brandon's
    sandbox test. This pins the new accessor that reads through the
    nested block."""
    lease = Lease.model_validate(
        {
            "Id": 100,
            "AccountDetails": {"Rent": 1850.50, "SecurityDeposit": 500.0},
            "LeaseStatus": "Active",
        }
    )
    assert lease.rent == 1850.50
    assert lease.account_details is not None
    assert lease.account_details.security_deposit == 500.0


def test_lease_rent_none_when_account_details_missing() -> None:
    """Legacy / partial Buildium responses with no AccountDetails
    block parse cleanly to ``rent is None`` — never crashes, never
    spoofs a number."""
    lease = Lease.model_validate({"Id": 101, "LeaseStatus": "Active"})
    assert lease.rent is None
    assert lease.account_details is None


def test_lease_rent_none_when_account_details_omits_rent() -> None:
    """``AccountDetails`` present but no ``Rent`` key (security-deposit-
    only update payload, etc.) still resolves to None on the
    convenience accessor."""
    lease = Lease.model_validate({"Id": 102, "AccountDetails": {"SecurityDeposit": 300.0}})
    assert lease.rent is None
    assert lease.account_details is not None
    assert lease.account_details.security_deposit == 300.0


def test_lease_account_details_direct_round_trip() -> None:
    """The standalone ``LeaseAccountDetails`` model parses on its own
    so any future endpoint returning just the account block (e.g. a
    PATCH response) keeps working."""
    block = LeaseAccountDetails.model_validate({"Rent": 1234.0})
    assert block.rent == 1234.0


# ── Sentry PYTHON-FASTAPI-2Y — move-out rows carry no ``Id`` ─────────


def test_lease_move_out_parses_prod_row_without_id() -> None:
    """Regression for the ``Lease.MoveOut.Created`` webhook crash-loop.

    The live ``/leases/{leaseId}/moveouts`` row Buildium returned in prod
    (2026-07-25) had NO ``Id`` — just ``TenantId``/``MoveOutDate``/
    ``NoticeGivenDate``. ``Id`` was typed required (``int``), so
    ``schema.model_validate`` 422'd on every move-out event and the handler
    released the event back to the worker to retry forever. This pins the
    loosened field against the exact prod payload."""
    mo = LeaseMoveOut.model_validate(
        {"TenantId": 195782, "MoveOutDate": "2025-06-30", "NoticeGivenDate": None}
    )
    assert mo.id is None
    assert mo.tenant_id == 195782
    assert mo.move_out_date is not None
    assert mo.move_out_date.date().isoformat() == "2025-06-30"
    assert mo.notice_given_date is None


def test_lease_move_out_still_accepts_id_when_present() -> None:
    """No regression for a row that DOES echo ``Id`` — it still typecasts."""
    mo = LeaseMoveOut.model_validate(
        {"Id": 42, "LeaseId": 5678, "TenantId": 195782, "MoveOutDate": "2025-06-30"}
    )
    assert mo.id == 42
    assert mo.lease_id == 5678
