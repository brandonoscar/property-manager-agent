"""PR #80 — routing-grid foundation + three webhook bug fixes.

Brandon's design (post-PR-#79 webhook activation):

> Renders properties for buildium with 5 dropboxes beside the properties.
> Inside the dropbox is the employees rendered in. So hard rule, No
> typing and adding properties or members. It all needs to come from
> buildium.

PR #80 ships the verifiable foundation:

1. **Event → category map** (``categorize``) — deterministic
   classification into one of five categories.
2. **Property resolver** (``extract_property_id``) — webhook envelope
   → Buildium property id or ``None``.
3. **Action handler registry** (``HANDLERS``) — keyed by
   ``action_type``; the only v1 entry is ``assign_to_user``.
4. **Routing pipeline** (``route_event``) — categorize → resolve →
   load property_assignment → handle → admin fallback.
5. **property_assignments migration** (0014) — schema is in place
   even though v1's UI doesn't write to it (Properties page ships
   in PR #81 once we have the verified /v1/users SDK).
6. **Three webhook bug fixes** — task/work-order titles now render
   instead of raw Buildium ids ("Buildium created a task: Send lease
   to Ryan" vs the pre-PR-#80 "task #17434"); the Activity row's
   "Unknown user" attribution becomes "Buildium" for webhook rows
   (tested at the backend repo level here; the frontend ActivityPage
   tweak is covered by manual smoke).

The Properties page UI, the Buildium /v1/users SDK, the staff sync
job, and the Inbox reassign dropdown all defer to PR #81 — they
require Buildium spec verification we don't have in this session.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.webhooks.actions import HANDLERS, get_handler
from helixis.webhooks.actions.assign_to_user import AssignToUserHandler
from helixis.webhooks.categorization import CATEGORIES, EVENT_CATEGORY, categorize
from helixis.webhooks.property_resolver import extract_property_id
from helixis.webhooks.routing import route_event

# ─────────────────────────────────────────────────────────────────────
# categorize
# ─────────────────────────────────────────────────────────────────────


def test_categories_match_brandon_five() -> None:
    """The five categories from the brief — pin order + names."""
    assert CATEGORIES == (
        "maintenance",
        "leasing",
        "accounting",
        "tenant_relations",
        "other",
    )


def test_categorize_known_events_are_correct() -> None:
    """Spot-check a few headline events from each lane. Every entry
    in ``EVENT_CATEGORY`` must resolve to itself — the dict IS the
    contract."""
    assert categorize("WorkOrder.Created") == "maintenance"
    assert categorize("TenantRequest.Created") == "maintenance"
    assert categorize("Lease.Created") == "leasing"
    assert categorize("Applicant.Updated") == "leasing"
    assert categorize("Bill.Paid") == "accounting"
    assert categorize("LeaseTransaction.Created") == "accounting"
    assert categorize("LeaseTenant.Updated") == "tenant_relations"
    assert categorize("Tenant.Created") == "tenant_relations"
    assert categorize("Vendor.Created") == "other"
    assert categorize("Task.Created") == "other"


def test_categorize_unknown_event_defaults_to_other() -> None:
    """Unknown events fall back to ``other`` so the router can always
    make a routing decision. NO LLM classification in v1."""
    assert categorize("Mystery.UFO") == "other"
    assert categorize("") == "other"


def test_every_categorized_event_uses_a_valid_category() -> None:
    """Every value in the map MUST be one of the five categories."""
    valid = set(CATEGORIES)
    bad = {k: v for k, v in EVENT_CATEGORY.items() if v not in valid}
    assert not bad, f"events with invalid categories: {bad}"


# ─────────────────────────────────────────────────────────────────────
# extract_property_id
# ─────────────────────────────────────────────────────────────────────


def test_resolver_lease_event_extracts_property_id() -> None:
    envelope = {
        "EventName": "Lease.Created",
        "AccountId": 1,
        "LeaseId": 5,
        "PropertyId": 100,
    }
    assert extract_property_id("Lease.Created", envelope) == 100


def test_resolver_workorder_event_extracts_property_id() -> None:
    assert (
        extract_property_id(
            "WorkOrder.Created",
            {"AccountId": 1, "WorkOrderId": 9, "PropertyId": 222},
        )
        == 222
    )


def test_resolver_task_event_with_property_extracts_property_id() -> None:
    assert (
        extract_property_id(
            "Task.Created",
            {"AccountId": 1, "TaskId": 9, "PropertyId": 333},
        )
        == 333
    )


def test_resolver_task_event_without_property_returns_none() -> None:
    """A property-manager-wide task (no PropertyId on the envelope)
    routes via the 'no property' default row."""
    assert extract_property_id("Task.Created", {"AccountId": 1, "TaskId": 9}) is None


def test_resolver_bill_event_pulls_from_lines_first_property() -> None:
    """Bills carry PropertyId per-line under ``Lines[]`` — return the
    first non-null one so the bill routes to its primary property."""
    envelope = {
        "EventName": "Bill.Created",
        "AccountId": 1,
        "BillId": 7,
        "Lines": [
            {"Amount": 100.0},  # no PropertyId
            {"Amount": 300.0, "PropertyId": 444},
            {"Amount": 200.0, "PropertyId": 555},  # ignored — first wins
        ],
    }
    assert extract_property_id("Bill.Created", envelope) == 444


def test_resolver_vendor_event_has_no_property_tie() -> None:
    """Vendor events route via the 'no property' default row."""
    assert extract_property_id("Vendor.Created", {"AccountId": 1, "VendorId": 42}) is None


def test_resolver_handles_nested_body_shape() -> None:
    """Newer Buildium webhook shapes nest the entity under ``Body``;
    older ones put fields at the top level. Both must work."""
    nested = {
        "EventName": "Lease.Created",
        "AccountId": 1,
        "Body": {"LeaseId": 5, "PropertyId": 600},
    }
    assert extract_property_id("Lease.Created", nested) == 600


def test_resolver_rejects_bool_masquerading_as_int() -> None:
    """``isinstance(True, int)`` is True; the helper must reject so a
    boolean PropertyId field doesn't get used as a property id."""
    envelope = {
        "EventName": "Lease.Created",
        "AccountId": 1,
        "PropertyId": True,  # nonsense — should be filtered out
    }
    assert extract_property_id("Lease.Created", envelope) is None


