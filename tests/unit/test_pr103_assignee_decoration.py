"""PR #103 — Inbox detail pane: assignee resolution + r.keys() sweep.

Backend helpers under test:
  - ``_fetch_staff_name_map(pool, company_id)`` reads
    ``buildium_staff`` and returns ``{buildium_user_id: display_name}``.
    Tolerates the table being absent (returns empty map) and skips
    rows whose first+last name compose to an empty string.
  - ``_decorate_enrichment_assignee(enrichment, staff_map)`` injects
    ``task.assignee_name`` when ``assigned_to_user_id`` resolves
    through the staff map. No-op on missing enrichment / missing
    task / missing id / id-not-in-map.

Why this lives in its own test file: the route's existing test file
(``test_pr96_recent_events_serializer.py``) mocks ``pool.fetch`` at
a level that doesn't distinguish the new staff-cache query from the
events query. Keeping PR #103's coverage standalone makes the
contract for the new helpers explicit and lets a future planner
PR build on the staff-cache fetch pattern without re-discovering
the shape.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from helixis.api.routes.buildium import (
    _decorate_enrichment_assignee,
    _fetch_staff_name_map,
)


def _make_pool_for_staff(rows: list[dict[str, Any]] | Exception) -> MagicMock:
    """Build a mock pool whose ``fetch`` returns ``rows`` (or raises
    when ``rows`` is an Exception instance)."""
    pool = MagicMock()
    if isinstance(rows, Exception):
        pool.fetch = AsyncMock(side_effect=rows)
    else:
        pool.fetch = AsyncMock(return_value=rows)
    return pool


# ── _fetch_staff_name_map ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_staff_name_map_composes_first_plus_last() -> None:
    pool = _make_pool_for_staff(
        [
            {"buildium_user_id": 70287, "first_name": "Vinny", "last_name": "Dan"},
            {"buildium_user_id": 70288, "first_name": "Sarah", "last_name": "Chen"},
        ]
    )
    out = await _fetch_staff_name_map(pool, "company-xyz")
    assert out == {70287: "Vinny Dan", 70288: "Sarah Chen"}


@pytest.mark.asyncio
async def test_fetch_staff_name_map_trims_whitespace_and_skips_empties() -> None:
    """A row whose first+last collapses to empty must NOT inject a
    bogus blank-name entry (would render as an empty Assignee cell
    rather than '—' on the frontend)."""
    pool = _make_pool_for_staff(
        [
            {"buildium_user_id": 1, "first_name": "  ", "last_name": "  "},
            {"buildium_user_id": 2, "first_name": "Jimmy", "last_name": None},
            {"buildium_user_id": 3, "first_name": None, "last_name": "Doe"},
        ]
    )
    out = await _fetch_staff_name_map(pool, "company-xyz")
    # 1 was empty after trim → excluded.
    # 2 is "Jimmy" alone (last_name None).
    # 3 is "Doe" alone (first_name None).
    assert out == {2: "Jimmy", 3: "Doe"}


@pytest.mark.asyncio
async def test_fetch_staff_name_map_returns_empty_when_table_missing() -> None:
    """Pre-migration / fresh-tenant DB: ``buildium_staff`` doesn't
    exist yet. Route must not 500 — the Inbox should still render
    with Assignee falling back to '—'."""
    pool = _make_pool_for_staff(asyncpg.exceptions.UndefinedTableError("relation does not exist"))
    out = await _fetch_staff_name_map(pool, "company-xyz")
    assert out == {}


@pytest.mark.asyncio
async def test_fetch_staff_name_map_returns_empty_on_column_drift() -> None:
    """Same defensive shape for ``UndefinedColumnError`` — the helper
    is downstream of the Inbox hot path; degrade, don't 500."""
    pool = _make_pool_for_staff(asyncpg.exceptions.UndefinedColumnError("column does not exist"))
    out = await _fetch_staff_name_map(pool, "company-xyz")
    assert out == {}


# ── _decorate_enrichment_assignee ─────────────────────────────────


def test_decorate_assignee_injects_name_when_id_resolves() -> None:
    enrichment = {
        "task": {
            "id": 17435,
            "assigned_to_user_id": 70287,
        },
        "unit": {"unit_number": "4B"},
    }
    staff_map = {70287: "Vinny Dan"}
    out = _decorate_enrichment_assignee(enrichment, staff_map)
    assert out is enrichment  # mutates in place
    assert enrichment["task"]["assignee_name"] == "Vinny Dan"
    # Other enrichment fields untouched.
    assert enrichment["unit"] == {"unit_number": "4B"}


def test_decorate_assignee_no_op_when_enrichment_none() -> None:
    assert _decorate_enrichment_assignee(None, {70287: "Vinny Dan"}) is None


def test_decorate_assignee_no_op_when_task_missing() -> None:
    enrichment = {"unit": {"unit_number": "4B"}}
    out = _decorate_enrichment_assignee(enrichment, {70287: "Vinny Dan"})
    assert out is enrichment
    assert "task" not in enrichment


def test_decorate_assignee_no_op_when_assigned_to_user_id_null() -> None:
    enrichment = {"task": {"id": 1, "assigned_to_user_id": None}}
    out = _decorate_enrichment_assignee(enrichment, {70287: "Vinny Dan"})
    assert out is enrichment
    assert "assignee_name" not in enrichment["task"]


def test_decorate_assignee_no_op_when_id_not_in_staff_map() -> None:
    """User exists on the task but the staff cache hasn't synced
    that user yet (or they were removed from Buildium). The
    frontend's '—' fallback kicks in."""
    enrichment = {"task": {"id": 1, "assigned_to_user_id": 99999}}
    out = _decorate_enrichment_assignee(enrichment, {70287: "Vinny Dan"})
    assert out is enrichment
    assert "assignee_name" not in enrichment["task"]


def test_decorate_assignee_handles_string_id_gracefully() -> None:
    """JSONB roundtrip sometimes turns numeric IDs into strings
    depending on driver and ORM layer. The helper coerces to int
    and resolves anyway rather than silently failing."""
    enrichment = {"task": {"id": 1, "assigned_to_user_id": "70287"}}
    out = _decorate_enrichment_assignee(enrichment, {70287: "Vinny Dan"})
    assert out is enrichment
    assert enrichment["task"]["assignee_name"] == "Vinny Dan"


def test_decorate_assignee_no_op_when_id_unparseable() -> None:
    """A non-numeric, non-coercible id (e.g. a UUID string from a
    schema change) must not raise — the route would 500 the whole
    inbox response if it did."""
    enrichment = {"task": {"id": 1, "assigned_to_user_id": "not-a-number"}}
    out = _decorate_enrichment_assignee(enrichment, {70287: "Vinny Dan"})
    assert out is enrichment
    assert "assignee_name" not in enrichment["task"]
