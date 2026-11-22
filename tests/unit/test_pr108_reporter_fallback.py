"""PR #108 — Reporter column fallback via CreatedByUserId.

Per Buildium's Tasks API documentation, the four task subtypes
carry the requester differently:

  * ``/tasks/residentrequests`` — populated ``RequestedByUserEntity``
  * ``/tasks/rentalownerrequests`` — populated ``RequestedByUserEntity``
  * ``/tasks/contactrequests`` — populated ``RequestedByUserEntity``
  * ``/tasks/todorequests`` — staff to-dos, **NO**
    ``RequestedByUserEntity``. The creator IS the reporter.

Production observation: staff-created todorequests showed up in
the Inbox with Reporter = '—' even though Buildium had the
``CreatedByUserId`` field populated. This PR closes that gap by
falling back to the staff cache when the explicit requester is
absent. Mirrors PR #103's assignee resolution path.

Tests pin:
- ``EnrichedTask`` accepts the new ``created_by_user_id`` field.
- ``_task_from_raw`` extracts ``CreatedByUserId`` from Buildium's
  task payload.
- ``_decorate_enrichment_reporter`` injects requested_by_first_name
  / requested_by_last_name when the explicit fields are absent
  and the creator resolves through the staff map.
- The explicit ``RequestedByUserEntity`` data is NEVER overwritten
  (resident-supplied requester wins over staff-creator fallback).
- Defensive no-ops on missing enrichment / missing task / missing
  creator id / id-not-in-staff-map / unparseable id.
"""

from __future__ import annotations

from typing import Any

from helixis.agent.enrichment.buildium_task_enricher import _task_from_raw
from helixis.agent.enrichment.models import EnrichedTask
from helixis.api.routes.buildium import _decorate_enrichment_reporter

# ── EnrichedTask carries the new field ───────────────────────────


def test_enriched_task_accepts_created_by_user_id() -> None:
    task = EnrichedTask(id=17435, created_by_user_id=70287)
    assert task.created_by_user_id == 70287


def test_enriched_task_created_by_user_id_is_optional() -> None:
    """Prod rows written before PR #108 don't have the field;
    they must still deserialize cleanly."""
    task = EnrichedTask(id=17435)
    assert task.created_by_user_id is None


# ── _task_from_raw extracts CreatedByUserId per Buildium API ──────


def test_task_from_raw_extracts_created_by_user_id() -> None:
    """A staff-created todorequest from Buildium: no
    RequestedByUserEntity, but CreatedByUserId is always present."""
    raw: dict[str, Any] = {
        "Id": 17435,
        "Title": "Loud Crashing",
        "Description": "Bathroom mirror fell.",
        "Priority": "Normal",
        "AssignedToUserId": 70288,
        "CreatedByUserId": 70287,
        # No RequestedByUserEntity — this is a todorequest.
    }
    enriched = _task_from_raw(raw)
    assert enriched.created_by_user_id == 70287
    # And the requested_by_* fields remain None because Buildium
    # didn't supply them.
    assert enriched.requested_by_first_name is None
    assert enriched.requested_by_last_name is None


def test_task_from_raw_preserves_requested_by_when_present() -> None:
    """A residentrequest from Buildium: RequestedByUserEntity is
    populated, CreatedByUserId is also populated (different person
    — the creator might be staff who entered it on behalf of the
    resident). Both fields land on the EnrichedTask."""
    raw: dict[str, Any] = {
        "Id": 17435,
        "Title": "Loud crashing",
        "RequestedByUserEntity": {
            "Id": 9001,
            "Type": "Resident",
            "FirstName": "Sarah",
            "LastName": "Chen",
        },
        "CreatedByUserId": 70287,
    }
    enriched = _task_from_raw(raw)
    assert enriched.requested_by_first_name == "Sarah"
    assert enriched.requested_by_last_name == "Chen"
    assert enriched.requested_by_tenant_id == 9001
    assert enriched.created_by_user_id == 70287


def test_task_from_raw_handles_missing_created_by_user_id() -> None:
    """Defensive: a malformed payload missing CreatedByUserId
    must not break enrichment."""
    raw: dict[str, Any] = {"Id": 17435, "Title": "x"}
    enriched = _task_from_raw(raw)
    assert enriched.created_by_user_id is None


# ── _decorate_enrichment_reporter fallback ────────────────────────


def test_reporter_fallback_populates_when_requested_by_absent() -> None:
    """The motivating case: a todorequest with no
    RequestedByUserEntity, CreatedByUserId resolves to a staff
    member. The route fills requested_by_* so formatReporter on
    the frontend renders the name."""
    enrichment = {
        "task": {
            "id": 17435,
            "created_by_user_id": 70287,
            "requested_by_first_name": None,
            "requested_by_last_name": None,
        }
    }
    staff_map = {70287: "Vinny Dan"}
    out = _decorate_enrichment_reporter(enrichment, staff_map)
    assert out is enrichment
    assert enrichment["task"]["requested_by_first_name"] == "Vinny"
    assert enrichment["task"]["requested_by_last_name"] == "Dan"


