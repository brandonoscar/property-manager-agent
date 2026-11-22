"""PR #82 — pin the InboxPage-shaped surface builder.

The receiver writes whatever this function produces directly into
``buildium_events.action_surface``, where InboxPage reads it via
``GET /buildium/recent-events?include=action_surface``. The shape
MUST match ``InboxActionSurface`` on the frontend verbatim — no
backwards-incompatible field renames, no missing required keys.
"""

from __future__ import annotations

from helixis.db.repositories.buildium_staff import BuildiumStaffRow
from helixis.webhooks.actions import RoutingDecision
from helixis.webhooks.surface_builder import (
    SCHEMA_VERSION,
    _buildium_url,
    build_action_surface_from_routing,
)


def _admin_decision(buildium_user_id: int | None = None) -> RoutingDecision:
    return RoutingDecision(
        action_type="assign_to_user",
        assigned_buildium_user_id=buildium_user_id,
        assigned_helixis_user_id="admin-uuid",
        surface="inbox",
        reason="test",
    )


def _staff(buildium_user_id: int, first: str, last: str) -> BuildiumStaffRow:
    return BuildiumStaffRow(
        buildium_user_id=buildium_user_id,
        first_name=first,
        last_name=last,
        email=None,
        user_types=["Staff"],
        user_role_id=None,
        user_role_name=None,
        is_active=True,
    )


def test_surface_has_schema_version_and_required_keys() -> None:
    """Frontend ``InboxActionSurface`` requires ``summary`` +
    ``actions`` + ``freeform_placeholder``. Schema version v2 was
    introduced by PR #82."""
    surface = build_action_surface_from_routing(
        envelope={"EventName": "Task.Created", "TaskId": 1, "Title": "x"},
        event_name="Task.Created",
        decision=_admin_decision(buildium_user_id=42),
        staff_cache={42: _staff(42, "Robert", "Friedman")},
    )
    assert surface["schema_version"] == SCHEMA_VERSION
    assert "summary" in surface
    assert "actions" in surface
    assert "freeform_placeholder" in surface


def test_summary_uses_task_title() -> None:
    """``Title=Loud Crashing`` + assignee=Robert Friedman → the
    summary string Brandon expects to see in the Inbox card."""
    surface = build_action_surface_from_routing(
        envelope={"EventName": "Task.Created", "TaskId": 17434, "Title": "Loud Crashing"},
        event_name="Task.Created",
        decision=_admin_decision(buildium_user_id=42),
        staff_cache={42: _staff(42, "Robert", "Friedman")},
    )
    assert surface["summary"] == "Task: Loud Crashing — assigned to Robert Friedman"


def test_summary_no_id_leak() -> None:
    """HARD PRESENTATION RULES — the raw Buildium task id MUST NOT
    appear anywhere in the user-facing summary. Regression for the
    pre-PR-#80 ``task #17434`` Activity row."""
    surface = build_action_surface_from_routing(
        envelope={"EventName": "Task.Created", "TaskId": 17434, "Title": "Loud Crashing"},
        event_name="Task.Created",
        decision=_admin_decision(buildium_user_id=42),
        staff_cache={42: _staff(42, "Robert", "Friedman")},
    )
    assert "17434" not in surface["summary"]
    assert "task #" not in surface["summary"].lower()


def test_summary_unknown_assignee_falls_back_to_admin_text() -> None:
    """Empty ``staff_cache`` (sync never ran) → assignee text is
    ``"the admin"``. Never the Buildium user id, never empty."""
    surface = build_action_surface_from_routing(
        envelope={"EventName": "Task.Created", "TaskId": 1, "Title": "x"},
        event_name="Task.Created",
        decision=_admin_decision(buildium_user_id=42),
        staff_cache={},  # empty cache
    )
    assert "the admin" in surface["summary"]


def test_summary_admin_fallback_when_decision_has_no_user_id() -> None:
    """Routing fell to the admin branch (assigned_buildium_user_id=
    None) → same ``the admin`` text. Pins both paths converge."""
    surface = build_action_surface_from_routing(
        envelope={"EventName": "Task.Created", "TaskId": 1, "Title": "x"},
        event_name="Task.Created",
        decision=_admin_decision(buildium_user_id=None),
        staff_cache={42: _staff(42, "Robert", "Friedman")},
    )
    assert "the admin" in surface["summary"]


def test_buildium_url_uses_envelope_task_id() -> None:
    """The ``_buildium_url`` helper still resolves the task id from the
    envelope (kept for future deep-links), even though the floor surface
    no longer emits an external "Open in Buildium" action."""
    assert _buildium_url({"TaskId": 99}, "Task.Created").endswith("/tasks/99")


def test_buildium_url_falls_back_to_tasks_root_without_id() -> None:
    assert _buildium_url({"Title": "x"}, "Task.Created").endswith("/tasks")


def test_floor_action_is_native_internal_note_for_task() -> None:
    """UI refinement #3 / obs#1 — the deterministic floor action is now a
    NATIVE in-app action (add_internal_note), not an external "Open in
    Buildium" bounce. Work happens inside Helixis."""
    surface = build_action_surface_from_routing(
        envelope={"EventName": "Task.Created", "TaskId": 1, "Title": "x"},
        event_name="Task.Created",
        decision=_admin_decision(buildium_user_id=42),
        staff_cache={42: _staff(42, "Robert", "Friedman")},
    )
    assert len(surface["actions"]) == 1
    assert surface["actions"][0]["tool_id"] == "add_internal_note"
    # No external-link parameters remain on the floor action.
    assert "buildium_url" not in surface["actions"][0]["parameters"]


def test_summary_without_title_uses_generic_phrase() -> None:
    """No ``Title`` in the envelope → ``"Task created — assigned to X"``.
    No id, no empty quotes, no NULL-injection."""
    surface = build_action_surface_from_routing(
        envelope={"EventName": "Task.Created", "TaskId": 1},  # no Title
        event_name="Task.Created",
        decision=_admin_decision(buildium_user_id=42),
        staff_cache={42: _staff(42, "Robert", "Friedman")},
    )
    assert surface["summary"] == "Task created — assigned to Robert Friedman"


def test_summary_reads_nested_envelope_title() -> None:
    """Some Buildium webhook shapes nest the entity under
    ``Event.EventData.{Entity}.Title``. Both root and nested must
    work — the receiver doesn't know which Buildium account is on
    which envelope version."""
    envelope = {
        "EventName": "Task.Created",
        "Event": {"EventData": {"Task": {"Title": "Nested title"}}},
        "TaskId": 1,
    }
    surface = build_action_surface_from_routing(
        envelope=envelope,
        event_name="Task.Created",
        decision=_admin_decision(buildium_user_id=42),
        staff_cache={42: _staff(42, "Robert", "Friedman")},
    )
    assert "Nested title" in surface["summary"]


def test_summary_handles_non_task_event_without_crashing() -> None:
    """Lease / WorkOrder / unknown events must still produce a valid
    surface — the routing surface is the floor for EVERY event, not
    just tasks."""
    surface = build_action_surface_from_routing(
        envelope={"EventName": "Lease.Created", "LeaseId": 1},
        event_name="Lease.Created",
        decision=_admin_decision(buildium_user_id=None),
        staff_cache={},
    )
    assert isinstance(surface["summary"], str)
    assert "the admin" in surface["summary"]
    assert isinstance(surface["actions"], list)
