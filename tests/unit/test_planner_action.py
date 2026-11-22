"""Unit tests for ``ActionPlanner``.

Strategy: inject a fake ``completion_fn`` so we never call LiteLLM /
LLMs / network. The fake returns a precomposed string that the planner
parses, validates, and returns. We exercise:
  1. Happy path: 4 valid actions, schema-conforming.
  2. Pattern detection: planner sees 3 plumbing tasks in 90 days, the
     fake LLM proposes a follow-up — verifies the prompt + parsing path.
  3. Invalid JSON on first attempt → retried once with a correction
     nudge → second attempt valid → returns.
  4. Two invalid attempts → raises PlannerError.
  5. Tool id outside the catalog → raises PlannerError after retry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import litellm
import pytest

from helixis.agent.enrichment.models import (
    EnrichedEvent,
    EnrichedHistoricalTask,
    EnrichedLease,
    EnrichedTask,
    EnrichedTenant,
    EnrichedUnit,
)
from helixis.agent.planner import ActionPlanner, PlannerError
from helixis.settings import Settings

COMPANY_ID = "company-A"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the transient-retry backoff instant in tests so the 0.5s
    ``asyncio.sleep`` in ``plan()``'s retry branch doesn't slow the
    suite. Autouse so every test in the file gets it for free."""

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("helixis.agent.planner.action_planner.asyncio.sleep", _instant)


# ── Fake LiteLLM response objects ────────────────────────────────


@dataclass
class _FakeMessage:
    content: str


@dataclass
class _FakeChoice:
    message: _FakeMessage


@dataclass
class _FakeResponse:
    choices: list[_FakeChoice]


class _ScriptedCompletion:
    """Callable async fake. Yields the next scripted response on each
    invocation; records every call's kwargs so tests can assert what
    the planner sent to LiteLLM."""

    def __init__(self, scripts: list[str]) -> None:
        self._scripts = list(scripts)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        if not self._scripts:
            raise AssertionError("scripted completion ran out of responses")
        content = self._scripts.pop(0)
        return _FakeResponse(choices=[_FakeChoice(_FakeMessage(content))])


# ── Sample enriched events ───────────────────────────────────────


def _enriched_minimal() -> EnrichedEvent:
    return EnrichedEvent(
        event_name="Task.Created",
        company_id=COMPANY_ID,
        task=EnrichedTask(
            id=4001,
            title="Kitchen sink leaking",
            description="Tenant reports drip under the sink.",
            status="New",
            category="Plumbing",
            priority="Normal",
            property_id=31,
            unit_id=412,
        ),
        unit=EnrichedUnit(id=412, property_id=31, unit_number="3B"),
        active_lease=EnrichedLease(
            id=7771,
            status="Active",
            tenants=[
                EnrichedTenant(id=9001, first_name="Mei", last_name="Chen", email="mei@example.com")
            ],
        ),
        history_unit=[],
        history_property=[],
    )


def _enriched_with_plumbing_pattern() -> EnrichedEvent:
    base = _enriched_minimal()
    base.history_unit = [
        EnrichedHistoricalTask(
            id=3980,
            title="Bathroom sink slow drain",
            category="Plumbing",
            status="Completed",
            created_date=datetime(2026, 3, 1),
        ),
        EnrichedHistoricalTask(
            id=3950,
            title="Toilet running",
            category="Plumbing",
            status="Completed",
            created_date=datetime(2026, 2, 14),
        ),
        EnrichedHistoricalTask(
            id=3900,
            title="Kitchen faucet drip",
            category="Plumbing",
            status="Completed",
            created_date=datetime(2026, 1, 5),
        ),
    ]
    return base


# ── Tests ────────────────────────────────────────────────────────


def _settings() -> Settings:
    return Settings(
        env="test",
        memory={"enabled": False},
        api={"host": "127.0.0.1", "port": 8000},
        llm={
            "default_model": "gemini/gemini-2.0-flash",
            "google_api_key": "fake-key",
            "models": {"reasoning": {"model": "gemini/gemini-2.0-flash"}},
        },
    )


_HAPPY_SURFACE = {
    "summary": (
        "Tenant Mei Chen reported a kitchen sink leak in unit 3B; the task "
        "is new and uncategorised at Normal priority."
    ),
    "actions": [
        {
            "tool_id": "update_buildium_task",
            "label": "Raise to High priority",
            "intent": "Raise priority to High",
            "parameters": {"priority": "High"},
        },
        {
            "tool_id": "send_tenant_message",
            "label": "Confirm with Mei",
            "intent": "Ask Mei to confirm a 2-hour service window tomorrow",
            "parameters": {
                "tenant_id": 9001,
                "subject": "Quick check before we send a plumber",
                "body": (
                    "Hi Mei, we have your kitchen sink leak on the list — "
                    "can we send a plumber tomorrow between 9-11am or 1-3pm?"
                ),
            },
        },
    ],
    "freeform_placeholder": "e.g. 'send vendor with parts for kitchen sink'",
}


