"""PR #99 — conversational intelligence backend tests.

Covers:
  - ProposedAction parses with new V2 fields (type, confidence,
    primary_action, rationale, triggers, alternative) and back-compat
    with V1-shape outputs (none of those fields present).
  - ActionSurface accepts an empty ``actions`` array (V2 allows zero
    suggestions when the planner has nothing genuine to propose).
  - ``_select_system_prompt()`` returns V2 by default and V1 when
    ``HELIXIS_PLANNER_INTENT_V2=0``.
  - V2 planner prompt embeds the information_need/bonus rule and the
    empty-array-acceptable rule.
  - V2 summarizer prompt embeds the four-anchor instructions.
  - ``claim_summary_batch`` picks up rows with stale
    ``summary_prompt_version`` when ``current_prompt_version`` is
    supplied.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from helixis.agent.planner.models import (
    ActionSurface,
    PrimaryAction,
    PrimaryActionTarget,
    ProposedAction,
)

# ── ProposedAction V2 fields ────────────────────────────────────────


def test_proposed_action_parses_v2_fields() -> None:
    """V2 planner output carries type/confidence/primary_action/etc."""
    data = {
        "tool_id": "send_tenant_message",
        "label": "Ask Sarah for photos",
        "intent": "Ask Sarah for bathroom photos",
        "parameters": {"tenant_id": 9001, "subject": "Photos"},
        "type": "information_need",
        "confidence": "high",
        "primary_action": {
            "label": "Send to Sarah",
            "kind": "send_message",
            "target": {"kind": "tenant", "name": "Sarah Chen"},
            "draft": "Hi Sarah — could you send a few photos?",
        },
        "rationale": "Description mentions crashing but no fixture.",
        "triggers": ["No attached photos", "Task description vague"],
        "alternative": "Could have dispatched blind.",
    }
    action = ProposedAction.model_validate(data)
    assert action.type == "information_need"
    assert action.confidence == "high"
    assert action.primary_action is not None
    assert action.primary_action.label == "Send to Sarah"
    assert action.primary_action.kind == "send_message"
    assert action.primary_action.target is not None
    assert action.primary_action.target.kind == "tenant"
    assert action.primary_action.target.name == "Sarah Chen"
    assert action.primary_action.draft == "Hi Sarah — could you send a few photos?"
    assert action.rationale == "Description mentions crashing but no fixture."
    assert action.triggers == ["No attached photos", "Task description vague"]
    assert action.alternative == "Could have dispatched blind."


def test_proposed_action_v1_back_compat() -> None:
    """V1 planner outputs (no type/confidence/primary_action) still parse."""
    data = {
        "tool_id": "update_buildium_task",
        "label": "Raise priority",
        "intent": "Raise priority to High",
        "parameters": {"priority": "High"},
    }
    action = ProposedAction.model_validate(data)
    assert action.type is None
    assert action.confidence is None
    assert action.primary_action is None
    assert action.rationale is None
    assert action.triggers is None
    assert action.alternative is None


def test_primary_action_target_optional() -> None:
    """primary_action with no target (e.g. internal note) is valid."""
    data = {
        "label": "Add note",
        "kind": "create_note",
        "target": None,
        "draft": "Internal observation.",
    }
    pa = PrimaryAction.model_validate(data)
    assert pa.target is None
    assert pa.draft == "Internal observation."


def test_primary_action_target_parses() -> None:
    target = PrimaryActionTarget.model_validate({"kind": "vendor", "name": "Eli Plumbing"})
    assert target.kind == "vendor"
    assert target.name == "Eli Plumbing"


# ── ActionSurface empty actions ──────────────────────────────────────


def test_action_surface_accepts_empty_actions() -> None:
    """PR #99 relaxed min_length from 1 → 0 on actions."""
    data = {
        "schema_version": 2,
        "summary": "Nothing actionable — no genuine information need or bonus suggestion.",
        "actions": [],
        "freeform_placeholder": "Type anything Helixis didn't propose…",
    }
    surface = ActionSurface.model_validate(data)
    assert surface.actions == []


def test_action_surface_with_signals() -> None:
    data = {
        "summary": "Sarah Chen reported a leak.",
        "actions": [],
        "freeform_placeholder": "Type…",
        "signals": ["Loud crashing matches fixture-failure pattern"],
    }
    surface = ActionSurface.model_validate(data)
    assert surface.signals == ["Loud crashing matches fixture-failure pattern"]


# ── _select_system_prompt flag behavior ──────────────────────────────


