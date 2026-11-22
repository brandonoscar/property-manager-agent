"""Planner email-path tests.

Verifies the email-ingest planner branch: the reply_to_email tool is in
the catalog, the email prompt is selected for email events, the Buildium
prompts/catalog are unchanged, and plan() returns a valid surface with a
reply_to_email action (fake LLM — no network).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from helixis.agent.enrichment.models import EnrichedEvent
from helixis.agent.planner import ActionPlanner
from helixis.agent.planner.action_planner import (
    SYSTEM_PROMPT_EMAIL,
    _build_user_prompt,
    _is_email_event,
    _select_system_prompt,
)
from helixis.agent.planner.tool_catalog import TOOL_IDS, tool_catalog_as_prompt
from helixis.settings import Settings
from helixis.webhooks.email_triage import InboundEmail, build_email_enrichment


def _settings() -> Settings:
    return Settings(
        env="test",
        llm={"default_model": "openai/gpt-4o", "openai_api_key": "test-key"},
        memory={"enabled": False},
    )


def _email_event() -> EnrichedEvent:
    email = InboundEmail(
        message_id="m1",
        from_email="sarah@example.com",
        from_name="Sarah Chen",
        subject="Lease renewal",
        body_text=" ".join(["renew"] * 150),
        received_at="2026-06-27T00:00:00Z",
        thread_id="t1",
    )
    return EnrichedEvent.model_validate(build_email_enrichment(email, "co-1"))


# ── catalog ──────────────────────────────────────────────────────────


def test_reply_tool_in_ids_but_excluded_from_buildium_catalog() -> None:
    assert "reply_to_email" in TOOL_IDS
    buildium = {t["id"] for t in tool_catalog_as_prompt()}
    assert "reply_to_email" not in buildium  # Buildium prompt unchanged
    assert "update_buildium_task" in buildium
    email = {t["id"] for t in tool_catalog_as_prompt(email=True)}
    assert email == {"reply_to_email"}  # email prompt sees only the reply tool


# ── prompt selection ─────────────────────────────────────────────────


def test_is_email_event() -> None:
    assert _is_email_event(_email_event())
    buildium = _email_event()
    # Flip it to a Buildium-shaped event → not email.
    buildium = buildium.model_copy(update={"event_name": "Task.Created"})
    buildium.task.category = "Plumbing"
    assert not _is_email_event(buildium)


def test_select_system_prompt_branches() -> None:
    assert _select_system_prompt(_email_event()) is SYSTEM_PROMPT_EMAIL
    buildium = _email_event().model_copy(update={"event_name": "Task.Created"})
    buildium.task.category = "Plumbing"
    assert _select_system_prompt(buildium) is not SYSTEM_PROMPT_EMAIL


def test_build_user_prompt_uses_email_catalog() -> None:
    p = _build_user_prompt(_email_event(), email=True)
    assert "reply_to_email" in p
    assert "update_buildium_task" not in p


# ── plan() end-to-end with a fake LLM ────────────────────────────────


@dataclass
class _Msg:
    content: str


@dataclass
class _Choice:
    message: _Msg


@dataclass
class _Resp:
    choices: list[_Choice]


def _fake(content: str):
    async def _call(**_kwargs: Any) -> _Resp:
        return _Resp(choices=[_Choice(_Msg(content))])

    return _call


@pytest.mark.asyncio
async def test_plan_email_returns_reply_surface() -> None:
    surface_json = json.dumps(
        {
            "schema_version": 2,
            "summary": "Sarah Chen wants to renew her lease.",
            "actions": [
                {
                    "tool_id": "reply_to_email",
                    "label": "Reply to Sarah",
                    "intent": "Confirm renewal and next steps",
                    "type": "information_need",
                    "confidence": "high",
                    "primary_action": {
                        "label": "Review & send",
                        "kind": "send_message",
                        "target": {"kind": "tenant", "name": "Sarah Chen"},
                        "draft": "Hi Sarah, happy to renew — paperwork coming this week.",
                    },
                    "parameters": {
                        "body": "Hi Sarah, happy to renew — paperwork coming this week."
                    },
                }
            ],
            "freeform_placeholder": "edit the reply…",
            "signals": ["She's asking to renew — time-sensitive"],
        }
    )
    planner = ActionPlanner(_settings(), completion_fn=_fake(surface_json))
    surface = await planner.plan(_email_event(), "co-1")
    assert len(surface.actions) == 1
    action = surface.actions[0]
    assert action.tool_id == "reply_to_email"  # passes _parse_action_surface validation
    assert action.primary_action.draft.startswith("Hi Sarah")
    assert surface.signals == ["She's asking to renew — time-sensitive"]


# ── enrichment registry wiring (bug #2: planner never ran for emails) ─


def test_email_event_registered_in_enrichment_registry() -> None:
    from helixis.agent.planner import ActionPlanner
    from helixis.webhooks.email_triage import EMAIL_EVENT_NAME
    from helixis.webhooks.handlers.enrichment_registry import get_enrichment_handler

    handler = get_enrichment_handler(EMAIL_EVENT_NAME)
    assert handler is not None, "Email.Received must resolve a handler or the planner never runs"
    assert isinstance(handler.planner, ActionPlanner)


@pytest.mark.asyncio
async def test_email_enricher_builds_valid_event_from_payload() -> None:
    from helixis.agent.enrichment import EmailEventEnricher
    from helixis.webhooks.email_ingest import build_email_event_payload
    from helixis.webhooks.email_triage import InboundEmail

    payload = build_email_event_payload(
        InboundEmail(
            message_id="m1",
            from_email="sarah@example.com",
            from_name="Sarah Chen",
            subject="Lease question",
            body_text="x",
            received_at="2026-06-27T00:00:00Z",
            thread_id="t1",
        ),
        account_id=0,
    )
    enriched = await EmailEventEnricher().enrich(payload, "co-1")
    assert enriched.event_name == "Email.Received"
    assert enriched.task.title == "Lease question"
    assert enriched.task.raw["thread_id"] == "t1"
