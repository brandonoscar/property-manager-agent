"""Detect browser_agent runs that ended on a notable page state WITHOUT
escalating to the user — the observability backstop for issue #156.

Background: the PR #154 browser_agent eval cases (060/061/062) were loosened
during real-LLM verification — they no longer enforce that the agent calls
``escalate_to_user`` when terminal page state warrants it, nor that the final
reply explains what happened. Issue #155 tracks the product fix; THIS is the
canary that makes the failure rate visible in Langfuse even when the evals
don't catch it.

The detection is intentionally a pure function over the run's tool trail + the
final reply, so it is unit-testable without the streaming machinery. The
orchestrator feeds it what it already tracks and emits a Langfuse event + score
when it fires (see ``run_agent_streaming``).
"""

from __future__ import annotations

# Must match ``orchestrator.AGENT_DISPLAY_NAMES["browser_agent"]``. A test
# asserts they stay in sync so a rename can't silently disable the canary.
BROWSER_AGENT_NAME = "Browser Agent"

#: The browser agent's own tools — the OTHER way to recognise a browser run,
#: and since the specialist-as-tool migration the only way that works.
#:
#: ⚠ **THIS CANARY HAD NEVER FIRED, AND THE AGENT NAME IS WHY.** Measured on
#: live `orchestrator_runs` 2026-09-10, cross-tenant deliberately because
#: handoff-vs-tool wiring is a property of the ORCHESTRATOR and not of one
#: company's books (gotcha 47): **726 runs since 2026-06-03, ZERO naming
#: "Browser Agent" anywhere, ZERO silent-terminal events.** The CONTROL is what
#: makes that evidence rather than an empty column — 127 of those runs DO carry
#: a handoff, naming Buildium Agent / Web Agent / Calendar Agent / Data
#: Analyst, so the mechanism plainly works and browser simply never appears.
#:
#: The cause is dated: handoffs were 73% of June's runs, 67% of July's, and
#: **2.7% of August's** — the `.as_tool()` migration gotcha 29 records. A
#: specialist reached as a TOOL never fires `agent_updated_stream_event`, so
#: `current_agent_name` stays "Helixis", the trail is never appended, and
#: `detect_silent_terminal_state` returns at its first line. Every trigger, ack
#: phrase and payload field below was correct the whole time and unreachable.
#:
#: Keying on the TOOL name fixes that for both wirings at once: a browser tool
#: call is a browser tool call whether a handoff or a `.as_tool()` consult made
#: it. `test_the_tool_names_match_the_browser_agents_registry` reads
#: `browser_agent.py`'s own list, so a renamed or added tool cannot silently
#: shrink the canary's reach (gotcha 28 — both sides).
BROWSER_TOOL_NAMES = frozenset(
    {
        "navigate",
        "read_page",
        "extract_text",
        "click",
        "type_into",
        "select_option",
        "submit_form",
        "screenshot",
        "escalate_to_user",
    }
)


def is_browser_run(agent_name: str, tool_trail: list[tuple[str, str]]) -> bool:
    """Did the browser agent run, under EITHER wiring?

    The agent-name arm is kept exactly as it was, so a handoff-driven run
    behaves byte-for-byte as before; the tool arm is what reaches the
    `.as_tool()` path that is 97% of production today.
    """
    if agent_name == BROWSER_AGENT_NAME:
        return True
    return any(name in BROWSER_TOOL_NAMES for name, _ in tool_trail)


_ESCALATE_TOOL = "escalate_to_user"
_READ_PAGE_TOOL = "read_page"

# Substrings a tool result carries when the agent hit a wall it should have
# escalated on. ``disallowed_domain:*`` / ``budget_exhausted:*`` are emitted as
# tags by the browser tools; the login-wall shape is inferred from a read_page
# result that looks like a sign-in form.
_TRIGGER_DISALLOWED = "disallowed_domain"
_TRIGGER_BUDGET = "budget_exhausted"
_TRIGGER_LOGIN_WALL = "login_wall"

_LOGIN_WALL_SIGNINS = ("sign in", "log in", "login", "signin")

# Phrases in the final reply that count as the agent having acknowledged the
# terminal/blocked condition to the user. If the reply contains any of these we
# do NOT flag — the user was told what happened, even if no formal escalate
# tool call was made.
_ACK_PHRASES = (
    "couldn't",
    "could not",
    "couldn’t",
    "unable",
    "can't access",
    "cannot access",
    "can’t access",
    "blocked",
    "not allowed",
    "requires you to",
    "requires a login",
    "log in",
    "sign in",
    "login wall",
    "i wasn't able",
    "i was not able",
    "ran out of",
    "reached the limit",
    "hit a limit",
    "need you to",
)


def trigger_for_output(tool_name: str, output: str) -> str | None:
    """Return the terminal-trigger code a single tool result warrants, or None.

    Pure + side-effect free so it can be tested in isolation."""
    s = (output or "").lower()
    if _TRIGGER_DISALLOWED in s:
        return _TRIGGER_DISALLOWED
    if _TRIGGER_BUDGET in s:
        return _TRIGGER_BUDGET
    if (
        tool_name == _READ_PAGE_TOOL
        and "password" in s
        and any(token in s for token in _LOGIN_WALL_SIGNINS)
    ):
        return _TRIGGER_LOGIN_WALL
    return None


def detect_silent_terminal_state(
    *,
    agent_name: str,
    tool_trail: list[tuple[str, str]],
    final_output: str,
) -> str | None:
    """Return the terminal-trigger code if a browser_agent run terminated
    silently on a notable page state, else None.

    Fires when ALL of these hold:
      1. the run's last agent was the Browser Agent,
      2. the LAST warning-bearing tool result carried a terminal trigger,
      3. ``escalate_to_user`` was NOT called after that warning, and
      4. the final reply is empty OR does not acknowledge the condition.

    ``tool_trail`` is the ordered ``(tool_name, output)`` list for the run's
    browser-agent tool calls.
    """
    if not is_browser_run(agent_name, tool_trail):
        return None

    last_warn_idx: int | None = None
    trigger: str | None = None
    for i, (name, output) in enumerate(tool_trail):
        code = trigger_for_output(name, output)
        if code is not None:
            last_warn_idx, trigger = i, code

    if trigger is None or last_warn_idx is None:
        return None

    # Escalated after the warning → the user was looped in; not silent.
    for name, _ in tool_trail[last_warn_idx + 1 :]:
        if name == _ESCALATE_TOOL:
            return None

    # Reply acknowledges the terminal condition → not silent.
    reply = (final_output or "").strip().lower()
    if reply and any(phrase in reply for phrase in _ACK_PHRASES):
        return None

    return trigger


def build_silent_terminal_payload(
    *,
    trigger: str,
    tool_trail: list[tuple[str, str]],
    final_output: str,
    company_id: str,
    session_id: str,
) -> dict[str, object]:
    """Build the Langfuse event payload for a silent-terminal-state signal.

    The reply preview is run through the identity-rule sanitizer so no tenant
    identifiers or PII land in telemetry (issue #156's explicit requirement)."""
    from helixis.security.pii import redact

    reply = final_output or ""
    return {
        "terminal_trigger": trigger,
        "tool_call_trail": [name for name, _ in tool_trail],
        "final_output_length": len(reply),
        "final_output_preview": redact(reply[:200]),
        "company_id": company_id,
        "session_id": session_id,
    }


__all__ = [
    "BROWSER_AGENT_NAME",
    "BROWSER_TOOL_NAMES",
    "build_silent_terminal_payload",
    "detect_silent_terminal_state",
    "is_browser_run",
    "trigger_for_output",
]
