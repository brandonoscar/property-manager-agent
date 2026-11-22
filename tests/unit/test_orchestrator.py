"""Tests for orchestrator and specialist agent creation."""

from __future__ import annotations

from unittest.mock import MagicMock

from helixis.agents.buildium_agent import create_buildium_agent
from helixis.agents.calendar_agent import create_calendar_agent
from helixis.agents.drive_agent import create_drive_agent
from helixis.agents.gmail_agent import create_gmail_agent
from helixis.agents.orchestrator import _is_handoff_narration, create_orchestrator
from helixis.settings import Settings


def _test_settings() -> Settings:
    """Default to the production-shaped config.

    PR #153 — the legacy ``browser_enabled`` flag is removed. The
    Browser Agent now always registers as a handoff target and
    self-gates on Browserbase credentials at the tool layer.
    Production runs with no credentials configured by default;
    every browser tool call returns ``browser_unavailable`` until
    the operator sets ``HELIXIS_BROWSER__BROWSERBASE_API_KEY`` and
    ``HELIXIS_BROWSER__BROWSERBASE_PROJECT_ID``.
    """
    return Settings(
        env="test",
        llm={"default_model": "openai/gpt-4o", "openai_api_key": "test-key"},
        memory={"enabled": False},
    )


def test_create_buildium_agent_has_buildium_tools():
    """Buildium agent carries the Buildium tool set. It no longer carries a
    separate ``request_user_confirmation`` tool — write tools enforce the
    confirm gate inside their own bodies (confirm_or_abort), so confirmation
    is structural, not a separate model-invoked step."""
    settings = _test_settings()
    agent = create_buildium_agent(settings)
    assert agent.name == "buildium_agent"
    assert len(agent.tools) >= 2  # the buildium read/write tool set
    tool_names = {getattr(t, "name", "") for t in agent.tools}
    assert "request_user_confirmation" not in tool_names
    assert "buildium_create_to_do_task" in tool_names


def test_create_analysis_agent_has_analysis_tools():
    """Data Analyst (ADR 0007) carries schema lookup + guarded SQL +
    show_table. Registered unconditionally; the tools self-gate on
    HELIXIS_ANALYSIS__ENABLED (dark by default)."""
    from helixis.agents.analysis_agent import create_analysis_agent

    settings = _test_settings()
    agent = create_analysis_agent(settings)
    assert agent.name == "analysis_agent"
    tool_names = {getattr(t, "name", "") for t in agent.tools}
    assert tool_names == {"describe_mirror_schema", "run_analysis", "show_table"}


def test_create_calendar_agent_default_empty_tools():
    """Calendar agent has ZERO tools when no Composio tools are supplied
    (user hasn't connected the toolkit yet). Confirmation is no longer a
    per-agent tool — Composio writes are gated at the get_tools chokepoint."""
    settings = _test_settings()
    agent = create_calendar_agent(settings)
    assert agent.name == "calendar_agent"
    assert len(agent.tools) == 0


def test_create_calendar_agent_merges_composio_tools():
    settings = _test_settings()
    mock_a = MagicMock()
    mock_b = MagicMock()
    agent = create_calendar_agent(settings, calendar_tools=[mock_a, mock_b])
    # By NAME, not count — see the gmail case above.
    names = {getattr(t, "name", "") for t in agent.tools}
    assert {"show_table", "show_schedule", "suggest_next_steps"} <= names
    assert mock_a in agent.tools and mock_b in agent.tools


def test_calendar_agent_not_connected_notice_when_no_tools():
    """With no tools supplied the prompt must spell out that Google
    Calendar is not connected and route the user to Settings."""
    settings = _test_settings()
    agent = create_calendar_agent(settings)
    assert "NOT CONNECTED" in agent.instructions
    assert "Settings → Connectors → Google Calendar" in agent.instructions


def test_calendar_agent_no_not_connected_notice_when_tools_present():
    """With tools supplied the warning should NOT appear — the agent
    is fully wired and should call its tools."""
    settings = _test_settings()
    agent = create_calendar_agent(settings, calendar_tools=[MagicMock()])
    assert "NOT CONNECTED" not in agent.instructions


def test_create_gmail_agent_merges_composio_tools():
    settings = _test_settings()
    agent = create_gmail_agent(settings, gmail_tools=[MagicMock()])
    assert agent.name == "gmail_agent"
    # By NAME, not count: a count pins the size of the renderer set, which
    # grows every time one is added, and says nothing about the thing under
    # test (that the Composio tools got merged in).
    names = {getattr(t, "name", "") for t in agent.tools}
    assert {"show_table", "show_email_draft", "suggest_next_steps"} <= names