@pytest.mark.asyncio
async def test_happy_path_returns_validated_action_surface() -> None:
    fake = _ScriptedCompletion([json.dumps(_HAPPY_SURFACE)])
    planner = ActionPlanner(_settings(), completion_fn=fake)

    surface = await planner.plan(_enriched_minimal(), COMPANY_ID)

    assert surface.summary.startswith("Tenant Mei Chen")
    assert len(surface.actions) == 2
    assert surface.actions[0].tool_id == "update_buildium_task"
    assert surface.actions[0].parameters == {"priority": "High"}
    assert surface.actions[1].parameters["tenant_id"] == 9001
    assert "kitchen" in surface.freeform_placeholder.lower()
    # One LLM call.
    assert len(fake.calls) == 1
    # The user message must include the enriched event JSON and the
    # tool catalog (to guarantee the planner is giving the model what
    # the spec requires).
    user_msg = fake.calls[0]["messages"][1]["content"]
    assert "ENRICHED EVENT" in user_msg
    assert "TOOL CATALOG" in user_msg


@pytest.mark.asyncio
async def test_pattern_detected_planner_includes_followup_action() -> None:
    """When the LLM is shown 3 plumbing tasks in 90 days, the planner's
    job is to surface a follow-up inspection. We assert the parsed
    surface carries the create_followup_task tool with a reasonable
    intent — the LLM behaviour is faked here, but the planner must
    pass it through and validate it."""

    pattern_response = {
        "summary": (
            "Third plumbing complaint at unit 3B in 90 days — Mei Chen "
            "reports a kitchen sink leak today, following bathroom drain "
            "and toilet issues earlier this year."
        ),
        "actions": [
            {
                "tool_id": "update_buildium_task",
                "label": "Raise priority + Plumbing category",
                "intent": "Raise priority to High and re-categorise as Plumbing",
                "parameters": {"priority": "High", "category": "Plumbing"},
            },
            {
                "tool_id": "create_followup_task",
                "label": "Schedule unit-level plumbing inspection (30d)",
                "intent": "Schedule plumbing inspection of unit 3B in 30 days",
                "parameters": {
                    "title": "Plumbing inspection — unit 3B",
                    "description": (
                        "Third plumbing task in 90 days. Inspect all "
                        "fixtures and supply lines to head off the trend."
                    ),
                    "due_date": "2026-06-12",
                    "category": "Plumbing",
                    "unit_id": 412,
                    "priority": "Normal",
                },
            },
        ],
        "freeform_placeholder": "e.g. 'send Eli Plumbing for a full inspection'",
    }

    fake = _ScriptedCompletion([json.dumps(pattern_response)])
    planner = ActionPlanner(_settings(), completion_fn=fake)

    surface = await planner.plan(_enriched_with_plumbing_pattern(), COMPANY_ID)

    tool_ids = {a.tool_id for a in surface.actions}
    assert "create_followup_task" in tool_ids
    followup = next(a for a in surface.actions if a.tool_id == "create_followup_task")
    assert followup.parameters["unit_id"] == 412
    assert followup.parameters["category"] == "Plumbing"


@pytest.mark.asyncio
async def test_invalid_json_first_attempt_retried_once_then_succeeds() -> None:
    """Planner's contract: invalid response → retry once with a
    correction nudge → if the second attempt is valid, return it."""
    fake = _ScriptedCompletion(
        [
            "not valid json {",
            json.dumps(_HAPPY_SURFACE),
        ]
    )
    planner = ActionPlanner(_settings(), completion_fn=fake)

    surface = await planner.plan(_enriched_minimal(), COMPANY_ID)

    assert surface.summary.startswith("Tenant Mei Chen")
    # Two LLM calls (initial + one retry).
    assert len(fake.calls) == 2
    # The retry call's message list includes the correction nudge as
    # the third entry (system / user / correction).
    second_messages = fake.calls[1]["messages"]
    assert len(second_messages) == 3
    assert "not valid" in second_messages[2]["content"].lower()


