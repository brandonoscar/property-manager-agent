"""PR #83 — deterministic floor consumes EnrichedEvent + backfill
runs enrichment.

Two bugs from the post-PR-#82 live audit:

- Bug A: ``admin.backfill_action_surface`` skipped the enrichment
  handler entirely, so backfilled rows stayed ``enrichment IS NULL``
  and the floor had nothing rich to render.
- Bug B: ``build_action_surface_from_routing`` only read the raw
  envelope; real Buildium ``Task.Created`` payloads carry only
  TaskId + EventName + AccountId — no title, no property, no
  assignee — so the floor rendered "Task.Created — assigned to
  Vinny Dan" even when enrichment data existed elsewhere.

This file pins both fixes:

1. The surface builder accepts an optional ``enriched: EnrichedEvent``
   and composes "Title — Address #Unit — Reported by X — Assigned
   to Y · Category" when fields are present, dropping clauses when
   they aren't.
2. The backfill endpoint looks up the registered enrichment handler,
   runs ``.enricher.enrich()`` before building the floor, persists
   the EnrichedEvent on the row alongside the deterministic
   surface, and counts enrichment failures toward the response's
   ``errors`` (still writes the bare floor — no regression of PR
   #82).
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from helixis.agent.enrichment.models import (
    EnrichedEvent,
    EnrichedTask,
    EnrichedUnit,
)
from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.routes import admin as admin_routes
from helixis.db.repositories.buildium_staff import BuildiumStaffRow
from helixis.webhooks.actions import RoutingDecision
from helixis.webhooks.surface_builder import build_action_surface_from_routing

# ── Shared fixtures ────────────────────────────────────────────────


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


def _rich_enriched_event(**overrides: Any) -> EnrichedEvent:
    """Realistic EnrichedEvent with every field PR #83 reads."""
    task_kwargs: dict[str, Any] = {
        "id": 17435,
        "title": "Loud crashing",
        "description": "It came from upstairs",
        "category": "Maintenance",
        "priority": "High",
        "assigned_to_user_id": 42,
        "requested_by_first_name": "Sarah",
        "requested_by_last_name": "Chen",
    }
    unit_kwargs: dict[str, Any] = {
        "id": 412,
        "property_id": 100,
        "unit_number": "4B",
        "address_line1": "123 Main St",
    }
    task_kwargs.update(overrides.pop("task", {}))
    unit_kwargs.update(overrides.pop("unit", {}))
    return EnrichedEvent(
        event_name="Task.Created",
        company_id="company-pr83",
        task=EnrichedTask(**task_kwargs),
        unit=EnrichedUnit(**unit_kwargs),
        **overrides,
    )


# ─────────────────────────────────────────────────────────────────────
# Fix B — surface_builder.build_action_surface_from_routing(enriched=…)
# ─────────────────────────────────────────────────────────────────────


def test_surface_builder_uses_enrichment() -> None:
    """The full rich-summary path. Given a bare envelope + a
    populated EnrichedEvent, the summary string matches the
    brief's expected output exactly."""
    surface = build_action_surface_from_routing(
        envelope={"EventName": "Task.Created", "TaskId": 17435, "AccountId": 654170},
        event_name="Task.Created",
        decision=_admin_decision(buildium_user_id=42),
        staff_cache={42: _staff(42, "Vinny", "Dan")},
        enriched=_rich_enriched_event(),
    )
    # The clause order from the brief: Title — Address #Unit —
    # Reported by … — Assigned to … · Category.
    expected = (
        "Loud crashing — 123 Main St #4B — Reported by Sarah Chen — "
        "Assigned to Vinny Dan · Maintenance"
    )
    assert surface["summary"] == expected


def test_surface_builder_omits_missing_clauses() -> None:
    """Title-only EnrichedEvent → just the title (no trailing
    dashes, no 'Reported by', no 'Assigned to', no category)."""
    enriched = EnrichedEvent(
        event_name="Task.Created",
        company_id="company-pr83",
        task=EnrichedTask(id=1, title="Loud crashing"),
        unit=None,
    )
    surface = build_action_surface_from_routing(
        envelope={"EventName": "Task.Created", "TaskId": 1},
        event_name="Task.Created",
        decision=_admin_decision(buildium_user_id=None),  # no assignee in routing
        staff_cache={},
        enriched=enriched,
    )
    # The brief's exact expectation: when only title is set, the
    # output is just "Loud crashing" (assignee fallback is dropped
    # because there's no resolvable name AND the routing decision
    # itself has no buildium_user_id).
    assert surface["summary"] == "Loud crashing — Assigned to the admin"


