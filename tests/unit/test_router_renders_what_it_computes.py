"""The router must be able to DRAW what it can COMPUTE.

WHY THIS FILE EXISTS
--------------------
Four card renderers — ``show_chart``, ``show_detail_card``, ``show_checklist``,
``show_schedule`` — were built end to end and had **never rendered once**.
Measured product-wide over all 514 ``orchestrator_runs`` since 2026-06-03
(445 of them carrying tool calls):

    show_table            73 calls   ← registered on the router
    suggest_next_steps     8 calls   ← specialist-only
    show_chart             0
    show_detail_card       0
    show_checklist         0
    show_schedule          0

Every layer was present and individually tested: the tool, the payload
builder, the ``EventType`` member, the SSE case, the React component, the
component test. Nothing joined them, because the only agent that writes the
user's final answer did not carry the tools. That is gotcha 28's
producer/consumer split one level up, and gotcha 35's rule pointed at a
feature rather than a test — **a card that has never rendered is not a
shipped card.**

The gap widened over time rather than closing: the eight portfolio rollups
were deliberately moved ONTO the router so it would stop opening a specialist
to answer "who's behind on rent", which also moved those answers onto the one
path that had no renderer for them.

WHAT THIS GUARDS, AND WHY IT IS NOT A TAUTOLOGY
-----------------------------------------------
Asserting "these four names appear in ``base_tools``" would just restate the
diff. The contract worth pinning is the PAIRING: a data tool on the router
whose output has an obvious visual shape must have that renderer on the router
too. That fails the day someone adds the next rollup and forgets its renderer
— which is exactly how this bug was made.

Gotcha 45's rule applies as well: the guards read the REGISTRIES, not
hand-written inputs. A test fed a fabricated tool list would pass forever
while the real wiring rotted.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from helixis.agents import visual_tools
from helixis.agents.buildium_agent import create_buildium_agent
from helixis.agents.calendar_agent import create_calendar_agent
from helixis.agents.orchestrator import create_orchestrator
from helixis.common.events import EventType
from helixis.settings import Settings

# ── The pairing contract ────────────────────────────────────────────────
#
# "If the router can produce THIS, it must be able to draw it THIS way."
#
# Each entry is (data tool registered on the router, renderer it needs, why).
# Deliberately written out by name rather than derived: the mapping is a
# judgement about what a tool's OUTPUT looks like, and a derivation would
# invent pairings nobody decided on.
ROUTER_DATA_TO_RENDERER: list[tuple[str, str, str]] = [
    ("spending_report", "show_chart", "spend by property/category is a comparison"),
    ("delinquency_report", "show_chart", "balances by bucket is a comparison"),
    ("unpaid_bills", "show_chart", "amounts by vendor/property is a comparison"),
    ("portfolio_overview", "show_chart", "the rollup's numbers are the answer"),
    ("contact_lookup", "show_detail_card", "a single hit is one record's facts"),
    ("portfolio_review", "show_checklist", "a portfolio review is a multi-step plan"),
]

#: Renderers the router must NOT carry, with the reason. The omission is a
#: decision, not an oversight, so it gets a test of its own — otherwise the
#: next reader "completes the set" and pays schema cost for a card the router
#: can never populate.
#:
#: ⚠ ``test_reasoning_loop_topology._MUST_NOT_BE_ON_THE_ROUTER`` pins the same
#: name. That is deliberate redundancy, not a second spelling: both assert the
#: SAME direction (absent from the router), so they cannot drift into
#: disagreeing the way gotcha 50's two definitions of "open" did — adding
#: ``show_schedule`` to the router fails both. This file carries the rendering
#: reason; that one is the whole-router census.
ROUTER_MUST_NOT_RENDER: list[tuple[str, str]] = [
    ("show_schedule", "the router holds no time-slot data; calendar_agent does"),
]


def _settings() -> Settings:
    return Settings()


def _tool_names(agent: object) -> set[str]:
    return {getattr(t, "name", "") for t in getattr(agent, "tools", [])}


@pytest.fixture(scope="module", params=[True, False], ids=["loop_mode", "handoff_mode"])
def router_tools(request: pytest.FixtureRequest) -> set[str]:
    """The orchestrator's ACTUAL registered tool names, built the way the app
    builds it. Read from the factory rather than parsed out of the source, so
    a conditional append or a rename can't slip past.

    ⚠ BOTH modes, because the repo cannot prove which one production runs.
    ``reasoning_loop`` defaults ``False`` in ``settings.py`` and is set by env
    var on Render — gotcha 29, whose whole point is that a default plus a
    config grep is not evidence. The two paths assemble ``tools=`` separately,
    so testing one would leave the live one uncovered."""
    agent = create_orchestrator(_settings(), loop_mode=request.param)
    return _tool_names(agent)


def test_the_router_can_draw_everything_it_can_compute(router_tools: set[str]) -> None:
    """Every (data tool, renderer) pair: holding the data means holding the
    renderer. This is the guard that would have caught the original bug — the
    rollups arrived on the router and their renderers did not."""
    missing: list[str] = []
    for data_tool, renderer, why in ROUTER_DATA_TO_RENDERER:
        if data_tool not in router_tools:
            # The pairing only binds when the router actually holds the data.
            # A rollup moved back to a specialist takes its obligation with it.
            continue
        if renderer not in router_tools:
            missing.append(f"{data_tool!r} is on the router but {renderer!r} is not — {why}")
    assert not missing, (
        "The router can compute these and cannot draw them:\n  "
        + "\n  ".join(missing)
        + "\n\nRegister the renderer in create_orchestrator_agent's base_tools, "
        "or move the data tool back to a specialist that has it."
    )


def test_at_least_one_pairing_is_live(router_tools: set[str]) -> None:
    """Guard the guard. If every data tool in the table were absent from the
    router the test above would pass vacuously — 'no pairings bind' reads
    exactly like 'all pairings hold'. Gotcha 33's dead-sandbox shape: an
    assertion that never runs is not an assertion that passed."""
    live = [d for d, _r, _w in ROUTER_DATA_TO_RENDERER if d in router_tools]
    assert live, (
        "None of the paired data tools are on the router, so the pairing test "
        "asserted nothing. Either the rollups moved and this table is stale, "
        f"or the factory changed. Router holds: {sorted(router_tools)}"
    )


def test_the_router_does_not_carry_a_renderer_it_cannot_feed(router_tools: set[str]) -> None:
    """The deliberate omissions stay omitted. Schema cost on every turn for a
    card the agent can never populate is pure loss."""
    for renderer, why in ROUTER_MUST_NOT_RENDER:
        assert renderer not in router_tools, (
            f"{renderer!r} is registered on the router, but {why}. "
            "If the router gained the data this renderer needs, move it out of "
            "ROUTER_MUST_NOT_RENDER and into ROUTER_DATA_TO_RENDERER with the "
            "data tool that feeds it."
        )


def test_registering_a_renderer_without_its_guidance_is_half_a_wiring() -> None:
    """A tool the prompt never mentions is a schema the model pays for and
    doesn't reach for. ``show_table`` has had its own section in the router's
    instructions from the start; the other renderers arrived with none, which
    is the other half of why they never fired.

    Asserted on the ASSEMBLED instructions (gotcha 40: never on a source
    substring, which ``ruff format`` can reflow out from under you)."""
    agent = create_orchestrator(_settings())
    instructions = agent.instructions or ""
    assert visual_tools.CARD_TOOLS_HINT.strip() in instructions, (
        "The card renderers are registered on the router but CARD_TOOLS_HINT "
        "is not in its instructions — the model has four schemas and no "
        "statement of when the shape fits."
    )
    assert visual_tools.NEXT_STEPS_HINT.strip() in instructions, (
        "suggest_next_steps is registered on the router but NEXT_STEPS_HINT is "
        "not in its instructions."
    )
    # The pre-existing table guidance must survive the addition — it is the one
    # renderer whose section is written inline rather than shared.
    assert "## Answering with a table" in instructions


# ── The wider net: a card nobody registers is unreachable ───────────────


def _card_tool_names() -> dict[str, str]:
    """Every ``_queue_card("<kind>", …)`` call site in ``visual_tools``, mapped
    kind → the tool function that queues it.

    Read out of the module's own source by AST rather than hand-listed, so a
    new card is covered the day it is written (gotcha 28: read BOTH sides)."""
    import ast

    source = inspect.getsource(visual_tools)
    tree = ast.parse(source)
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "_queue_card"
                and inner.args
                and isinstance(inner.args[0], ast.Constant)
                and isinstance(inner.args[0].value, str)
            ):
                # ``_show_x_impl`` bodies are called by the ``@function_tool``
                # wrapper of the same stem; normalise to the public tool name.
                name = node.name.removeprefix("_").removesuffix("_impl")
                out[inner.args[0].value] = name
    return out


def test_every_card_kind_has_a_tool_that_queues_it() -> None:
    """Each card ``EventType`` is produced by some tool. A member with no
    producer is a card the model can never cause — the inverse of gotcha 28's
    missing enum member, where a producer had no member and killed the run."""
    card_kinds = {
        EventType.DETAIL_CARD.value,
        EventType.CHART.value,
        EventType.SCHEDULE.value,
        EventType.CHECKLIST.value,
        EventType.NEXT_STEPS.value,
    }
    produced = set(_card_tool_names())
    assert card_kinds <= produced, (
        f"card EventType members with no tool that queues them: {sorted(card_kinds - produced)}"
    )


def test_every_card_tool_is_registered_on_some_agent(router_tools: set[str]) -> None:
    """A card tool no agent carries is dead code wearing a feature's clothes —
    it is the state all four of these were in, one agent short of visible.

    Reads the real factories. ``calendar_agent`` needs a Composio tool to
    register anything at all (it ships zero tools when the toolkit is
    unconnected), so it is built connected here."""
    settings = _settings()
    registered = (
        router_tools
        | _tool_names(create_buildium_agent(settings))
        | _tool_names(create_calendar_agent(settings, calendar_tools=[MagicMock()]))
    )
    card_tools = set(_card_tool_names().values())
    unreachable = sorted(t for t in card_tools if t not in registered)
    assert not unreachable, (
        f"card tools registered on NO agent, so the model can never call them: {unreachable}"
    )


def test_the_card_tool_scan_found_something() -> None:
    """Guard the scan. A wrong AST shape or a renamed helper collects zero call
    sites, and both tests above then pass against an empty set — gotcha 45's
    'a control whose failure mode is silently permits more'."""
    found = _card_tool_names()
    assert len(found) >= 5, f"the _queue_card scan found only {found!r}"
    assert found.get("chart") == "show_chart", found
    assert found.get("next_steps") == "suggest_next_steps", found