def test_resolver_accepts_numeric_strings() -> None:
    """Some Buildium payloads stringify ints; accept "100" as 100."""
    assert (
        extract_property_id(
            "Lease.Created",
            {"AccountId": 1, "PropertyId": "100"},
        )
        == 100
    )


# ─────────────────────────────────────────────────────────────────────
# Action handler registry
# ─────────────────────────────────────────────────────────────────────


def test_assign_to_user_handler_is_registered() -> None:
    """``HANDLERS["assign_to_user"]`` must resolve at import time —
    side-effect import in ``actions/__init__.py`` runs it."""
    assert "assign_to_user" in HANDLERS
    handler = get_handler("assign_to_user")
    assert handler is not None
    assert isinstance(handler, AssignToUserHandler)


def test_unknown_action_type_returns_none() -> None:
    """Forward-compat: ``get_handler("automate")`` must return None
    (not raise) so the router can fall back to admin instead of
    crashing on a database row from a future PR."""
    assert get_handler("automate") is None


@pytest.mark.asyncio
async def test_assign_to_user_with_buildium_user_id_routes_to_admin_inbox() -> None:
    """v1: the assigned Helixis user is the company admin (the
    Buildium↔Helixis user-mapping ships in PR #81). The Buildium
    user id is preserved on the decision so future linkage can
    backfill it."""
    handler = AssignToUserHandler()
    with patch(
        "helixis.webhooks.actions.assign_to_user.resolve_admin_user_id",
        new=AsyncMock(return_value="admin-uuid"),
    ):
        decision = await handler.handle(
            event_name="Task.Created",
            envelope={"AccountId": 1, "TaskId": 9},
            config={"buildium_user_id": 42},
            company_id="company-1",
        )
    assert decision.action_type == "assign_to_user"
    assert decision.assigned_buildium_user_id == 42
    assert decision.assigned_helixis_user_id == "admin-uuid"
    assert decision.surface == "inbox"
    assert "buildium_user_id=42" in decision.reason