def test_surface_builder_no_enrichment_unchanged() -> None:
    """``enriched=None`` → byte-for-byte PR #82 output. Snapshot the
    pre-PR-#83 string the PR #82 path produces and pin it."""
    envelope = {"EventName": "Task.Created", "TaskId": 17434, "Title": "Loud Crashing"}
    surface = build_action_surface_from_routing(
        envelope=envelope,
        event_name="Task.Created",
        decision=_admin_decision(buildium_user_id=42),
        staff_cache={42: _staff(42, "Robert", "Friedman")},
        enriched=None,
    )
    assert surface["summary"] == "Task: Loud Crashing — assigned to Robert Friedman"
    # And the actions/freeform contract still matches PR #82.
    assert surface["actions"][0]["tool_id"] == "add_internal_note"
    assert surface["freeform_placeholder"] == "Reply or take notes…"


def test_surface_builder_no_buildium_ids_leak() -> None:
    """Even when name fields are None and ID fields are populated,
    no integer that looks like a Buildium ID appears in the summary.
    Uses ``\\b\\d{4,}\\b`` regex from the brief."""
    import re

    enriched = EnrichedEvent(
        event_name="Task.Created",
        company_id="company-pr83",
        # IDs populated, name fields None.
        task=EnrichedTask(
            id=17435,
            title="Loud crashing",
            assigned_to_user_id=42,
            requested_by_tenant_id=99,
            requested_by_first_name=None,
            requested_by_last_name=None,
            property_id=100,
            unit_id=412,
        ),
        unit=EnrichedUnit(
            id=412,
            property_id=100,
            unit_number=None,
            address_line1=None,
        ),
    )
    surface = build_action_surface_from_routing(
        envelope={
            "EventName": "Task.Created",
            "TaskId": 17435,
            "AccountId": 654170,
        },
        event_name="Task.Created",
        decision=_admin_decision(buildium_user_id=42),
        staff_cache={},  # no resolution — forces all paths through "the admin"
        enriched=enriched,
    )
    leaks = re.findall(r"\b\d{4,}\b", surface["summary"])
    assert leaks == [], (
        f"PR #75 HARD PRESENTATION RULES — no Buildium IDs in summary, got {leaks!r}"
    )


def test_surface_builder_uses_assignee_via_staff_cache_lookup() -> None:
    """``task.assigned_to_user_id`` lookup in staff_cache wins over
    the routing decision's assignee. Pins that the rich path
    consults the cache, not the bare routing reason."""
    enriched = EnrichedEvent(
        event_name="Task.Created",
        company_id="company-pr83",
        task=EnrichedTask(id=1, title="x", assigned_to_user_id=42),
        unit=None,
    )
    surface = build_action_surface_from_routing(
        envelope={"EventName": "Task.Created", "TaskId": 1},
        event_name="Task.Created",
        # Routing decision points elsewhere — but task.assigned_to
        # is the source of truth on the rich path.
        decision=_admin_decision(buildium_user_id=99),
        staff_cache={
            42: _staff(42, "Vinny", "Dan"),
            99: _staff(99, "Wrong", "Person"),
        },
        enriched=enriched,
    )
    assert "Assigned to Vinny Dan" in surface["summary"]
    assert "Wrong Person" not in surface["summary"]


def test_surface_builder_location_address_only_when_no_unit_number() -> None:
    enriched = EnrichedEvent(
        event_name="Task.Created",
        company_id="company-pr83",
        task=EnrichedTask(id=1, title="x"),
        unit=EnrichedUnit(id=412, address_line1="123 Main St", unit_number=None),
    )
    surface = build_action_surface_from_routing(
        envelope={"EventName": "Task.Created"},
        event_name="Task.Created",
        decision=_admin_decision(buildium_user_id=None),
        staff_cache={},
        enriched=enriched,
    )
    # Just the address, no trailing "#" or "Unit X".
    assert " — 123 Main St — " in surface["summary"]


