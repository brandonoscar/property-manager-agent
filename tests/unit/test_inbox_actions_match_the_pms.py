"""An Inbox card offers only writes that can run on the company's PMS.

Measured 2026-10-03 on helixis-test: the one Rentvine company's Inbox held 17
``Risk.FlagRaised`` cards offering ``create_followup_task`` (10) and
``add_internal_note`` (7). Both write to Buildium, and a company is on exactly
one PMS (ADR 0010 a1), so every button could only fail, with a 400 telling the
user to configure Buildium credentials, which the database forbids
(gotcha 183).

Three layers, each tested: the planner offers and keeps only runnable tools,
the Inbox read marks an action planned before that (never removing it, since
the executor addresses an action by position), and the executor refuses one.
The routes' half lives in ``test_inbox_endpoints.py``.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path

import pytest

from helixis.agent.enrichment.models import EnrichedEvent
from helixis.agent.planner import ActionPlanner
from helixis.agent.planner import tool_catalog as catalog
from helixis.webhooks import worker

from .test_planner_action import (
    _HAPPY_SURFACE,
    COMPANY_ID,
    _enriched_minimal,
    _ScriptedCompletion,
    _settings,
)

_REPO = Path(__file__).resolve().parents[2]
_BUILDIUM_WRITES = ["update_buildium_task", "add_internal_note", "send_tenant_message"]


# ── the catalog ──────────────────────────────────────────────────────────────


def test_the_buildium_prompt_catalog_is_unchanged() -> None:
    ids = [t["id"] for t in catalog.tool_catalog_as_prompt()]
    assert ids == [*_BUILDIUM_WRITES, "create_followup_task"]
    assert catalog.tool_catalog_as_prompt() == catalog.tool_catalog_as_prompt(pms="buildium")


def test_a_rentvine_event_is_offered_no_buildium_write() -> None:
    assert catalog.tool_catalog_as_prompt(pms="rentvine") == []
    # An email reply goes out through Gmail, so a Rentvine inbox keeps it.
    email = catalog.tool_catalog_as_prompt(email=True, pms="rentvine")
    assert [t["id"] for t in email] == ["reply_to_email"]


def test_the_reason_names_what_the_user_can_do() -> None:
    reason = catalog.unavailable_reason("create_followup_task", "rentvine")
    assert reason is not None
    assert "Buildium" in reason and "Make this change in Rentvine." in reason
    assert catalog.unavailable_reason("create_followup_task", "buildium") is None
    assert catalog.unavailable_reason("reply_to_email", "rentvine") is None
    assert catalog.unavailable_reason("not_a_tool", "rentvine") is None


def test_marking_keeps_every_action_in_place() -> None:
    surface = {
        "summary": "s",
        "actions": [
            {"tool_id": "reply_to_email", "label": "Reply"},
            {"tool_id": "add_internal_note", "label": "Note"},
            "not-a-dict",
        ],
    }
    marked = catalog.mark_unavailable(surface, "rentvine")
    assert [a.get("tool_id") if isinstance(a, dict) else a for a in marked["actions"]] == [
        "reply_to_email",
        "add_internal_note",
        "not-a-dict",
    ]
    assert catalog.UNAVAILABLE_KEY not in marked["actions"][0]
    assert "Rentvine" in marked["actions"][1][catalog.UNAVAILABLE_KEY]
    # The stored surface is not mutated, and a Buildium company sees no mark.
    assert catalog.UNAVAILABLE_KEY not in surface["actions"][1]
    assert catalog.mark_unavailable(surface, "buildium") == surface
    assert catalog.mark_unavailable(None, "rentvine") is None


# ── the planner ──────────────────────────────────────────────────────────────


def _catalog_in(prompt: str) -> list[dict[str, object]]:
    """The TOOL CATALOG JSON block of the planner's user message."""
    after = prompt.split("TOOL CATALOG:\n", 1)[1]
    parsed, _end = json.JSONDecoder().raw_decode(after)
    return list(parsed)


async def _plan(pms: str | None, enriched: EnrichedEvent | None = None) -> tuple[object, str]:
    fake = _ScriptedCompletion([json.dumps(_HAPPY_SURFACE)])
    planner = ActionPlanner(_settings(), completion_fn=fake)
    kwargs = {} if pms is None else {"pms": pms}
    surface = await planner.plan(enriched or _enriched_minimal(), COMPANY_ID, **kwargs)
    return surface, fake.calls[0]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_a_rentvine_plan_keeps_the_reading_and_drops_the_buildium_writes() -> None:
    surface, prompt = await _plan("rentvine")
    assert _catalog_in(prompt) == []
    # The model named two Buildium writes anyway; neither survives, and the
    # card's reading of the event does.
    assert surface.actions == []  # type: ignore[attr-defined]
    assert surface.summary == _HAPPY_SURFACE["summary"]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_buildium_plan_is_byte_for_byte_what_it_was() -> None:
    # One event for both plans: it carries the moment it was enriched.
    enriched = _enriched_minimal()
    default_surface, default_prompt = await _plan(None, enriched)
    surface, prompt = await _plan("buildium", enriched)
    assert prompt == default_prompt
    assert [t["id"] for t in _catalog_in(prompt)] == [*_BUILDIUM_WRITES, "create_followup_task"]
    assert [a.tool_id for a in surface.actions] == [  # type: ignore[attr-defined]
        "update_buildium_task",
        "send_tenant_message",
    ]
    assert surface == default_surface


def test_the_worker_plans_with_the_companys_pms() -> None:
    """``plan`` defaults to Buildium, so the worker must pass the company's."""
    tree = ast.parse(inspect.getsource(worker))
    plans = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "plan"
    ]
    assert plans, "the worker no longer calls planner.plan"
    for call in plans:
        pms = next((k.value for k in call.keywords if k.arg == "pms"), None)
        assert isinstance(pms, ast.Await), "plan() is called without the company's pms"
        inner = pms.value
        assert isinstance(inner, ast.Call) and getattr(inner.func, "id", None) == "company_pms"


# ── the web app reads the same field ─────────────────────────────────────────


def test_the_inbox_hides_the_field_the_server_sets() -> None:
    """Python and TypeScript must name the field the same (CLAUDE.md rule 15)."""
    key = catalog.UNAVAILABLE_KEY
    types = (_REPO / "frontend/src/services/inbox.ts").read_text(encoding="utf-8")
    interface = re.search(r"export interface InboxAction \{(.*?)\n\}", types, re.S)
    assert interface and re.search(rf"\n\s*{key}\?:", interface.group(1)), (
        f"InboxAction does not declare {key}"
    )
    page = (_REPO / "frontend/src/components/InboxPage.tsx").read_text(encoding="utf-8")
    card_list = page.split("function ActionCardList(", 1)[1].split("\nfunction ", 1)[0]
    assert re.search(rf"\.filter\([^;]*!action\.{key}\b", card_list), (
        f"ActionCardList no longer hides an action carrying {key}"
    )