def test_gmail_agent_not_connected_notice_when_no_tools():
    settings = _test_settings()
    agent = create_gmail_agent(settings)
    assert "NOT CONNECTED" in agent.instructions
    assert "Settings → Connectors → Gmail" in agent.instructions


def test_create_outlook_agent_merges_composio_tools():
    from helixis.agents.outlook_agent import create_outlook_agent

    settings = _test_settings()
    agent = create_outlook_agent(settings, outlook_tools=[MagicMock()])
    assert agent.name == "outlook_agent"
    # 1 outlook tool + show_table (connected agents can render a table)
    assert len(agent.tools) == 2
    assert "show_table" in {getattr(t, "name", "") for t in agent.tools}


def test_outlook_agent_not_connected_notice_when_no_tools():
    from helixis.agents.outlook_agent import (
        NOT_CONNECTED_NOTICE,
        RECONNECT_NOTICE,
        create_outlook_agent,
    )

    settings = _test_settings()
    fresh = create_outlook_agent(settings)
    assert len(fresh.tools) == 0
    assert NOT_CONNECTED_NOTICE.strip()[:10] in fresh.instructions

    expired = create_outlook_agent(settings, connection_expired=True)
    assert RECONNECT_NOTICE.strip()[:10] in expired.instructions


def test_email_routing_hint_prefers_connected_provider():
    from helixis.agents.orchestrator import _email_routing_hint

    # Outlook-only → route email to Outlook, not Gmail.
    outlook_only = _email_routing_hint(gmail_connected=False, outlook_connected=True)
    assert "OUTLOOK" in outlook_only and "Outlook Agent" in outlook_only

    gmail_only = _email_routing_hint(gmail_connected=True, outlook_connected=False)
    assert "GMAIL" in gmail_only and "Gmail Agent" in gmail_only

    both = _email_routing_hint(gmail_connected=True, outlook_connected=True)
    assert "BOTH" in both

    # ⚠ USED TO ASSERT "" (empty), then #977 swung the other way and asserted
    # this branch NAME Buildium and its tools. Both were written from a story
    # about one production run; neither was measured. The second was worse.
    #
    # Case 037, 20 runs per configuration, claude-sonnet-5, 2026-09-03:
    #
    #     empty steer (pre-#977)                10% misroute to buildium_agent
    #     #977 block minus its Buildium para    60%
    #     #977 block as shipped                 75-90%
    #     this branch, naming no wrong lane      0%
    #
    # The mechanism: a routing instruction that argues for the forbidden lane
    # in concrete, tool-named detail ("consulting it before drafting is right
    # and often essential") primes the choice it exists to prevent, and the
    # closing "but never END there" does not undo it. Naming the wrong
    # destination is not free.
    #
    # ⚠ It also asked for something UNREACHABLE on this path. Routing runs the
    # handoff topology (``reasoning_loop`` absent -> False), where a handoff is
    # a one-way transfer of control: going to Buildium IS terminating in
    # Buildium. "Gather there, then hand off" only exists in tool-mode.
    #
    # So the assertions below pin the SHAPE that measured 0/20, not any
    # particular wording: say where to go, and name no lane to avoid.
    # Re-measure before changing it (``--case 037``, n>=20 -- a single run
    # cannot see a rate, which is how 20% read as "flaky" for weeks).
    neither = _email_routing_hint(gmail_connected=False, outlook_connected=False)
    assert neither, "the no-provider branch is empty again -- see the comment above"
    assert "NO EMAIL ACCOUNT IS CONNECTED" in neither
    # It must still send email work to the email specialist.
    assert "Gmail Agent" in neither
    # ⚠ THE LOAD-BEARING ONE. No wrong lane may be named in a routing steer --
    # this is the assertion that would have caught #977 before it shipped.
    for branch in (outlook_only, gmail_only, both, neither):
        assert "uildium" not in branch, (
            "a routing hint names Buildium; measured 75-90% misroute on case 037"
        )


def test_create_drive_agent_merges_composio_tools():
    settings = _test_settings()
    agent = create_drive_agent(settings, drive_tools=[MagicMock()])
    assert agent.name == "drive_agent"
    # By name, not count — the renderer set grows and the count says nothing
    # about the thing under test.
    names = {getattr(t, "name", "") for t in agent.tools}
    assert {"show_table", "suggest_next_steps"} <= names


def test_drive_agent_not_connected_notice_when_no_tools():
    settings = _test_settings()
    agent = create_drive_agent(settings)
    assert "NOT CONNECTED" in agent.instructions
    assert "Settings → Connectors → Google Drive" in agent.instructions


