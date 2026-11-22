"""Cross-source reasoning-loop topology (W1, settings.agent.reasoning_loop).

Slice 1 wires the flag + the ``create_orchestrator`` branch only:

* OFF (default) — the live path is byte-for-byte unchanged: 9 terminal
  handoffs, 3 base tools, no specialist tools. (Guards against the flag
  leaking into prod.)
* ON — specialists become callable ``.as_tool()`` tools so the orchestrator
  can consult several in one turn: 0 handoffs, 3 base tools + 9 specialist
  tools, and the gather-then-synthesize addendum in the instructions.

Telemetry/routing-eval re-keying, confirm-gate verification, and runaway
protection are later slices; this file only asserts the topology switch.
"""

from __future__ import annotations

from helixis.agents.orchestrator import REASONING_LOOP_ADDENDUM, create_orchestrator
from helixis.settings import Settings

_SPECIALIST_NAMES = {
    "buildium_agent",
    "calendar_agent",
    "gmail_agent",
    "outlook_agent",
    "drive_agent",
    "document_agent",
    "web_agent",
    "analysis_agent",
    "browser_agent",
}
# Multiplayer PR B added send_task_to_teammate to the orchestrator's own
# tools (it delegates to a teammate, so it lives on the router, not a
# specialist). Code-exec Inc.2 added run_computation (registered
# unconditionally, self-gates on settings.codeexec.enabled — dark by default).
#
# contact_lookup joined 2026-08-27. It is the one identity question — "is
# there a person called X?" — that gates almost every request naming someone,
# and it is a single indexed mirror query ("Instant (no Buildium API calls)"
# per its own docstring). Reaching it through buildium_agent cost a nested
# agent run with 74 tool schemas; one measured turn opened that run TWICE for
# the same lookup. Added to the router, NOT moved off the specialist.
#
# The eight portfolio ROLLUPS joined the same day, on measurement from battery
# v6: five of twelve questions were rescued off the soft deadline, and 24 of
# the 29 tool calls across those five were ``buildium_agent`` consults. "Which
# tenants are behind on rent, and how much in total" opened NINE of them for a
# question ``delinquency_report`` answers in one indexed query. The trade was
# measured, not assumed: ~925 tokens/turn for all eight against ~13,489 tokens
# of cache-cold prefix for ONE consult.
_ROLLUP_TOOL_NAMES = {
    "portfolio_review",
    "portfolio_overview",
    "delinquency_report",
    "expiring_leases",
    "unpaid_bills",
    "spending_report",
    "open_tasks",
    "open_work_orders",
    # 2026-10-03: a whole-portfolio answer in one call, visible on Rentvine
    # runs only (``rent_roll.is_enabled``).
    "rent_roll",
    "bank_balances",
}
#: ⚠ Per-RECORD reads that must stay OFF the router. Putting one where the
#: routing decision is made invites the N+1 at the router instead of inside a
#: specialist — gotcha 32's "removing one N+1 just promoted the next one behind
#: it". ``remember_contact_alias`` is here for a different reason: it WRITES.
_MUST_NOT_BE_ON_THE_ROUTER = {
    "property_snapshot",
    "properties_near",
    "property_hazards",
    "jurisdiction_lookup",
    "remember_contact_alias",
    # ``show_schedule`` belongs here for a third reason: the router holds no
    # time-slot data, so it could never populate the card. Its sibling
    # renderers below ARE on the router because the router holds what feeds
    # them. See tests/unit/test_router_renders_what_it_computes.py.
    "show_schedule",
}
#: The card RENDERERS, on the agent that writes the user's final answer.
#: Added 2026-08-28 after measuring all 514 ``orchestrator_runs`` since
#: 2026-06-03: ``show_chart`` / ``show_detail_card`` / ``show_checklist`` /
#: ``show_schedule`` had fired **zero** times ever, against ``show_table``'s 73
#: — the difference being which agent carried them.
_RENDERER_TOOL_NAMES = {
    "show_detail_card",
    "show_chart",
    "show_checklist",
    "suggest_next_steps",
    # Added 2026-09-01: the card that ASKS rather than renders. A vague
    # request was answered with a paragraph of questions; this puts the
    # blanks on screen instead. ROUTER-ONLY — a specialist has no user to
    # ask (gotcha 39).
    "request_details",
    # Added 2026-08-29: nothing could leave the chat. The storage last
    # mile already existed (``persist_artifacts``) and was reachable only
    # from the dark code sandbox, so serialising a table the router
    # already holds had no path out. On the ROUTER because the router is
    # the agent that writes the final answer, i.e. the one holding the
    # table worth exporting.
    "export_table",
    # Added 2026-08-29: the document store had a complete write path and
    # no read. On the ROUTER because "what does our policy say about X" is
    # a question it already tries to answer from the distilled
    # company-knowledge profile — this lets it go deeper than the summary
    # without opening a specialist.
    "company_docs_find",
}
_BASE_TOOL_NAMES = (
    {
        "search_memory",
        "store_memory",
        "show_table",
        "send_task_to_teammate",
        "contact_lookup",
        "run_computation",
        "apply_actions",
    }
    | _ROLLUP_TOOL_NAMES
    | _RENDERER_TOOL_NAMES
)