@pytest.mark.asyncio
async def test_assign_to_user_with_missing_config_falls_back_to_admin() -> None:
    """Half-configured row (empty config dict) must NOT crash; the
    handler returns an admin-fallback decision and logs the reason."""
    handler = AssignToUserHandler()
    with patch(
        "helixis.webhooks.actions.assign_to_user.resolve_admin_user_id",
        new=AsyncMock(return_value="admin-uuid"),
    ):
        decision = await handler.handle(
            event_name="Task.Created",
            envelope={"AccountId": 1, "TaskId": 9},
            config={},  # missing buildium_user_id
            company_id="company-1",
        )
    assert decision.assigned_buildium_user_id is None
    assert decision.assigned_helixis_user_id == "admin-uuid"
    assert "missing buildium_user_id" in decision.reason


# ─────────────────────────────────────────────────────────────────────
# route_event — the full pipeline with admin fallback
# ─────────────────────────────────────────────────────────────────────


def _pool_with_assignment(assignment_row: dict[str, Any] | None) -> MagicMock:
    """Build a fake asyncpg pool that returns ``assignment_row`` from
    ``fetchrow``. Used to exercise the routing pipeline without
    pulling in a real database."""
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=assignment_row)
    return pool


@pytest.mark.asyncio
async def test_route_event_uses_property_specific_assignment_when_present() -> None:
    """Property-specific row beats the default row."""
    row = {
        "id": "assignment-1",
        "company_id": "company-1",
        "buildium_property_id": 100,
        "category": "maintenance",
        "action_type": "assign_to_user",
        "action_config": '{"buildium_user_id": 42}',  # asyncpg jsonb-as-str
    }
    pool = _pool_with_assignment(row)
    with patch(
        "helixis.webhooks.actions.assign_to_user.resolve_admin_user_id",
        new=AsyncMock(return_value="admin-uuid"),
    ):
        decision = await route_event(
            pool,
            event_name="WorkOrder.Created",
            envelope={"AccountId": 1, "WorkOrderId": 9, "PropertyId": 100},
            company_id="company-1",
        )
    assert decision.assigned_buildium_user_id == 42
    assert decision.action_type == "assign_to_user"


@pytest.mark.asyncio
async def test_route_event_falls_back_to_default_row_when_property_unassigned() -> None:
    """No property-specific row exists for this property × category,
    but the company default row does — that wins."""
    default_row = {
        "id": "assignment-default",
        "company_id": "company-1",
        "buildium_property_id": None,
        "category": "maintenance",
        "action_type": "assign_to_user",
        "action_config": {"buildium_user_id": 7},
    }
    pool = MagicMock()
    # First fetchrow (property-specific): None. Second (default): row.
    pool.fetchrow = AsyncMock(side_effect=[None, default_row])
    with patch(
        "helixis.webhooks.actions.assign_to_user.resolve_admin_user_id",
        new=AsyncMock(return_value="admin-uuid"),
    ):
        decision = await route_event(
            pool,
            event_name="WorkOrder.Created",
            envelope={"AccountId": 1, "WorkOrderId": 9, "PropertyId": 999},
            company_id="company-1",
        )
    assert decision.assigned_buildium_user_id == 7


