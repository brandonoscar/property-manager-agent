"""Unit tests for the Buildium event envelope + dedup key."""

from __future__ import annotations

from helixis.webhooks.event_schemas import BuildiumEventEnvelope, dedup_key


def _payload(**extra):
    base = {
        "EventName": "Lease.Created",
        "EventDateTime": "2026-04-20T19:43:00.0000000Z",
        "AccountId": 42,
    }
    base.update(extra)
    return base


def test_envelope_parses_common_fields() -> None:
    env = BuildiumEventEnvelope.model_validate(_payload(LeaseId=99))
    assert env.event_name == "Lease.Created"
    assert env.account_id == 42


def test_envelope_extracts_entity_ids() -> None:
    payload = _payload(LeaseId=99, UnitId=7, PropertyId=3)
    env = BuildiumEventEnvelope.model_validate(payload)
    ids = env.entity_ids(payload)
    assert ids == {"LeaseId": 99, "UnitId": 7, "PropertyId": 3}


def test_envelope_accepts_unknown_event_name() -> None:
    env = BuildiumEventEnvelope.model_validate(_payload(EventName="Some.Future.Event"))
    assert env.event_name == "Some.Future.Event"


def test_dedup_key_is_stable_for_same_event() -> None:
    a = _payload(LeaseId=99)
    b = _payload(LeaseId=99)
    assert dedup_key(a) == dedup_key(b)


def test_dedup_key_differs_by_entity_id() -> None:
    assert dedup_key(_payload(LeaseId=99)) != dedup_key(_payload(LeaseId=100))


def test_dedup_key_includes_all_present_entity_ids() -> None:
    # Every present id is folded in (fixed order), not just the first —
    # so a multi-entity event keys on its full identity.
    key = dedup_key(_payload(LeaseId=1, TenantId=2))
    assert "LeaseId=1" in key
    assert "TenantId=2" in key


def test_same_second_charges_on_one_lease_do_not_collide() -> None:
    # The 2026-07 audit data-loss bug: a rent charge carries BOTH LeaseId and
    # TransactionId. Keying on LeaseId alone made two same-second charges on
    # one lease dedup to the same key → the second dropped by ON CONFLICT.
    # Distinct TransactionIds must now yield distinct keys.
    a = _payload(LeaseId=5, TransactionId=100)
    b = _payload(LeaseId=5, TransactionId=101)
    assert dedup_key(a) != dedup_key(b)
    assert "TransactionId=100" in dedup_key(a)


def test_dedup_key_stable_for_genuine_redelivery() -> None:
    # An identical Buildium redelivery (same ids) must still key identically,
    # so real duplicates are still caught by the unique index.
    a = _payload(LeaseId=5, TransactionId=100)
    b = _payload(LeaseId=5, TransactionId=100)
    assert dedup_key(a) == dedup_key(b)


def test_dedup_key_is_scoped_by_account_id() -> None:
    """Regression: two tenants receiving structurally identical events
    in the same second must not collide on the dedup key, since the
    unique index would reject the second insert and one tenant would
    silently drop the event."""
    payload_a = _payload(AccountId=1, LeaseId=99)
    payload_b = _payload(AccountId=2, LeaseId=99)
    assert dedup_key(payload_a) != dedup_key(payload_b)
    assert dedup_key(payload_a).startswith("account=1|")
    assert dedup_key(payload_b).startswith("account=2|")