def _settings(*, reasoning_loop: bool) -> Settings:
    return Settings(
        env="test",
        llm={"default_model": "openai/gpt-4o", "openai_api_key": "test-key"},
        memory={"enabled": False},
        agent={"reasoning_loop": reasoning_loop},
    )


def test_flag_off_keeps_terminal_handoff_topology() -> None:
    """Default OFF: the proven handoff router is untouched."""
    orchestrator = create_orchestrator(_settings(reasoning_loop=False))

    # 9 terminal handoffs, browser last.
    handoff_names = [h.name for h in orchestrator.handoffs]
    assert len(handoff_names) == 9
    assert set(handoff_names) == _SPECIALIST_NAMES
    assert handoff_names[-1] == "browser_agent"

    # Base router tools + step-zero recall — specialists are handoffs here,
    # not tools. check_known_state joined the live path 2026-08-17
    # (read-only; the terminal-handoff topology itself is unchanged).
    tool_names = {getattr(t, "name", "") for t in orchestrator.tools}
    assert tool_names == _BASE_TOOL_NAMES | {"check_known_state"}


def test_flag_on_wires_specialists_as_tools() -> None:
    """ON: specialists become callable tools; no handoffs."""
    orchestrator = create_orchestrator(_settings(reasoning_loop=True))

    # No handoffs in tool-mode.
    assert list(orchestrator.handoffs) == []

    tool_names = {getattr(t, "name", "") for t in orchestrator.tools}
    # base router tools + the known-state channel (W2 slice 3) + 9 specialist tools.
    assert _BASE_TOOL_NAMES <= tool_names
    assert "check_known_state" in tool_names
    assert _SPECIALIST_NAMES <= tool_names
    assert len(orchestrator.tools) == len(_BASE_TOOL_NAMES) + 1 + len(_SPECIALIST_NAMES)


def test_flag_on_appends_reasoning_addendum() -> None:
    """ON: the gather-then-synthesize contract is taught; OFF: it isn't."""
    on = create_orchestrator(_settings(reasoning_loop=True))
    off = create_orchestrator(_settings(reasoning_loop=False))

    # instructions is a str for the orchestrator (no callable prompt here).
    assert isinstance(on.instructions, str)
    assert isinstance(off.instructions, str)
    assert REASONING_LOOP_ADDENDUM.strip() in on.instructions
    assert REASONING_LOOP_ADDENDUM.strip() not in off.instructions


