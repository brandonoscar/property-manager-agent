"""The live (handoff-path) orchestrator carries check_known_state.

The known-state channel shipped loop-mode-only (W2 slice 3), so with
HELIXIS_AGENT__REASONING_LOOP off — production's state — the orchestrator
could neither see payment promises nor active reminders: the audit's
"attention layer built, never surfaced" finding. These pin the registration
AND the prompt guidance on the live path, so neither can silently regress
to loop-only again.
"""

from __future__ import annotations

from helixis.agents.orchestrator import LIVE_KNOWN_STATE_ADDENDUM, create_orchestrator
from helixis.settings import Settings


def _live_orchestrator():
    settings = Settings(agent={"reasoning_loop": False})
    return create_orchestrator(settings)


def test_live_orchestrator_registers_check_known_state() -> None:
    agent = _live_orchestrator()
    tool_names = {getattr(t, "name", "") for t in agent.tools}
    assert "check_known_state" in tool_names, (
        "check_known_state missing from the LIVE orchestrator's tools — "
        "the known-state channel regressed to loop-mode-only"
    )


def test_live_orchestrator_prompt_explains_known_state() -> None:
    """A registered tool the prompt never mentions is dead weight — the
    instructions must tell the model when to consult it and that routing
    (handoffs) is unchanged afterwards."""
    agent = _live_orchestrator()
    instructions = agent.instructions
    assert isinstance(instructions, str)
    assert "check_known_state" in instructions
    # The addendum keeps the untrusted-data rule attached to recalled state.
    assert "untrusted" in LIVE_KNOWN_STATE_ADDENDUM
    # And explicitly preserves the handoff architecture.
    assert "hand off" in LIVE_KNOWN_STATE_ADDENDUM
