"""PR #107 — planner auto-replan when ``PLANNER_PROMPT_VERSION`` bumps.

Mirrors the pattern PR #99 / PR #100 established for the
summarizer: when the in-code prompt version constant bumps, the
worker re-plans rows whose persisted ``action_surface.prompt_version``
is stale. No migration is needed — the version lives inside the
``action_surface`` JSONB blob, not in a column.

The motivating bug: post-PR-#99 the V2 prompt forbids
"Re-categorize as Maintenance" / "Raise priority" busywork
suggestions, but rows planned BEFORE PR #99 deployed still
display those V1-shape suggestions because their persisted
``action_surface`` doesn't auto-refresh. Clicking Re-plan works
per-row but doesn't scale. With this PR, the worker drains the
backlog automatically on its next tick after the version bump.

Three contracts pinned here:

1. ``ActionSurface`` accepts an optional ``prompt_version: str | None``
   so the V1/V2 rows in prod (which have no such field) still
   deserialize.
2. ``ActionPlanner.plan()`` stamps ``PLANNER_PROMPT_VERSION`` on every
   surface it produces.
3. ``claim_planner_batch`` accepts ``current_prompt_version`` and
   adds an OR-clause that picks up succeeded rows whose
   ``action_surface->>'prompt_version'`` IS DISTINCT FROM the
   current version. The non-version path (no kwarg) is unchanged —
   back-compat for any callers that didn't migrate.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.agent.planner import PLANNER_PROMPT_VERSION
from helixis.agent.planner.models import ActionSurface

# ── ActionSurface accepts prompt_version ─────────────────────────


def test_action_surface_accepts_prompt_version_field() -> None:
    surface = ActionSurface(
        schema_version=2,
        summary="Sarah Chen at 123 Main St unit 4B reported a leak.",
        actions=[],
        freeform_placeholder="describe what you want…",
        prompt_version="v2",
    )
    assert surface.prompt_version == "v2"


def test_action_surface_prompt_version_is_optional() -> None:
    """V1 / V2-era rows in prod were written without this field. They
    must still deserialize cleanly — the worker treats missing as
    stale and re-plans on the next tick."""
    surface = ActionSurface(
        schema_version=2,
        summary="Sarah Chen at 123 Main St unit 4B reported a leak.",
        actions=[],
        freeform_placeholder="describe what you want…",
    )
    assert surface.prompt_version is None


def test_action_surface_round_trips_prompt_version_through_model_dump() -> None:
    """The persistence path serializes via ``model_dump`` → jsonb →
    asyncpg. The version must survive the round-trip."""
    surface = ActionSurface(
        schema_version=2,
        summary="Sarah Chen at 123 Main St unit 4B reported a leak.",
        actions=[],
        freeform_placeholder="describe what you want…",
        prompt_version="v2",
    )
    dumped = surface.model_dump(mode="json")
    assert dumped["prompt_version"] == "v2"


# ── PLANNER_PROMPT_VERSION constant ──────────────────────────────


def test_planner_prompt_version_is_current() -> None:
    """Pin the constant — bumping it triggers re-plan of all existing
    rows, so the value change should be deliberate and visible in
    code review."""
    assert PLANNER_PROMPT_VERSION == "v4"


# ── claim_planner_batch with current_prompt_version ──────────────


@pytest.mark.asyncio
async def test_claim_planner_batch_with_version_includes_stale_predicate() -> None:
    """When ``current_prompt_version`` is set, the claim SQL gets
    the same shape as the summarizer self-heal: OR-clause picking up
    succeeded rows with stale ``action_surface.prompt_version``."""
    from helixis.webhooks.queue import claim_planner_batch

    captured: dict[str, Any] = {}

    async def _fake_fetch(sql: str, *args: Any) -> list[Any]:
        captured["sql"] = sql
        captured["args"] = args
        return []

    pool = MagicMock()
    pool.fetch = _fake_fetch

    await claim_planner_batch(
        pool,
        batch_size=4,
        max_attempts=5,
        cooldown_seconds=30,
        current_prompt_version="v2",
    )

    sql = captured["sql"]
    # Both the normal pending path AND the stale-version path.
    # ⚠ Widened 2026-08-29 to reclaim rows abandoned mid-plan; see
    # test_planner_queue_retry.py for the 52-day stall this closes.
    assert "planner_status IN ('pending', 'in_progress', 'timed_out')" in sql
    assert "planner_status = 'succeeded'" in sql
    assert "action_surface->>'prompt_version'" in sql
    assert "IS DISTINCT FROM $4" in sql
    assert captured["args"] == (4, 5, 30, "v2")


@pytest.mark.asyncio
async def test_claim_planner_batch_without_version_uses_original_sql() -> None:
    """Back-compat: callers that don't pass ``current_prompt_version``
    get the pre-PR-#107 SQL (no stale-version predicate)."""
    from helixis.webhooks.queue import claim_planner_batch

    captured: dict[str, Any] = {}

    async def _fake_fetch(sql: str, *args: Any) -> list[Any]:
        captured["sql"] = sql
        captured["args"] = args
        return []

    pool = MagicMock()
    pool.fetch = _fake_fetch

    await claim_planner_batch(
        pool,
        batch_size=4,
        max_attempts=5,
        cooldown_seconds=30,
    )

    sql = captured["sql"]
    # ⚠ Widened 2026-08-29 to reclaim rows abandoned mid-plan; see
    # test_planner_queue_retry.py for the 52-day stall this closes.
    assert "planner_status IN ('pending', 'in_progress', 'timed_out')" in sql
    # Stale-version logic must NOT be present.
    assert "action_surface->>'prompt_version'" not in sql
    assert "IS DISTINCT FROM" not in sql
    # Three positional args, not four.
    assert captured["args"] == (4, 5, 30)


# ── ActionPlanner.plan stamps the version ────────────────────────


@pytest.mark.asyncio
async def test_planner_stamps_prompt_version_on_returned_surface() -> None:
    """After ``plan()`` returns, the surface must carry the current
    version so the worker can persist it. Without this, the
    self-heal claim would treat every new row as stale on the next
    tick."""
    from helixis.agent.enrichment.models import EnrichedEvent, EnrichedTask
    from helixis.agent.planner.action_planner import ActionPlanner

    # Minimal stubs — we only exercise the parse + stamp path, not
    # the LLM call or model resolution.
    raw_response_text = """{
        "schema_version": 2,
        "summary": "Sarah Chen at 123 Main St unit 4B reported a leak.",
        "actions": [],
        "freeform_placeholder": "describe what you want me to do…",
        "signals": null
    }"""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = raw_response_text

    completion = AsyncMock(return_value=response)
    settings = MagicMock()
    settings.llm.models = {"reasoning": {"model": "gemini/gemini-2.5-flash"}}
    settings.llm.default_model = "gemini/gemini-2.5-flash"
    settings.llm.google_api_key = "test-key"
    settings.llm.openai_api_key = None
    settings.llm.anthropic_api_key = None

    planner = ActionPlanner(settings=settings, completion_fn=completion)

    enriched = EnrichedEvent(
        event_name="Task.Created",
        company_id="company-xyz",
        task=EnrichedTask(
            id=17435,
            title="loud crashing",
            description="bathroom",
            category="General Inquiry",
            priority="Normal",
            requested_by_first_name="Sarah",
            requested_by_last_name="Chen",
        ),
    )

    surface = await planner.plan(enriched, company_id="company-xyz")

    assert surface.prompt_version == PLANNER_PROMPT_VERSION