def test_select_system_prompt_default_returns_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (no env var) → V2."""
    monkeypatch.delenv("HELIXIS_PLANNER_INTENT_V2", raising=False)
    from helixis.agent.planner.action_planner import _select_system_prompt

    prompt = _select_system_prompt()
    assert "information_need" in prompt
    assert "bonus" in prompt


def test_select_system_prompt_flag_on_returns_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HELIXIS_PLANNER_INTENT_V2", "1")
    from helixis.agent.planner.action_planner import _select_system_prompt

    prompt = _select_system_prompt()
    assert "information_need" in prompt


def test_select_system_prompt_flag_off_returns_v1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HELIXIS_PLANNER_INTENT_V2", "0")
    from helixis.agent.planner.action_planner import _select_system_prompt

    prompt = _select_system_prompt()
    # V1 does NOT mention information_need.
    assert "information_need" not in prompt
    # V1 has the "1 and N actions" rule (count cap raised 4 → 6 with
    # the dynamic-card surface, PR #367).
    assert "1 and 6 actions" in prompt or "1-6" in prompt


def test_select_system_prompt_flag_false_returns_v1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HELIXIS_PLANNER_INTENT_V2", "false")
    from helixis.agent.planner.action_planner import _select_system_prompt

    prompt = _select_system_prompt()
    assert "information_need" not in prompt


# ── V2 planner prompt content ────────────────────────────────────────


def test_v2_planner_prompt_embeds_information_need_and_bonus_rule() -> None:
    from helixis.agent.planner.action_planner import SYSTEM_PROMPT_V2

    assert "information_need" in SYSTEM_PROMPT_V2
    assert "bonus" in SYSTEM_PROMPT_V2
    assert '"actions": []' in SYSTEM_PROMPT_V2 or "empty array" in SYSTEM_PROMPT_V2.lower()


def test_v2_planner_prompt_forbids_recategorization() -> None:
    from helixis.agent.planner.action_planner import SYSTEM_PROMPT_V2

    assert "Re-categorizing" in SYSTEM_PROMPT_V2 or "re-classifying" in SYSTEM_PROMPT_V2.lower()


def test_v2_planner_prompt_requires_signals() -> None:
    from helixis.agent.planner.action_planner import SYSTEM_PROMPT_V2

    assert "signals" in SYSTEM_PROMPT_V2


def test_v2_planner_prompt_scales_action_count_to_event() -> None:
    """v3 — the V2 prompt must tell the model to vary the number of
    suggestions with the event (up to SIX) instead of defaulting to a
    habitual ~2. This is the prompt half of the dynamic-card surface
    (PR #367 raised the schema cap + render-one-card-per-action; the
    cap alone doesn't change behavior without this guidance)."""
    from helixis.agent.planner.action_planner import SYSTEM_PROMPT_V2

    assert "SIX" in SYSTEM_PROMPT_V2 or "six" in SYSTEM_PROMPT_V2
    assert "let the event decide" in SYSTEM_PROMPT_V2.lower()


# ── V2 summarizer prompt content ─────────────────────────────────────


def test_summarizer_prompt_version_is_current() -> None:
    """PR #99 / PR #105 / PR #109 — pin the in-code prompt version constant.

    Lineage:
    - "v2" → PR #99 — four narrative anchors (reporter / where /
      creator / assignee).
    - "v3" → PR #105 — runtime fix (max_tokens 280 → 1500 to leave
      room for gemini-2.5-flash thinking budget); prompt TEXT unchanged
      from v2; version bump triggers re-summarization of truncated rows.
    - "v4" → PR #109 — prompt TEXT extended with CONTEXT (recurrence /
      urgency from enrichment.history_*) and RECOMMENDATION (one light
      data-grounded next-step sentence) sections; output cap relaxed
      from 1-3 to 2-4 sentences; version bump drains v3 rows.
    - "v5" → PR #121 — prompt TEXT adds an explicit FALLBACK branch
      for the case where the task has no requested_by name. v4 left
      this implicit and Gemini bailed to a one-liner ("A task was
      created this evening.") in production despite rich enrichment;
      v5 directs the model to lead with property + symptom and use
      active_lease tenants as context (never as the reporter).
    - "v6" → PR #127 — second no-reporter example added covering
      the stricter case where active_lease is ALSO null (because
      PR #124's UnitId validation dropped a mismatched lease).
      Production row 1ab5cdb8 (Garage door at 33 Garden Row #3D)
      regressed under v5 because no example showed
      property + symptom + history-only narratives; v6 adds one.
    """
    from helixis.agent.summarizer.prompt import SUMMARY_PROMPT_VERSION

    assert SUMMARY_PROMPT_VERSION == "v7"


def test_summarizer_prompt_has_narrative_anchors() -> None:
    from helixis.agent.summarizer.prompt import SYSTEM_PROMPT

    assert "WHO REPORTED IT" in SYSTEM_PROMPT
    assert "WHERE" in SYSTEM_PROMPT
    assert "WHO CREATED" in SYSTEM_PROMPT
    # Commit 1 (field strip): assignment/priority/category/due render in
    # the strip, so the prose must NOT restate them.
    assert "Do NOT restate the structured fields" in SYSTEM_PROMPT


def test_v2_summarizer_prompt_has_unassigned_clause() -> None:
    from helixis.agent.summarizer.prompt import SYSTEM_PROMPT

    assert "currently unassigned" in SYSTEM_PROMPT


def test_v2_summarizer_prompt_keeps_loud_fail_rules() -> None:
    """PR #92's ID-shaped reference rule must survive the v2 rewrite."""
    from helixis.agent.summarizer.prompt import SYSTEM_PROMPT

    assert "Task #17435" in SYSTEM_PROMPT or "Buildium internal ID" in SYSTEM_PROMPT


def test_v2_summarizer_prompt_forbids_placeholder_words() -> None:
    from helixis.agent.summarizer.prompt import SYSTEM_PROMPT

    # The prompt must warn against generic placeholder words.
    assert "placeholder" in SYSTEM_PROMPT.lower() or "the property" in SYSTEM_PROMPT


# ── claim_summary_batch stale-version pickup ─────────────────────────


@pytest.mark.asyncio
async def test_claim_summary_batch_with_version_picks_up_stale_rows() -> None:
    """When ``current_prompt_version`` is passed, the WHERE clause
    includes ``summary_status = 'succeeded' AND
    summary_prompt_version IS DISTINCT FROM $4``."""
    from helixis.webhooks.queue import claim_summary_batch

    captured: dict[str, Any] = {}

    async def _fake_fetch(sql: str, *args: Any) -> list[Any]:
        captured["sql"] = sql
        captured["args"] = args
        return []

    pool = MagicMock()
    pool.fetch = _fake_fetch

    await claim_summary_batch(
        pool,
        batch_size=4,
        max_attempts=5,
        cooldown_seconds=30,
        current_prompt_version="v2",
    )

    sql = captured["sql"]
    # Must include both the normal pending/retrying path AND the
    # stale-version path.
    assert "summary_status IN ('pending', 'retrying')" in sql
    assert "summary_prompt_version IS DISTINCT FROM $4" in sql
    assert "summary_status = 'succeeded'" in sql
    assert captured["args"] == (4, 5, 30, "v2")


@pytest.mark.asyncio
async def test_claim_summary_batch_without_version_uses_original_sql() -> None:
    """Without ``current_prompt_version``, the SQL is unchanged from
    PR #92 — no stale-version predicate."""
    from helixis.webhooks.queue import claim_summary_batch

    captured: dict[str, Any] = {}

    async def _fake_fetch(sql: str, *args: Any) -> list[Any]:
        captured["sql"] = sql
        captured["args"] = args
        return []

    pool = MagicMock()
    pool.fetch = _fake_fetch

    await claim_summary_batch(
        pool,
        batch_size=4,
        max_attempts=5,
        cooldown_seconds=30,
    )

    sql = captured["sql"]
    assert "summary_status IN ('pending', 'retrying')" in sql
    assert "summary_prompt_version IS DISTINCT FROM" not in sql
    assert captured["args"] == (4, 5, 30)


@pytest.mark.asyncio
async def test_claim_summary_batch_stale_version_respects_attempts_and_cooldown() -> None:
    """Stale-version rows must still pass through max_attempts
    and cooldown gates."""
    from helixis.webhooks.queue import claim_summary_batch

    captured: dict[str, Any] = {}

    async def _fake_fetch(sql: str, *args: Any) -> list[Any]:
        captured["sql"] = sql
        return []

    pool = MagicMock()
    pool.fetch = _fake_fetch

    await claim_summary_batch(
        pool,
        batch_size=3,
        max_attempts=3,
        cooldown_seconds=60,
        current_prompt_version="v2",
    )

    sql = captured["sql"]
    assert "summary_attempts < $2" in sql
    assert "summary_last_attempted_at IS NULL" in sql
    # ⚠ RE-POINTED 2026-09-10, not weakened. The claim is now a two-CTE
    # round-robin (webhooks/fair_queue), so the lock names its table:
    # Postgres refuses a bare FOR UPDATE over a join to a CTE. The
    # property asserted — concurrent workers skip rather than block — is
    # unchanged, and this literal is what the live server accepted.
    assert "FOR UPDATE OF b SKIP LOCKED" in sql