@pytest.mark.asyncio
async def test_two_invalid_attempts_raises_planner_error() -> None:
    fake = _ScriptedCompletion(
        [
            "still not json",
            "still still not json {",
        ]
    )
    # Pin max_retries=1 so this exhaustion test stays deterministic and
    # independent of the prod default (bumped to 3 for Gemini-3 JSON resilience).
    planner = ActionPlanner(_settings(), completion_fn=fake, max_retries=1)

    with pytest.raises(PlannerError):
        await planner.plan(_enriched_minimal(), COMPANY_ID)
    # Both attempts consumed.
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_call_llm_passes_timeout_kwarg_to_completion_fn() -> None:
    """Regression: ``acompletion`` must be called with an explicit
    ``timeout`` so LiteLLM does NOT fall back to its provider default
    (~600s for Gemini). Pre-fix the kwargs dict did not include
    ``timeout``.

    PR #89: bumped from 8.0s to 30.0s when the planner moved out of
    the synchronous webhook receiver into the worker. Production rows
    0fd4e984 / fcb56b89 were silently timing out at 8s; the worker
    is decoupled from Buildium's 10s SLA so a realistic budget is
    safe."""
    fake = _ScriptedCompletion([json.dumps(_HAPPY_SURFACE)])
    planner = ActionPlanner(_settings(), completion_fn=fake)

    await planner.plan(_enriched_minimal(), COMPANY_ID)

    assert len(fake.calls) == 1
    assert fake.calls[0]["timeout"] == 30.0


@pytest.mark.asyncio
async def test_plan_retries_on_transient_network_error_then_succeeds() -> None:
    """A single ``litellm.Timeout`` should trigger one retry, after
    which the planner returns the second attempt's parsed surface.
    Regression for audit Finding #8 — transient network errors used to
    propagate as PlannerError on the first attempt with no retry."""

    class _FlakyCompletion:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def __call__(self, **kwargs: Any) -> _FakeResponse:
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise litellm.Timeout(message="boom", model="gemini/x", llm_provider="gemini")
            return _FakeResponse(choices=[_FakeChoice(_FakeMessage(json.dumps(_HAPPY_SURFACE)))])

    fake = _FlakyCompletion()
    planner = ActionPlanner(_settings(), completion_fn=fake)

    surface = await planner.plan(_enriched_minimal(), COMPANY_ID)
    assert surface.summary.startswith("Tenant Mei Chen")
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_plan_raises_planner_error_when_transient_exhausts_retries() -> None:
    """Two consecutive ``litellm.APIConnectionError`` raises
    PlannerError with the transient-retry message."""

    async def always_fails(**_kwargs: Any) -> _FakeResponse:
        raise litellm.APIConnectionError(message="network", llm_provider="gemini", model="gemini/x")

    planner = ActionPlanner(_settings(), completion_fn=always_fails)

    with pytest.raises(PlannerError, match="exhausted transient retries"):
        await planner.plan(_enriched_minimal(), COMPANY_ID)


@pytest.mark.asyncio
async def test_tool_id_outside_catalog_is_rejected() -> None:
    """An LLM response that references a tool not in TOOL_CATALOG
    fails validation and (after the retry budget is exhausted) raises
    PlannerError. Guards against drift between the planner's prompt
    and the executor side."""
    bogus = {
        "summary": "Tenant Mei Chen reported a kitchen sink leak in unit 3B.",
        "actions": [
            {
                "tool_id": "ssh_into_buildium",
                "label": "do something bad",
                "intent": "do something bad",
                "parameters": {},
            }
        ],
        "freeform_placeholder": "say more",
    }
    fake = _ScriptedCompletion([json.dumps(bogus), json.dumps(bogus)])
    planner = ActionPlanner(_settings(), completion_fn=fake)

    with pytest.raises(PlannerError):
        await planner.plan(_enriched_minimal(), COMPANY_ID)


@pytest.mark.asyncio
async def test_fenced_attempt0_parses_without_retry() -> None:
    """#161 (planner leg): a ```json-fenced but otherwise valid action surface
    must parse on the FIRST call — no correction-nudge retry."""
    fenced = "```json\n" + json.dumps(_HAPPY_SURFACE) + "\n```"
    fake = _ScriptedCompletion([fenced])
    planner = ActionPlanner(_settings(), completion_fn=fake)

    surface = await planner.plan(_enriched_minimal(), COMPANY_ID)

    assert len(surface.actions) == 2
    assert surface.actions[0].tool_id == "update_buildium_task"
    assert len(fake.calls) == 1  # NO retry — the fence was coerced on attempt 0


def test_parse_action_surface_coerces_prose_wrapped_json() -> None:
    """Unit-level: _parse_action_surface tolerates surrounding prose."""
    from helixis.agent.planner.action_planner import _parse_action_surface

    noisy = "Here is the action surface:\n" + json.dumps(_HAPPY_SURFACE) + "\nLet me know!"
    surface = _parse_action_surface(noisy)
    assert len(surface.actions) == 2