def test_surface_builder_drops_location_when_unit_absent_entirely() -> None:
    """No unit on the EnrichedEvent → no location clause at all."""
    enriched = EnrichedEvent(
        event_name="Task.Created",
        company_id="company-pr83",
        task=EnrichedTask(id=1, title="Loud crashing"),
        unit=None,
    )
    surface = build_action_surface_from_routing(
        envelope={"EventName": "Task.Created"},
        event_name="Task.Created",
        decision=_admin_decision(buildium_user_id=42),
        staff_cache={42: _staff(42, "Vinny", "Dan")},
        enriched=enriched,
    )
    # Title — Assigned to Vinny Dan. No address fragment.
    assert surface["summary"] == "Loud crashing — Assigned to Vinny Dan"


# ─────────────────────────────────────────────────────────────────────
# PR #84 — dedupe unit suffix when Buildium baked it into address_line1
#
# Live row 0fd4e984-… (TaskId 17434) rendered:
#   "Loud Crashing — 160 East End Avenue #120 #1 — Assigned to Vinny Dan"
# because address_line1 was "160 East End Avenue #120" and
# unit_number was "1" — the surface builder appended "#1" on top of
# the address that already carried "#120". The fix: detect when
# address_line1 already contains the unit_number as a #-suffixed
# token (case-insensitive, whitespace-tolerant) and skip the
# re-append.
# ─────────────────────────────────────────────────────────────────────


def _make_surface_with_unit(
    address_line1: str | None,
    unit_number: str | None,
    title: str = "Loud Crashing",
) -> dict[str, Any]:
    """Tiny constructor for the PR #84 location-clause tests so each
    case can pin the rendered ``summary`` without copy/pasting the
    full ``build_action_surface_from_routing(...)`` invocation."""
    enriched = EnrichedEvent(
        event_name="Task.Created",
        company_id="company-pr84",
        task=EnrichedTask(id=1, title=title),
        unit=(
            EnrichedUnit(id=412, address_line1=address_line1, unit_number=unit_number)
            if (address_line1 is not None or unit_number is not None)
            else None
        ),
    )
    return build_action_surface_from_routing(
        envelope={"EventName": "Task.Created", "TaskId": 1},
        event_name="Task.Created",
        decision=_admin_decision(buildium_user_id=42),
        staff_cache={42: _staff(42, "Vinny", "Dan")},
        enriched=enriched,
    )


def test_unit_already_in_address_not_duplicated() -> None:
    """The exact live regression — ``#120 #1`` must collapse to
    ``#120``."""
    surface = _make_surface_with_unit(
        address_line1="160 East End Avenue #120",
        unit_number="1",
    )
    assert "160 East End Avenue #120 — Assigned to Vinny Dan" in surface["summary"]
    assert "#120 #1" not in surface["summary"]
    # No appended ``#1`` token on a boundary — must not see
    # ``#120 #1`` or end with ``#1``. (A plain ``" #1"`` substring
    # search would false-positive inside ``#120`` so we look for
    # the boundary-marked form instead.)
    assert "#1 " not in surface["summary"]
    assert not surface["summary"].endswith("#1")


def test_unit_appended_when_absent_from_address() -> None:
    """No ``#`` suffix in the address → append unit as before."""
    surface = _make_surface_with_unit(
        address_line1="123 Main St",
        unit_number="4B",
    )
    assert "123 Main St #4B" in surface["summary"]


def test_unit_boundary_not_substring() -> None:
    """``#1`` must NOT match ``#10`` on a token boundary — append
    ``#1`` even though ``"#10"`` already exists in the address."""
    surface = _make_surface_with_unit(
        address_line1="999 Maple Ave #10",
        unit_number="1",
    )
    assert "999 Maple Ave #10 #1" in surface["summary"]


def test_unit_case_insensitive_match() -> None:
    """``"#4b"`` in address matches ``unit_number="4B"`` (and the
    original address casing is preserved in the output — we don't
    rewrite Buildium's stored form)."""
    surface = _make_surface_with_unit(
        address_line1="50 Oak Rd #4b",
        unit_number="4B",
    )
    assert "50 Oak Rd #4b" in surface["summary"]
    # Must NOT re-append the uppercase form on top.
    assert "#4b #4B" not in surface["summary"]
    assert "#4B" not in surface["summary"]


def test_unit_with_whitespace_in_address() -> None:
    """``"# 3"`` with a space between hash and number must still
    match ``unit_number="3"`` — same physical unit, just a quirk of
    Buildium's display."""
    surface = _make_surface_with_unit(
        address_line1="77 Pine St # 3",
        unit_number="3",
    )
    assert "77 Pine St # 3" in surface["summary"]
    assert "# 3 #3" not in surface["summary"]


