"""50-state seed — pins on the pure core (the parts that can be wrong
without a Buildium account to tell you).

The wire-shape rules pinned here are the prod-probed contracts from
CLAUDE.md gotchas 21/22 and the synthetic_traffic PR #200/#201 trail:
lease-charge lines use GlAccountId (lower-l), payment lines GLAccountId
(caps-L), to-do tasks use TaskStatus while a WO-embedded task uses
Status, bills must have Date strictly before DueDate, and invented
tenants are NEVER emailed (SendWelcomeEmail pinned false).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "seed_states", Path(__file__).resolve().parents[2] / "scripts" / "seed_states.py"
)
seed_states = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
sys.modules["seed_states"] = seed_states
_SPEC.loader.exec_module(seed_states)


def test_state_table_is_complete_and_unique() -> None:
    abbrs = [s[0] for s in seed_states.STATES]
    assert len(abbrs) == 50
    assert len(set(abbrs)) == 50
    for abbr, name, street, city, zipc, first, last, rent in seed_states.STATES:
        assert len(abbr) == 2 and abbr.isupper()
        assert street and city and first and last and name
        assert zipc.isdigit() and len(zipc) == 5
        assert 800 <= rent <= 3000


def test_property_names_round_trip_through_the_prefix_parser() -> None:
    for i, state in enumerate(seed_states.STATES):
        name = seed_states.property_name(state[0], state[2])
        assert seed_states.state_prefix(name) == state[0], name
    # Non-seed names never parse as seed properties.
    assert seed_states.state_prefix("Cherry Street Duplex") is None
    assert seed_states.state_prefix("ok — lowercase") is None


def test_unit_budget_is_exactly_fifty() -> None:
    # The account tier caps at 50 units; every property must be single-unit.
    total_units = sum(
        len(seed_states.build_property_body(i, bank_account_id=1)["Units"])
        for i in range(len(seed_states.STATES))
    )
    assert total_units == 50


def test_chaos_rotation_covers_every_detector_shape() -> None:
    n = len(seed_states.STATES)
    ledgers = [seed_states.ledger_kit(i) for i in range(n)]
    bills = [seed_states.bill_kit(i) for i in range(n)]
    # Each ledger kit appears often enough to be visible in the product…
    for kit in ("overdue", "current", "partial", "overpaid"):
        assert ledgers.count(kit) >= 5, kit
    # …and each bill kit feeds its detector at scale.
    for kit in ("duplicate", "orphan", "overdue"):
        assert bills.count(kit) >= 10, kit
    # Tricks land on every other state, rotating the whole catalog.
    tricks = [seed_states.trick_for(i) for i in range(n)]
    keys = {t[0] for t in tricks if t}
    assert keys == {k for k, _t, _d in seed_states.TRICKS}
    # ~1 in 5 leases sits in the renewal window.
    assert sum(1 for i in range(n) if seed_states.renewal_window(i)) == 10


def test_lease_body_pins_the_probe_grounded_contract() -> None:
    body = seed_states.build_lease_body(4, unit_id=777, income_gl_id=42)  # CA
    # Required floor per LeasePostMessage.
    assert body["LeaseType"] == "Fixed"
    assert body["UnitId"] == 777
    assert body["LeaseFromDate"] < body["LeaseToDate"]
    # Invented tenants are NEVER emailed.
    assert body["SendWelcomeEmail"] is False
    tenant = body["Tenants"][0]
    assert tenant["FirstName"] and tenant["LastName"] and tenant["Address"]["AddressLine1"]
    assert tenant["Email"].endswith("@example.com")
    # Write-side charge casing: GlAccountId, lower-l (gotcha 21/22).
    charge = body["Rent"]["Charges"][0]
    assert charge["GlAccountId"] == 42
    assert "GLAccountId" not in charge


def test_bill_body_orders_dates_and_uses_lowercase_gl() -> None:
    body = seed_states.build_bill_body(
        1, 2, 3, amount=265.0, memo="m", date_offset=-60, due_offset=-30
    )
    assert body["Date"] < body["DueDate"]  # Buildium 422s otherwise
    line = body["Lines"][0]
    assert line["GlAccountId"] == 3
    assert line["AccountingEntity"] == {"Id": 1, "AccountingEntityType": "Rental"}


def test_task_and_work_order_status_key_asymmetry() -> None:
    task = seed_states.build_task_body(1, 2, "t", "d")
    assert task["TaskStatus"] == "New"  # to-do endpoint key
    assert "Status" not in task
    wo = seed_states.build_work_order_body(1, 2, 3)
    assert wo["Task"]["Status"] == "New"  # WO-embedded key (PR #201)
    assert "TaskStatus" not in wo["Task"]
    assert wo["EntryAllowed"] == "Unknown"
    assert wo["VendorId"] == 2


def test_route_corridor_states_exist_in_the_table() -> None:
    abbrs = {s[0] for s in seed_states.STATES}
    assert seed_states.ROUTE_STATES <= abbrs
    assert len(seed_states.ROUTE_STATES) == 10


def test_past_lease_ends_before_the_active_lease_starts() -> None:
    past = seed_states.build_past_lease_body(1, unit_id=9)
    active = seed_states.build_lease_body(1, unit_id=9, income_gl_id=1)
    # Historical term is fully in the past AND clears the active start —
    # overlapping leases on one unit are the 422 we must never trigger.
    assert past["LeaseFromDate"] < past["LeaseToDate"] < active["LeaseFromDate"]
    assert past["SendWelcomeEmail"] is False
    assert "Rent" not in past  # history must not add recurring-charge templates
    # A prior tenant never collides with the current tenant's identity.
    prior = past["Tenants"][0]
    current = active["Tenants"][0]
    assert (prior["FirstName"], prior["LastName"]) != (current["FirstName"], current["LastName"])


def test_renewal_body_is_exactly_the_422_walked_floor() -> None:
    body = seed_states.build_renewal_body(3, income_gl_id=42)
    # Gotcha 22: {LeaseType, SendWelcomeEmail, Rent{Cycle, Charges}} —
    # nothing more (extra keys are untested territory), nothing less.
    assert set(body) == {"LeaseType", "SendWelcomeEmail", "Rent"}
    assert body["SendWelcomeEmail"] is False
    charge = body["Rent"]["Charges"][0]
    assert set(charge) == {"Amount", "GlAccountId", "NextDueDate"}
    assert charge["GlAccountId"] == 42
    assert charge["Amount"] == seed_states.STATES[3][7] + 50


def test_wo_variety_titles_are_unique_and_distinct_from_the_corridor_wo() -> None:
    titles = [t for t, _d in seed_states.WO_VARIETY]
    assert len(set(titles)) == len(titles) == 5
    assert seed_states.WO_TITLE not in titles  # corridor extra stays distinct


def test_prior_tenant_derivation_never_collides_with_the_current_tenant() -> None:
    for i, state in enumerate(seed_states.STATES):
        first, last = seed_states.prior_tenant(i)
        assert (first, last) != (state[5], state[6]), state[0]


def test_owner_and_announcement_bodies_carry_property_scoping() -> None:
    owner = seed_states.build_owner_body("Margaret", "Whitlock", [1, 2, 3])
    assert owner["PropertyIds"] == [1, 2, 3]
    assert owner["IsCompany"] is False
    ann = seed_states.build_announcement_body("Subject", "Body", [7])
    assert ann["PropertyIds"] == [7]
    assert ann["ExpirationDate"] > seed_states._iso(0)