def test_specialist_tools_carry_descriptions() -> None:
    """Each specialist tool advertises a non-empty description — that's what
    lets the orchestrator pick the right one (the tool-mode analogue of the
    routing prose)."""
    orchestrator = create_orchestrator(_settings(reasoning_loop=True))
    specialist_tools = [
        t for t in orchestrator.tools if getattr(t, "name", "") in _SPECIALIST_NAMES
    ]
    assert len(specialist_tools) == len(_SPECIALIST_NAMES)
    for tool in specialist_tools:
        desc = getattr(tool, "description", "")
        assert isinstance(desc, str) and desc.strip(), tool.name


# ── the portfolio rollups on the router (battery v6, 2026-08-27) ─────────


def _router_tool_names(*, reasoning_loop: bool = True) -> set[str]:
    orchestrator = create_orchestrator(_settings(reasoning_loop=reasoning_loop))
    return {getattr(t, "name", "") for t in orchestrator.tools}


def test_the_portfolio_rollups_are_reachable_without_a_specialist() -> None:
    """⚠ THE measurement this exists for. Every one of these answers a whole
    question in one indexed mirror query, and each one missing from the router
    is a question the model can only reach by opening a nested agent."""
    missing = _ROLLUP_TOOL_NAMES - _router_tool_names()
    assert not missing, (
        f"{sorted(missing)} are not on the router — the model cannot call them "
        f"where the routing decision is made, so it will consult buildium_agent "
        f"instead, at ~13,489 tokens of cold prefix per consult"
    )


def test_per_record_reads_stay_off_the_router() -> None:
    """⚠ The other half, and it is not symmetry for its own sake. A per-record
    read on the router re-creates the N+1 one level up, which is exactly how
    gotcha 32's ``property_snapshot`` finding went ('removing one N+1 just
    promoted the next one behind it'). And ``remember_contact_alias`` WRITES."""
    leaked = _MUST_NOT_BE_ON_THE_ROUTER & _router_tool_names()
    assert not leaked, (
        f"{sorted(leaked)} reached the router. A per-record read there invites "
        f"the fan-out at the routing layer; a write there skips the specialist's "
        f"gating entirely."
    )


def test_the_specialist_keeps_every_rollup_it_had() -> None:
    """⚠ ADDED, never MOVED — and this reads BOTH registries (gotcha 28).
    The 32x negative cases pin the specialist registry BY NAME, and a
    specialist that can no longer roll up mid-consult is a capability
    regression bought for nothing."""
    from helixis.agents.mirror_tools import mirror_tools

    specialist = {getattr(t, "name", "") for t in mirror_tools}
    lost = _ROLLUP_TOOL_NAMES - specialist
    assert not lost, f"{sorted(lost)} were MOVED off the specialist, not added to the router"


def test_every_rollup_on_the_router_is_budget_wrapped() -> None:
    """⚠ Without the memo this makes things WORSE, not better.

    The battery caught ``delinquency_report`` re-run 3x in one turn and 7x in
    another — same SQL, same answer, three times the wall clock. The ledger is
    a ContextVar that propagates INTO ``.as_tool()`` sub-runs, so a wrapped
    router rollup and an identical one inside a later consult share ONE memo.
    Registering these raw would hand the model eight new ways to re-run SQL the
    turn had already paid for.
    """
    from helixis.agents.call_budget import _is_budgeted

    unbudgeted = [n for n in _ROLLUP_TOOL_NAMES if not _is_budgeted(n)]
    assert not unbudgeted, (
        f"{sorted(unbudgeted)} are not recognised by the call ledger — they would "
        f"be memo-less on the router. Add them to _MIRROR_READS."
    )


def test_the_rollups_are_present_whether_or_not_loop_mode_is_on() -> None:
    """``reasoning_loop`` is set by env on Render and the repo cannot prove its
    live value (gotcha 29), so a cost fix that only lands on one branch is a
    fix that may never run."""
    for flag in (True, False):
        missing = _ROLLUP_TOOL_NAMES - _router_tool_names(reasoning_loop=flag)
        assert not missing, f"reasoning_loop={flag}: rollups missing {sorted(missing)}"