@pytest.mark.asyncio
async def test_route_event_falls_back_to_admin_when_no_assignment_anywhere() -> None:
    """Neither row exists → every event still lands in the admin's
    Inbox. The reason field tells ops which step bailed."""
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    with patch(
        "helixis.webhooks.routing.resolve_admin_user_id",
        new=AsyncMock(return_value="admin-uuid"),
    ):
        decision = await route_event(
            pool,
            event_name="WorkOrder.Created",
            envelope={"AccountId": 1, "WorkOrderId": 9, "PropertyId": 100},
            company_id="company-1",
        )
    assert decision.assigned_helixis_user_id == "admin-uuid"
    assert decision.assigned_buildium_user_id is None
    assert "no property_assignment" in decision.reason


@pytest.mark.asyncio
async def test_route_event_falls_back_to_admin_when_action_type_has_no_handler() -> None:
    """Forward-compat: a DB row with ``action_type='automate'`` (a
    type PR #80 doesn't ship a handler for) must NOT crash. The
    router logs a warning + admin-fallbacks."""
    row = {
        "id": "future-row",
        "company_id": "company-1",
        "buildium_property_id": 100,
        "category": "maintenance",
        "action_type": "automate",  # future
        "action_config": {"automation_id": "x"},
    }
    pool = _pool_with_assignment(row)
    with patch(
        "helixis.webhooks.routing.resolve_admin_user_id",
        new=AsyncMock(return_value="admin-uuid"),
    ):
        decision = await route_event(
            pool,
            event_name="WorkOrder.Created",
            envelope={"AccountId": 1, "WorkOrderId": 9, "PropertyId": 100},
            company_id="company-1",
        )
    assert decision.assigned_helixis_user_id == "admin-uuid"
    assert "no handler for action_type=automate" in decision.reason


@pytest.mark.asyncio
async def test_route_event_uses_default_row_for_property_less_events() -> None:
    """Vendor created → no PropertyId on envelope → resolver returns
    None → router skips the property-specific lookup and goes
    straight to the default row."""
    default_row = {
        "id": "default-vendor-row",
        "company_id": "company-1",
        "buildium_property_id": None,
        "category": "other",
        "action_type": "assign_to_user",
        "action_config": {"buildium_user_id": 11},
    }
    pool = _pool_with_assignment(default_row)
    with patch(
        "helixis.webhooks.actions.assign_to_user.resolve_admin_user_id",
        new=AsyncMock(return_value="admin-uuid"),
    ):
        decision = await route_event(
            pool,
            event_name="Vendor.Created",
            envelope={"AccountId": 1, "VendorId": 99},
            company_id="company-1",
        )
    # Only ONE fetchrow call — vendor has no PropertyId so the
    # property-specific lookup is skipped entirely.
    assert pool.fetchrow.await_count == 1
    assert decision.assigned_buildium_user_id == 11


# ─────────────────────────────────────────────────────────────────────
# Bug 1 — task/work-order title resolution
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_created_resolves_title_no_id_in_description() -> None:
    """The Activity row description must use the resolved task title
    and NOT contain the raw Buildium task id (PR #75 HARD PRESENTATION
    RULES). Pre-PR-#80 wrote ``task #17434``; PR #80 writes
    ``a task: Send lease to Ryan``."""
    from helixis.webhooks.handlers import maintenance

    captured: dict[str, Any] = {}

    async def _capture_activity(_pool: Any, entry: Any) -> None:
        captured["entry"] = entry

    task = MagicMock()
    task.title = "Send lease to Ryan"
    fake_api = MagicMock()
    fake_api.tasks = MagicMock()
    fake_api.tasks.get = AsyncMock(return_value=task)

    class _Ctx:
        async def __aenter__(self) -> Any:
            return fake_api

        async def __aexit__(self, *_exc: Any) -> None:
            return None

    with (
        patch.object(maintenance, "get_pool", return_value=MagicMock()),
        patch.object(maintenance, "write_activity", new=_capture_activity),
        patch.object(maintenance, "open_api_for_company", return_value=_Ctx()),
        # 0041: the handler also mirrors the refetched task; the MagicMock
        # payload can't be serialized, so stub the mirror write here.
        patch.object(maintenance.entities_repo, "upsert_task", new=AsyncMock()),
    ):
        await maintenance.handle_task_changed(
            "company-1",
            {"EventName": "Task.Created", "AccountId": 1, "TaskId": 17434},
        )

    entry = captured["entry"]
    assert "Send lease to Ryan" in entry.description
    assert "17434" not in entry.description
    assert "#" not in entry.description
    # The raw payload still carries the id (audit trail) — that's fine.
    assert entry.raw["task_id"] == 17434
    # And the title is stashed for downstream Inbox renderers.
    assert entry.raw["task_title"] == "Send lease to Ryan"


