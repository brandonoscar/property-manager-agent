"""Unit tests for the invariant library (evals/invariants.py) — Tier-2 #7.

Each of the 7 invariants gets a HOLDS case (no violation) and a VIOLATED case
(exactly one violation), plus the cross-item dedupe and the check_all dispatcher.
Fully deterministic — no model, no sandbox.
"""

from __future__ import annotations

from helixis.evals.invariants import (
    charge_references_active_lease,
    check_all,
    lease_dates_ordered,
    money_amounts_unmodified,
    no_duplicate_open_work_orders,
    notice_respects_period,
    tenant_email_has_consent,
    vendor_active_for_property,
    wo_close_has_completion_timestamp,
)


# 1. work-order close needs a completion timestamp
def test_wo_close_holds_with_timestamp() -> None:
    assert (
        wo_close_has_completion_timestamp(
            {"work_order_id": 5, "new_status": "Completed", "completion_date": "2026-07-10"}
        )
        == []
    )


def test_wo_close_violated_without_timestamp() -> None:
    v = wo_close_has_completion_timestamp({"work_order_id": 5, "new_status": "Completed"})
    assert len(v) == 1 and v[0].invariant == "wo_close_needs_completion_ts"


def test_wo_non_close_is_ignored() -> None:
    assert (
        wo_close_has_completion_timestamp({"work_order_id": 5, "new_status": "In Progress"}) == []
    )


# 2. tenant email needs consent
def test_tenant_email_holds_with_consent() -> None:
    assert tenant_email_has_consent({"tenant": {"id": 1, "consent": True}}) == []


def test_tenant_email_violated_without_consent() -> None:
    for tenant in ({"id": 1, "consent": False}, {"id": 1}):
        v = tenant_email_has_consent({"tenant": tenant})
        assert len(v) == 1 and v[0].invariant == "tenant_email_needs_consent"


# 3. lease date ordering + active-lease charge
def test_lease_dates_hold_when_ordered() -> None:
    assert (
        lease_dates_ordered({"id": 9, "start_date": "2026-01-01", "end_date": "2026-12-31"}) == []
    )


def test_lease_dates_violated_when_reversed() -> None:
    v = lease_dates_ordered({"id": 9, "start_date": "2026-12-31", "end_date": "2026-01-01"})
    assert len(v) == 1 and v[0].invariant == "lease_dates_ordered"


def test_charge_holds_on_active_lease() -> None:
    assert charge_references_active_lease({"id": 3, "lease": {"id": 9, "status": "Active"}}) == []


def test_charge_violated_on_inactive_lease() -> None:
    v = charge_references_active_lease({"id": 3, "lease": {"id": 9, "status": "Ended"}})
    assert len(v) == 1 and v[0].invariant == "charge_active_lease"


# 4. vendor active for property
def test_vendor_holds_when_active_and_services_property() -> None:
    assert (
        vendor_active_for_property(
            {"property_id": 2, "vendor": {"id": 7, "active": True, "property_ids": [1, 2, 3]}}
        )
        == []
    )


def test_vendor_violated_when_inactive() -> None:
    v = vendor_active_for_property({"property_id": 2, "vendor": {"id": 7, "active": False}})
    assert any(x.invariant == "vendor_active" for x in v)


def test_vendor_violated_when_not_servicing_property() -> None:
    v = vendor_active_for_property(
        {"property_id": 9, "vendor": {"id": 7, "active": True, "property_ids": [1, 2]}}
    )
    assert any("does not service property 9" in x.detail for x in v)


# 5. no duplicate open work order
def test_no_duplicate_when_unique() -> None:
    wos = [
        {"id": 1, "unit_id": 10, "category": "Plumbing", "status": "New"},
        {"id": 2, "unit_id": 10, "category": "Electrical", "status": "Open"},
    ]
    assert no_duplicate_open_work_orders(wos) == []


def test_duplicate_open_wo_flagged() -> None:
    wos = [
        {"id": 1, "unit_id": 10, "category": "Plumbing", "status": "New"},
        {"id": 2, "unit_id": 10, "category": "plumbing", "status": "Open"},
    ]
    v = no_duplicate_open_work_orders(wos)
    assert len(v) == 1 and v[0].invariant == "no_duplicate_open_wo"


def test_closed_duplicate_is_allowed() -> None:
    wos = [
        {"id": 1, "unit_id": 10, "category": "Plumbing", "status": "Completed"},
        {"id": 2, "unit_id": 10, "category": "Plumbing", "status": "New"},
    ]
    assert no_duplicate_open_work_orders(wos) == []


# 6. notice respects the curing/notice period
def test_notice_holds_when_period_met() -> None:
    assert (
        notice_respects_period(
            {
                "id": 1,
                "sent_date": "2026-07-01",
                "effective_date": "2026-07-15",
                "required_days": 14,
            }
        )
        == []
    )


def test_notice_violated_when_too_short() -> None:
    v = notice_respects_period(
        {"id": 1, "sent_date": "2026-07-01", "effective_date": "2026-07-05", "required_days": 14}
    )
    assert len(v) == 1 and v[0].invariant == "notice_period"


# 7. money amounts pass through unmodified
def test_money_holds_when_passed_through() -> None:
    assert (
        money_amounts_unmodified(
            {
                "source_amounts": ["$1,200.00", 450, "75.50"],
                "output_amounts": ["$1,200.00", "75.50"],
            }
        )
        == []
    )


def test_money_violated_on_model_arithmetic() -> None:
    # 1200 + 450 = 1650 was computed by the model — not a source value.
    v = money_amounts_unmodified(
        {"source_amounts": ["1200.00", "450.00"], "output_amounts": ["1650.00"]}
    )
    assert len(v) == 1 and v[0].invariant == "money_unmodified"


# dispatcher + cross-item
def test_check_all_routes_and_dedupes() -> None:
    items = [
        {"kind": "work_order_close", "work_order_id": 1, "new_status": "Completed"},  # violation
        {"kind": "tenant_email", "tenant": {"id": 2, "consent": True}},  # ok
        {"kind": "work_order", "id": 10, "unit_id": 5, "category": "HVAC", "status": "New"},
        {"kind": "work_order", "id": 11, "unit_id": 5, "category": "HVAC", "status": "Open"},  # dup
    ]
    violations = check_all(items)
    kinds = {v.invariant for v in violations}
    assert "wo_close_needs_completion_ts" in kinds
    assert "no_duplicate_open_wo" in kinds
    assert len(violations) == 2


def test_check_all_clean_trajectory_has_no_violations() -> None:
    items = [
        {"kind": "lease", "id": 1, "start_date": "2026-01-01", "end_date": "2026-12-31"},
        {"kind": "tenant_email", "tenant": {"id": 2, "consent": True}},
        {"kind": "money", "source_amounts": ["100.00"], "output_amounts": ["100.00"]},
    ]
    assert check_all(items) == []


def test_violation_str_is_readable() -> None:
    v = wo_close_has_completion_timestamp({"work_order_id": 5, "new_status": "closed"})[0]
    assert str(v).startswith("[wo_close_needs_completion_ts]")