def test_reporter_fallback_handles_single_word_name() -> None:
    """Staff cache returned a one-word name (e.g. ``Madonna``).
    Goes into first_name; last_name stays None."""
    enrichment = {
        "task": {
            "id": 17435,
            "created_by_user_id": 70287,
        }
    }
    staff_map = {70287: "Madonna"}
    _decorate_enrichment_reporter(enrichment, staff_map)
    assert enrichment["task"]["requested_by_first_name"] == "Madonna"
    assert enrichment["task"]["requested_by_last_name"] is None


def test_reporter_fallback_handles_multi_word_last_name() -> None:
    """Splits on FIRST space: 'Mary Jane Smith' → first='Mary',
    last='Jane Smith'. formatReporter then concatenates which
    renders correctly."""
    enrichment = {
        "task": {
            "id": 17435,
            "created_by_user_id": 70287,
        }
    }
    staff_map = {70287: "Mary Jane Smith"}
    _decorate_enrichment_reporter(enrichment, staff_map)
    assert enrichment["task"]["requested_by_first_name"] == "Mary"
    assert enrichment["task"]["requested_by_last_name"] == "Jane Smith"


def test_reporter_fallback_does_not_overwrite_explicit_requester() -> None:
    """A residentrequest already has RequestedByUserEntity →
    requested_by_first_name. Fallback MUST NOT overwrite it with
    the creator. The resident-supplied identity wins."""
    enrichment = {
        "task": {
            "id": 17435,
            "created_by_user_id": 70287,
            "requested_by_first_name": "Sarah",
            "requested_by_last_name": "Chen",
        }
    }
    staff_map = {70287: "Vinny Dan"}
    _decorate_enrichment_reporter(enrichment, staff_map)
    # Sarah Chen still wins.
    assert enrichment["task"]["requested_by_first_name"] == "Sarah"
    assert enrichment["task"]["requested_by_last_name"] == "Chen"


def test_reporter_fallback_skips_when_only_one_requested_field_present() -> None:
    """A partial requester (e.g. first_name set but last_name None)
    still counts as 'requester supplied' — don't overwrite."""
    enrichment = {
        "task": {
            "id": 17435,
            "created_by_user_id": 70287,
            "requested_by_first_name": "Madonna",
            "requested_by_last_name": None,
        }
    }
    staff_map = {70287: "Vinny Dan"}
    _decorate_enrichment_reporter(enrichment, staff_map)
    assert enrichment["task"]["requested_by_first_name"] == "Madonna"
    assert enrichment["task"]["requested_by_last_name"] is None


def test_reporter_fallback_no_op_when_enrichment_none() -> None:
    assert _decorate_enrichment_reporter(None, {70287: "Vinny Dan"}) is None


def test_reporter_fallback_no_op_when_task_missing() -> None:
    enrichment = {"unit": {"unit_number": "4B"}}
    out = _decorate_enrichment_reporter(enrichment, {70287: "Vinny Dan"})
    assert out is enrichment
    assert "task" not in enrichment


def test_reporter_fallback_no_op_when_created_by_user_id_null() -> None:
    enrichment = {"task": {"id": 17435, "created_by_user_id": None}}
    out = _decorate_enrichment_reporter(enrichment, {70287: "Vinny Dan"})
    assert out is enrichment
    assert "requested_by_first_name" not in enrichment["task"]


def test_reporter_fallback_no_op_when_id_not_in_staff_map() -> None:
    """The creator was a now-deleted staff user, or staff cache
    hasn't synced yet. Frontend's '—' fallback kicks in."""
    enrichment = {"task": {"id": 17435, "created_by_user_id": 99999}}
    out = _decorate_enrichment_reporter(enrichment, {70287: "Vinny Dan"})
    assert out is enrichment
    assert "requested_by_first_name" not in enrichment["task"]


def test_reporter_fallback_handles_string_id_gracefully() -> None:
    """JSONB roundtrips can stringify numeric IDs. Coerce + resolve."""
    enrichment = {"task": {"id": 17435, "created_by_user_id": "70287"}}
    _decorate_enrichment_reporter(enrichment, {70287: "Vinny Dan"})
    assert enrichment["task"]["requested_by_first_name"] == "Vinny"
    assert enrichment["task"]["requested_by_last_name"] == "Dan"


def test_reporter_fallback_no_op_when_id_unparseable() -> None:
    enrichment = {"task": {"id": 17435, "created_by_user_id": "not-a-number"}}
    out = _decorate_enrichment_reporter(enrichment, {70287: "Vinny Dan"})
    assert out is enrichment
    assert "requested_by_first_name" not in enrichment["task"]


def test_reporter_fallback_no_op_when_staff_map_empty() -> None:
    """Pre-migration / sync-not-yet-run: staff_map is {}. No-op."""
    enrichment = {"task": {"id": 17435, "created_by_user_id": 70287}}
    out = _decorate_enrichment_reporter(enrichment, {})
    assert out is enrichment
    assert "requested_by_first_name" not in enrichment["task"]
