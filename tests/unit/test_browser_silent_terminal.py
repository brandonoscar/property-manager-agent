"""Unit tests for the browser_agent silent-terminal-state detector (issue #156).

The detector is the observability backstop for the loosened PR #154 browser
evals: it flags runs that ended on a wall the agent should have escalated on,
without escalating and without telling the user. These tests pin the pure
decision logic; the orchestrator wiring just feeds it the run's trail.
"""

from __future__ import annotations

from helixis.observability.browser_terminal import (
    BROWSER_AGENT_NAME,
    BROWSER_TOOL_NAMES,
    build_silent_terminal_payload,
    detect_silent_terminal_state,
    trigger_for_output,
)

_BROWSER = BROWSER_AGENT_NAME


def test_drift_guard_matches_orchestrator_display_name() -> None:
    """The constant must match the orchestrator's display name or the canary
    silently never fires."""
    from helixis.agents.orchestrator import AGENT_DISPLAY_NAMES

    assert BROWSER_AGENT_NAME == AGENT_DISPLAY_NAMES["browser_agent"]


# ── trigger_for_output ────────────────────────────────────────────────────


def test_trigger_disallowed_domain() -> None:
    assert trigger_for_output("navigate", "blocked: disallowed_domain:irs.gov") == (
        "disallowed_domain"
    )


def test_trigger_budget_exhausted() -> None:
    assert trigger_for_output("read_page", "budget_exhausted:max_steps") == "budget_exhausted"


def test_trigger_login_wall_only_on_read_page() -> None:
    wall = "Username password field — please sign in to continue"
    assert trigger_for_output("read_page", wall) == "login_wall"
    # Same content from a different tool is not a login-wall signal.
    assert trigger_for_output("click", wall) is None


def test_trigger_login_wall_needs_password_and_signin() -> None:
    assert trigger_for_output("read_page", "just a password reset article") is None
    assert trigger_for_output("read_page", "please sign in") is None


def test_no_trigger_on_clean_output() -> None:
    assert trigger_for_output("read_page", "Here are the office hours: 9-5") is None


# ── detect_silent_terminal_state ──────────────────────────────────────────


def test_fires_on_disallowed_domain_with_empty_reply() -> None:
    trail = [("navigate", "disallowed_domain:irs.gov")]
    assert detect_silent_terminal_state(agent_name=_BROWSER, tool_trail=trail, final_output="") == (
        "disallowed_domain"
    )


def test_fires_on_budget_exhausted() -> None:
    trail = [("read_page", "ok"), ("read_page", "budget_exhausted:steps")]
    assert (
        detect_silent_terminal_state(agent_name=_BROWSER, tool_trail=trail, final_output="Done.")
        == "budget_exhausted"
    )


def test_fires_on_login_wall() -> None:
    trail = [("read_page", "Enter your password to sign in")]
    assert (
        detect_silent_terminal_state(
            agent_name=_BROWSER, tool_trail=trail, final_output="Here is the page."
        )
        == "login_wall"
    )


def test_no_fire_when_the_run_touched_no_browser_tool() -> None:
    """A non-browser run is still untouched — but the test that said so used
    to pin the very gate that made this canary dead code.

    It passed `agent_name="Web Agent"` with a trail containing `navigate`, and
    asserted None. That IS a browser run: `navigate` is the browser agent's
    tool and nothing else calls it. The old detector agreed only because it
    read the agent DISPLAY NAME, which stops moving the moment specialists are
    reached as tools rather than handoffs — 726 live runs, zero ever naming
    "Browser Agent" (gotcha 29's migration, and the reason the canary had
    never fired). An assertion holds a defect in place as firmly as it catches
    one (gotchas 68/74/93), so this is re-pointed at what it actually meant.
    """
    trail = [("delinquency_report", "disallowed_domain:irs.gov")]
    assert (
        detect_silent_terminal_state(agent_name="Web Agent", tool_trail=trail, final_output="")
        is None
    )


def test_a_consult_driven_browser_run_still_fires() -> None:
    """The production shape since the `.as_tool()` migration, and the whole
    point of the change: `current_agent_name` never leaves "Helixis", so the
    old gate returned None for every real browser run."""
    trail = [("navigate", "blocked: disallowed_domain:irs.gov")]
    assert (
        detect_silent_terminal_state(agent_name="Helixis", tool_trail=trail, final_output="")
        == "disallowed_domain"
    )


def test_the_handoff_path_is_unchanged() -> None:
    """The other direction. The agent-name arm is kept, not replaced, so a
    handoff-driven run behaves exactly as it did — including for a tool name
    the browser registry does not carry."""
    trail = [("some_future_browser_tool", "budget_exhausted:max_steps")]
    assert (
        detect_silent_terminal_state(agent_name=_BROWSER, tool_trail=trail, final_output="")
        == "budget_exhausted"
    )


def test_the_tool_names_match_the_browser_agents_registry() -> None:
    """BOTH SIDES (gotcha 28), read off `browser_agent.py`'s own tool list.

    The canary's reach is now this set, so a tool renamed or added there
    without updating it shrinks the canary silently — which is the failure
    mode that made it dead in the first place. Read by AST rather than by
    importing: `browser_agent` pulls the Agents SDK, and a guard that cannot
    run in every environment is one that stops being run.
    """
    import ast
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[2] / "src" / "helixis" / "agents" / "browser_agent.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    registered: set[str] = set()
    for node in ast.walk(tree):
        # The `tools: list[Any] = [...]` literal inside create_browser_agent.
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "tools":
            if isinstance(node.value, ast.List):
                registered = {e.id for e in node.value.elts if isinstance(e, ast.Name)}
    assert registered, "found no browser tool list — the AST anchor moved"
    assert registered == set(BROWSER_TOOL_NAMES), (
        "the canary's tool set and the browser agent's registry disagree.\n"
        f"  only registered: {registered - set(BROWSER_TOOL_NAMES)}\n"
        f"  only in canary:  {set(BROWSER_TOOL_NAMES) - registered}"
    )