@pytest.mark.asyncio
async def test_task_created_falls_back_to_generic_when_fetch_fails() -> None:
    """If the Buildium refetch 404s or the task title is empty, the
    Activity description falls back to a generic phrase — NEVER
    leaks the id as a substitute label."""
    from helixis.buildium.errors import BuildiumApiError
    from helixis.webhooks.handlers import maintenance

    captured: dict[str, Any] = {}

    async def _capture_activity(_pool: Any, entry: Any) -> None:
        captured["entry"] = entry

    fake_api = MagicMock()
    fake_api.tasks = MagicMock()
    fake_api.tasks.get = AsyncMock(side_effect=BuildiumApiError(404, "not found", path="/tasks/x"))

    class _Ctx:
        async def __aenter__(self) -> Any:
            return fake_api

        async def __aexit__(self, *_exc: Any) -> None:
            return None

    with (
        patch.object(maintenance, "get_pool", return_value=MagicMock()),
        patch.object(maintenance, "write_activity", new=_capture_activity),
        patch.object(maintenance, "open_api_for_company", return_value=_Ctx()),
        # 0041: the handler also mirrors the refetched task; the MagicMock
        # payload can't be serialized, so stub the mirror write here.
        patch.object(maintenance.entities_repo, "upsert_task", new=AsyncMock()),
    ):
        await maintenance.handle_task_changed(
            "company-1",
            {"EventName": "Task.Created", "AccountId": 1, "TaskId": 17434},
        )
    entry = captured["entry"]
    assert "Buildium" in entry.description
    assert "task" in entry.description.lower()
    assert "17434" not in entry.description
    assert "#" not in entry.description


@pytest.mark.asyncio
async def test_workorder_created_resolves_title_no_id_in_description() -> None:
    """Work orders get the same treatment — title in, id out."""
    from helixis.webhooks.handlers import maintenance

    captured: dict[str, Any] = {}

    async def _capture_activity(_pool: Any, entry: Any) -> None:
        captured["entry"] = entry

    wo = MagicMock()
    wo.id = 2916
    wo.title = "Leaky faucet in 4B"
    wo.priority = "Normal"
    wo.status = "New"
    wo.property_id = 100
    fake_api = MagicMock()
    fake_api.work_orders = MagicMock()
    fake_api.work_orders.get = AsyncMock(return_value=wo)

    class _Ctx:
        async def __aenter__(self) -> Any:
            return fake_api

        async def __aexit__(self, *_exc: Any) -> None:
            return None

    with (
        patch.object(maintenance, "get_pool", return_value=MagicMock()),
        patch.object(maintenance, "write_activity", new=_capture_activity),
        patch.object(maintenance, "upsert_work_order", new=AsyncMock()),
        patch.object(maintenance, "open_api_for_company", return_value=_Ctx()),
    ):
        await maintenance.handle_workorder_changed(
            "company-1",
            {"EventName": "WorkOrder.Created", "AccountId": 1, "WorkOrderId": 2916},
        )
    entry = captured["entry"]
    assert "Leaky faucet in 4B" in entry.description
    assert "2916" not in entry.description
    assert "work order #" not in entry.description.lower()