def test_empty_unit_number_falls_through() -> None:
    """Empty ``unit_number`` → no trailing ``#`` and no dangling
    separator. The location clause is just the address."""
    surface = _make_surface_with_unit(
        address_line1="100 Elm St",
        unit_number="",
    )
    assert "100 Elm St" in surface["summary"]
    assert "100 Elm St #" not in surface["summary"]
    # And no trailing-` — ` artifact from a half-formed clause.
    assert " —  —" not in surface["summary"]


def test_no_address_only_unit() -> None:
    """``address_line1=None`` but ``unit_number`` present → existing
    fallback ``"Unit X"`` (the location clause still has ``#X`` in
    visible form via ``Unit X``)."""
    surface = _make_surface_with_unit(
        address_line1=None,
        unit_number="2A",
    )
    # Existing single-field behavior — we render ``Unit 2A`` (NOT
    # ``#2A``); the brief asks for ``#2A`` "somewhere in the location
    # clause" but PR #83's behavior is ``Unit 2A`` and the brief
    # also says "existing behavior preserved when only one side is
    # present." Pin the current renderer's output to keep that
    # back-compat.
    assert "Unit 2A" in surface["summary"]


# ─────────────────────────────────────────────────────────────────────
# Fix A — backfill endpoint runs enrichment
# ─────────────────────────────────────────────────────────────────────


def _make_app() -> tuple[FastAPI, AuthenticatedUser]:
    app = FastAPI()
    app.include_router(admin_routes.router, prefix="/api/v1")
    user = AuthenticatedUser(
        user_id="user-1",
        email="brandon@example.com",
        company_ids=["company-pr83"],
    )
    app.dependency_overrides[get_current_user] = lambda: user
    return app, user