def test_the_orchestrator_feeds_the_trail_on_a_browser_tool_call() -> None:
    """The half a behavioural test structurally cannot see, and the mutation
    battery found it ALIVE.

    `detect_silent_terminal_state` can be perfect and fire never, because the
    orchestrator decides what reaches `browser_tool_trail` — and for three
    months that decision was `current_agent_name == BROWSER_AGENT_NAME`, which
    the `.as_tool()` migration made permanently false. Reverting just that
    condition restores the dead-code state in production and left every other
    test in this file green.

    Gotchas 62/66/69 are three separate instances of a well-tested helper whose
    one production call site quietly ignored it. An AST walk, because the
    condition is now multi-line and a substring scan would match the comment
    above it that quotes the broken form (gotcha 40).
    """
    import ast
    from pathlib import Path

    tree = ast.parse(
        (
            Path(__file__).resolve().parents[2] / "src" / "helixis" / "agents" / "orchestrator.py"
        ).read_text(encoding="utf-8")
    )

    def is_trail_append(stmt: ast.stmt) -> bool:
        """A DIRECT `browser_tool_trail.append(...)` statement.

        ⚠ The first cut walked each `If`'s whole subtree, so every ENCLOSING
        `if` matched too — the outer `event_type == "run_item_stream_event"`
        branch among them — and the guard failed on the correct tree, reporting
        `assert 'BROWSER_TOOL_NAMES' in {'event_type'}`. A scan that matches
        too broadly is as useless as one that matches too little; the baseline
        being RED is what caught it.
        """
        return (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Attribute)
            and stmt.value.func.attr == "append"
            and getattr(stmt.value.func.value, "id", "") == "browser_tool_trail"
        )

    guards = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.If) and any(is_trail_append(b) for b in n.body)
    ]
    assert guards, "found no `if …: browser_tool_trail.append(...)` — the anchor moved"
    for guard in guards:
        names = {x.id for x in ast.walk(guard.test) if isinstance(x, ast.Name)}
        assert "BROWSER_TOOL_NAMES" in names, (
            "the trail is gated on the agent DISPLAY NAME alone again. That name "
            "only moves on an SDK handoff, and handoffs are ~2.5% of production "
            "runs since the .as_tool() migration — so the canary goes back to "
            "never firing, silently. Measured: 726 runs, 0 canary events."
        )


def test_no_fire_when_escalated_after_warning() -> None:
    trail = [
        ("navigate", "disallowed_domain:irs.gov"),
        ("escalate_to_user", "asked the user to take over"),
    ]
    assert (
        detect_silent_terminal_state(agent_name=_BROWSER, tool_trail=trail, final_output="") is None
    )


def test_no_fire_when_reply_acknowledges() -> None:
    trail = [("navigate", "disallowed_domain:irs.gov")]
    reply = "I couldn't access that site because it's blocked for safety."
    assert (
        detect_silent_terminal_state(agent_name=_BROWSER, tool_trail=trail, final_output=reply)
        is None
    )


def test_no_fire_when_no_warning() -> None:
    trail = [("read_page", "Office hours are 9 to 5."), ("read_page", "Phone: front desk")]
    assert (
        detect_silent_terminal_state(agent_name=_BROWSER, tool_trail=trail, final_output="") is None
    )


def test_escalate_before_warning_still_fires() -> None:
    """Escalation must come AFTER the last warning to count — an earlier
    escalate on an unrelated step doesn't excuse a later silent wall."""
    trail = [
        ("escalate_to_user", "earlier unrelated escalation"),
        ("navigate", "disallowed_domain:irs.gov"),
    ]
    assert (
        detect_silent_terminal_state(agent_name=_BROWSER, tool_trail=trail, final_output="")
        == "disallowed_domain"
    )


# ── build_silent_terminal_payload ─────────────────────────────────────────


def test_payload_shape_and_pii_redaction() -> None:
    trail = [("navigate", "disallowed_domain:irs.gov"), ("read_page", "x")]
    reply = "Contact me at john.doe@example.com about the blocked filing"
    payload = build_silent_terminal_payload(
        trigger="disallowed_domain",
        tool_trail=trail,
        final_output=reply,
        company_id="co-1",
        session_id="sess-1",
    )
    assert payload["terminal_trigger"] == "disallowed_domain"
    assert payload["tool_call_trail"] == ["navigate", "read_page"]
    assert payload["final_output_length"] == len(reply)
    assert payload["company_id"] == "co-1"
    assert payload["session_id"] == "sess-1"
    # Email must be scrubbed from the preview before it reaches telemetry.
    assert "john.doe@example.com" not in payload["final_output_preview"]


def test_payload_preview_truncates_to_200() -> None:
    reply = "a" * 500
    payload = build_silent_terminal_payload(
        trigger="budget_exhausted",
        tool_trail=[],
        final_output=reply,
        company_id="co-1",
        session_id="sess-1",
    )
    # full length recorded, preview bounded to the first 200 chars (post-redact).
    assert payload["final_output_length"] == 500
    assert len(str(payload["final_output_preview"])) <= 200