def test_create_orchestrator_without_composio():
    """Orchestrator works without Composio client.

    Production default: 9 specialists (buildium, calendar, gmail,
    outlook, drive, document, web, analysis, browser). PR #153 removed
    the ``browser_enabled`` flag — Browser Agent always registers and
    self-gates on Browserbase credentials; the Data Analyst (ADR 0007)
    follows the same shape, self-gating on HELIXIS_ANALYSIS__ENABLED.
    The Composio-backed specialists still register as handoff targets
    but have no Composio tools; the document, web, analysis, and
    browser specialists always register (they degrade gracefully when
    their respective keys / flags / inputs are absent).
    """
    settings = _test_settings()
    orchestrator = create_orchestrator(settings)
    assert orchestrator.name == "Occupella"
    assert len(orchestrator.handoffs) == 9
    names = {h.name for h in orchestrator.handoffs}
    assert names == {
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
    # search_memory + store_memory + show_table + send_task_to_teammate
    # (multiplayer PR B — delegation lives on the router, not a specialist)
    # + run_computation (code-exec; registered unconditionally, self-gates on
    # settings.codeexec.enabled — dark by default)
    # + check_known_state (step-zero recall, live path since 2026-08-17)
    # + contact_lookup (identity resolution, 2026-08-27 — one indexed mirror
    #   query that used to require opening a nested buildium_agent run).
    # + the eight portfolio ROLLUPS (2026-08-27). Battery v6 measured five of
    #   twelve questions rescued off the soft deadline, with 24 of the 29 tool
    #   calls across them being buildium_agent consults — nine of those on one
    #   delinquency question that delinquency_report answers in a single
    #   indexed query. Added to the router, NOT moved off the specialist.
    # No standalone confirmation tool — writes are gated at the
    # specialist/tool layer, not the orchestrator.
    #
    # ⚠ The SET is the assertion; the count is derived from it. A bare
    # `len(...) == N` reports "8 became 16" and names nothing, so the reader
    # cannot tell an intended addition from a tool that silently went missing
    # while another arrived (gotcha 40's rule: assert the thing, not a number
    # that stands in for it).
    tool_names = {getattr(t, "name", "") for t in orchestrator.tools}
    assert tool_names == {
        "search_memory",
        "store_memory",
        "show_table",
        "send_task_to_teammate",
        "contact_lookup",
        "run_computation",
        "apply_actions",
        "check_known_state",
        "portfolio_review",
        "portfolio_overview",
        "delinquency_report",
        "expiring_leases",
        "unpaid_bills",
        "spending_report",
        "open_tasks",
        # 2026-09-25: the only work-order read a Rentvine workspace has.
        "open_work_orders",
        # 2026-10-03: the only rent roll a Rentvine workspace has. Registered
        # here but visible on Rentvine runs only (``rent_roll.is_enabled``);
        # test_rent_roll.py pins that a Buildium run cannot see it.
        "rent_roll",
        "bank_balances",
        # + the card RENDERERS (2026-08-28). Measured across all 514
        #   orchestrator_runs since 2026-06-03: show_table 73 calls (on the
        #   router), suggest_next_steps 8 (specialist-only), and show_chart /
        #   show_detail_card / show_checklist / show_schedule ZERO — four
        #   features built end to end that had never rendered, because the one
        #   agent that writes the user's answer could not see them. The eight
        #   rollups above made it worse by moving those answers onto this path.
        #   show_schedule is deliberately NOT here: the router holds no
        #   time-slot data to put in one. See test_router_renders_what_it_
        #   computes.py for the pairing contract this is the census of.
        "show_detail_card",
        "show_chart",
        "show_checklist",
        "suggest_next_steps",
        # Added 2026-09-01: the card that ASKS. A vague request ("make a task
        # for the leaky faucet") was answered with a paragraph of questions,
        # which the user then answered with a second paragraph the model had
        # to take apart again. ROUTER-ONLY, unlike the renderers above: a
        # specialist that asks a question is talking past its caller — it has
        # no user, and gotcha 39 records what a message written for one reader
        # costs when it lands on another.
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
    assert len(orchestrator.tools) == len(tool_names), "a tool is registered twice"
    assert "request_user_confirmation" not in tool_names


def test_create_orchestrator_with_composio_unavailable():
    """Orchestrator works when Composio client exists but is unavailable."""
    settings = _test_settings()
    mock_composio = MagicMock()
    mock_composio.available = False

    orchestrator = create_orchestrator(
        settings, composio_client=mock_composio, company_id="test-co"
    )
    assert orchestrator.name == "Occupella"
    # get_tools should NOT be called when unavailable
    mock_composio.get_tools.assert_not_called()


def test_create_orchestrator_fetches_per_toolkit_when_composio_available():
    """Each Composio-backed specialist gets its own scoped tool fetch
    keyed by the AUTH user_id (not company_id) — Google connectors
    are personal, so per-user OAuth state is what Composio holds."""
    settings = _test_settings()
    mock_composio = MagicMock()
    mock_composio.available = True
    mock_composio.get_tools.return_value = [MagicMock()]

    orchestrator = create_orchestrator(
        settings,
        composio_client=mock_composio,
        company_id="test-co",
        composio_user_id="auth-user-42",
    )
    assert orchestrator.name == "Occupella"
    # Six per-toolkit calls. GOOGLEDOCS + GOOGLESHEETS joined 2026-08-24 and
    # feed the SAME Drive specialist — so the fetch count grew but the agent
    # count did not, which is the whole point of folding them in rather than
    # adding two more specialists (gotchas 25/32/34 on fan-out).
    assert mock_composio.get_tools.call_count == 6
    call_kwargs = [c.kwargs for c in mock_composio.get_tools.call_args_list]
    requested = {tuple(kw["toolkits"]) for kw in call_kwargs}
    assert requested == {
        ("GMAIL",),
        ("OUTLOOK",),
        ("GOOGLECALENDAR",),
        ("GOOGLEDRIVE",),
        ("GOOGLEDOCS",),
        ("GOOGLESHEETS",),
    }
    # All calls must key by the auth user_id, NOT the company_id.
    assert all(kw["user_id"] == "auth-user-42" for kw in call_kwargs)


def test_create_orchestrator_falls_back_to_company_when_user_id_omitted():
    """For backwards compatibility, omitting ``composio_user_id`` falls
    back to the company_id. New callers should always pass user_id."""
    settings = _test_settings()
    mock_composio = MagicMock()
    mock_composio.available = True
    mock_composio.get_tools.return_value = []

    create_orchestrator(settings, composio_client=mock_composio, company_id="legacy-co")
    call_kwargs = [c.kwargs for c in mock_composio.get_tools.call_args_list]
    assert all(kw["user_id"] == "legacy-co" for kw in call_kwargs)


def test_orchestrator_always_registers_browser_agent():
    """PR #153 — the ``agent.browser_enabled`` flag was removed.
    Browser Agent now always registers as a handoff target and
    self-gates on Browserbase credentials at the tool layer (when
    credentials are missing, every browser tool returns
    ``browser_unavailable`` and the prompt teaches the agent to
    surface that gracefully).

    Concretely: the three previous tests
    (``test_orchestrator_skips_browser_handoff_when_disabled``,
    ``test_orchestrator_uses_api_only_instructions_when_browser_disabled``,
    ``test_orchestrator_includes_browser_handoff_when_flag_enabled``)
    are replaced by this single invariant — Browser Agent is in the
    handoff list unconditionally, registered LAST per the spec.
    """
    settings = _test_settings()
    orchestrator = create_orchestrator(settings)
    handoff_names = [h.name for h in orchestrator.handoffs]
    assert "browser_agent" in handoff_names
    # LAST in the handoff chain — the tool of last resort. In-domain
    # specialists come first, web_agent before browser_agent (snippet
    # answers preferred over real navigation when possible).
    assert handoff_names[-1] == "browser_agent"


# ── handoff-narration guard (Langfuse trace 35d829b8) ────────────────────
#
# Gemini sometimes emits "I've transferred you to the X Agent" as a terminal
# text alongside the handoff tool-call; the run ends on it before the
# specialist runs. Persisting that reply poisons the session's replayed
# history so the next turn mimics the narration. _is_handoff_narration flags
# it so the orchestrator drops it from history (keeps real answers).


def test_handoff_narration_flags_transfer_acknowledgments() -> None:
    for text in (
        "Okay, I've transferred you to the Gmail Agent.",
        "I've transferred you to the Gmail Agent.",
        "I have handed you to the Calendar Agent.",
        "Connecting you to the Buildium Agent...",
        "Routed you to the Web Agent.",
    ):
        assert _is_handoff_narration(text), text


def test_handoff_narration_keeps_real_answers() -> None:
    # Real specialist answers must never be dropped.
    keep = [
        "Your last email is from Google (no-reply@accounts.google.com), "
        "subject 'Security alert', received 2026-06-27.",
        "You have 2 events this week: Helixis Ontology on Fri Jun 27, 12:00-1:30 PM.",
        "",
        # Long answer that merely mentions a transfer — over the length guard,
        # so it survives (we only drop short, stand-alone acknowledgments).
        "The wire transfer of $2,450 you asked about was received on June 3 and "
        "posted to the tenant ledger for unit 4B; the balance is now $0 and no "
        "late fee applies. Let me know if you want the confirmation forwarded.",
    ]
    for text in keep:
        assert not _is_handoff_narration(text), text