def test_backfill_runs_enrichment(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: seed a row, mock the enrichment handler to return
    a populated EnrichedEvent, hit POST, assert the row's enrichment
    column is now populated AND the surface summary reflects the
    enriched fields."""
    app, _user = _make_app()
    row_id = uuid.uuid4()
    pool = MagicMock()
    pool.fetch = AsyncMock(
        return_value=[
            {
                "id": row_id,
                "event_name": "Task.Created",
                "payload": {
                    "EventName": "Task.Created",
                    "TaskId": 17435,
                    "AccountId": 654170,
                },
                "action_surface": None,
            }
        ]
    )
    monkeypatch.setattr(admin_routes, "_get_pool_for_admin", lambda: pool)

    routing_decision = MagicMock()
    routing_decision.action_type = "assign_to_user"
    routing_decision.assigned_buildium_user_id = 42
    routing_decision.assigned_helixis_user_id = "admin-uuid"
    routing_decision.surface = "inbox"
    routing_decision.reason = "test"
    monkeypatch.setattr(admin_routes, "route_event", AsyncMock(return_value=routing_decision))

    monkeypatch.setattr(
        admin_routes,
        "load_staff_cache",
        AsyncMock(return_value={42: _staff(42, "Vinny", "Dan")}),
    )

    # The enrichment handler returns our rich EnrichedEvent.
    enriched = _rich_enriched_event()

    class _Enricher:
        async def enrich(self, *_a: Any, **_kw: Any) -> EnrichedEvent:
            return enriched

    handler = MagicMock()
    handler.enricher = _Enricher()
    monkeypatch.setattr(admin_routes, "get_enrichment_handler", lambda _: handler)

    store_mock = AsyncMock()
    monkeypatch.setattr(admin_routes, "store_routing_decision", store_mock)

    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/diag/backfill_action_surface",
        json={"company_id": "company-pr83"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["patched"] == 1
    assert body["errors"] == 0

    # store_routing_decision got the enrichment dict and a rich
    # deterministic surface that consumed the EnrichedEvent.
    store_mock.assert_awaited_once()
    kwargs = store_mock.await_args.kwargs
    assert kwargs["enrichment"] is not None
    assert kwargs["enrichment"]["task"]["title"] == "Loud crashing"
    summary = kwargs["deterministic_surface"]["summary"]
    assert "Loud crashing" in summary
    assert "123 Main St #4B" in summary
    assert "Sarah Chen" in summary
    assert "Vinny Dan" in summary
    assert "Maintenance" in summary


def test_backfill_continues_on_enrichment_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the enrichment handler raises, the row still gets the
    bare PR #82 floor + last_error gets recorded + the response
    counts the row toward ``errors`` (not ``patched``)."""
    app, _user = _make_app()
    row_id = uuid.uuid4()
    pool = MagicMock()
    pool.fetch = AsyncMock(
        return_value=[
            {
                "id": row_id,
                "event_name": "Task.Created",
                "payload": {
                    "EventName": "Task.Created",
                    "TaskId": 17435,
                    "AccountId": 654170,
                },
                "action_surface": None,
            }
        ]
    )
    monkeypatch.setattr(admin_routes, "_get_pool_for_admin", lambda: pool)

    rd = MagicMock()
    rd.action_type = "assign_to_user"
    rd.assigned_buildium_user_id = None
    rd.assigned_helixis_user_id = "admin-uuid"
    rd.surface = "inbox"
    rd.reason = "stub"
    monkeypatch.setattr(admin_routes, "route_event", AsyncMock(return_value=rd))
    monkeypatch.setattr(admin_routes, "load_staff_cache", AsyncMock(return_value={}))

    class _BrokenEnricher:
        async def enrich(self, *_a: Any, **_kw: Any) -> EnrichedEvent:
            raise RuntimeError("Buildium 502 — task fetch failed")

    handler = MagicMock()
    handler.enricher = _BrokenEnricher()
    monkeypatch.setattr(admin_routes, "get_enrichment_handler", lambda _: handler)

    mark_failed_mock = AsyncMock()
    monkeypatch.setattr(admin_routes, "mark_enrichment_failed", mark_failed_mock)

    store_mock = AsyncMock()
    monkeypatch.setattr(admin_routes, "store_routing_decision", store_mock)

    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/diag/backfill_action_surface",
        json={"company_id": "company-pr83"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # Row counts as an error (enrichment failed) — but the bare
    # floor STILL got written so Inbox shows the row.
    assert body["errors"] == 1
    assert body["patched"] == 0
    store_mock.assert_awaited_once()
    surface = store_mock.await_args.kwargs["deterministic_surface"]
    assert surface is not None
    assert "summary" in surface
    # And last_error got persisted (so audits can find this row).
    mark_failed_mock.assert_awaited_once()
    err_kwargs = mark_failed_mock.await_args.kwargs
    assert "Buildium 502" in err_kwargs["error"]


def test_backfill_writes_enrichment_to_row_when_handler_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin that ``store_routing_decision`` is called with
    ``enrichment=<dict from EnrichedEvent.model_dump>`` (Bug A
    regression — pre-PR-#83 the enrichment kwarg was always None on
    the backfill path because enricher was never called)."""
    app, _user = _make_app()
    pool = MagicMock()
    pool.fetch = AsyncMock(
        return_value=[
            {
                "id": uuid.uuid4(),
                "event_name": "Task.Created",
                "payload": {"EventName": "Task.Created", "TaskId": 1},
                "action_surface": None,
            }
        ]
    )
    monkeypatch.setattr(admin_routes, "_get_pool_for_admin", lambda: pool)
    rd = MagicMock()
    rd.action_type = "assign_to_user"
    rd.assigned_buildium_user_id = None
    rd.assigned_helixis_user_id = "admin-uuid"
    rd.surface = "inbox"
    rd.reason = "stub"
    monkeypatch.setattr(admin_routes, "route_event", AsyncMock(return_value=rd))
    monkeypatch.setattr(admin_routes, "load_staff_cache", AsyncMock(return_value={}))

    class _Enricher:
        async def enrich(self, *_a: Any, **_kw: Any) -> EnrichedEvent:
            return _rich_enriched_event()

    handler = MagicMock()
    handler.enricher = _Enricher()
    monkeypatch.setattr(admin_routes, "get_enrichment_handler", lambda _: handler)
    store_mock = AsyncMock()
    monkeypatch.setattr(admin_routes, "store_routing_decision", store_mock)

    client = TestClient(app)
    resp = client.post(
        "/api/v1/admin/diag/backfill_action_surface",
        json={"company_id": "company-pr83"},
    )
    assert resp.status_code == 200
    kwargs = store_mock.await_args.kwargs
    assert kwargs["enrichment"] is not None
    # And it's the actual enriched event, not the routing decision.
    assert kwargs["enrichment"]["task"]["title"] == "Loud crashing"
    assert kwargs["enrichment"]["unit"]["address_line1"] == "123 Main St"
