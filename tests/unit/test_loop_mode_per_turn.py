"""The reasoning loop is a PER-QUESTION decision, not a per-deployment one.

``settings.agent.reasoning_loop`` is a deployment-wide switch and it is ON in
production (gotcha 29 — via a Render env var the ``False`` default does not
show). So the four-step GATHER/ANALYZE/DECIDE/SYNTHESIZE contract was running
on every question including trivia, at a measured 68,549 billable tokens across
11.5 model calls per turn (2026-08-24).

``loop_mode_for`` narrows that to the questions that need it, and
``create_orchestrator(loop_mode=...)`` carries the answer down. Two properties
are worth pinning:

* the decision is **downgrade-only** — it can turn the loop off for a
  recognised one-fact lookup and can never turn it on where the deployment
  flag is off;
* nothing else in ``orchestrator.py`` reads the raw flag. That second one is
  the structural half: gotcha 25's rule is that a changed decision must be
  fixed at EVERY layer that reads it, and this decision has six readers on the
  live request path. A future edit that reaches past ``loop_mode`` for the raw
  flag would silently restore the all-or-nothing behaviour on one of them,
  which no behavioural test would notice.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from helixis.agents import orchestrator as orch
from helixis.agents.orchestrator import create_orchestrator, loop_mode_for
from helixis.settings import Settings

_ORCH_SRC = Path(inspect.getfile(orch)).read_text(encoding="utf-8")

# Sanctioned readers of the raw deployment flag. Exactly two, and they are the
# two ends of the same wire: the function that DECIDES, and the parameter
# default that means "nobody decided, use the deployment flag".
_MAY_READ_RAW_FLAG = {"loop_mode_for", "create_orchestrator"}

# A one-fact lookup ``classify_effort`` positively recognises, and a question
# it must not. Kept as literals rather than imported from effort.py so a
# widened classifier cannot silently redefine what this file is testing.
_TRIVIAL = "what's the open balance for unit 4B?"
_COMPLEX = "compare maintenance spend across my portfolio and tell me what to cut"


def _settings(*, reasoning_loop: bool, adaptive_effort: bool = True) -> Settings:
    return Settings(
        env="test",
        llm={"default_model": "openai/gpt-4o", "openai_api_key": "test-key"},
        memory={"enabled": False},
        agent={"reasoning_loop": reasoning_loop, "adaptive_effort": adaptive_effort},
    )


# ── the decision ────────────────────────────────────────────────────────────


def test_recognised_lookup_skips_the_loop() -> None:
    assert loop_mode_for(_TRIVIAL, _settings(reasoning_loop=True)) is False


def test_unrecognised_question_keeps_the_loop() -> None:
    assert loop_mode_for(_COMPLEX, _settings(reasoning_loop=True)) is True


def test_the_decision_can_only_downgrade() -> None:
    """With the deployment flag off, NO question may turn the loop on.

    The asymmetry is the safety property: guessing "simple" wrong makes a hard
    question worse, so the classifier is only ever allowed to remove work.
    """
    off = _settings(reasoning_loop=False)
    for task in (_TRIVIAL, _COMPLEX, "", "   "):
        assert loop_mode_for(task, off) is False


def test_adaptive_effort_off_disables_the_downgrade() -> None:
    """``adaptive_effort`` is the kill switch for trusting ``classify_effort``.

    One classifier, one switch. If this decision kept firing while the switch
    was off, the two would disagree about whether the same question is trivial.
    """
    frozen = _settings(reasoning_loop=True, adaptive_effort=False)
    assert loop_mode_for(_TRIVIAL, frozen) is True
    assert loop_mode_for(_COMPLEX, frozen) is True


# ── the wire ────────────────────────────────────────────────────────────────

_SPECIALIST_TOOL_NAMES = {
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


def _is_loop_topology(agent: object) -> bool:
    """Loop topology = specialists as TOOLS, no handoffs. Handoff = the router."""
    tool_names = {t.name for t in agent.tools}  # type: ignore[attr-defined]
    return not agent.handoffs and bool(tool_names & _SPECIALIST_TOOL_NAMES)  # type: ignore[attr-defined]


@pytest.mark.parametrize("flag", [True, False])
def test_explicit_loop_mode_overrides_the_deployment_flag(flag: bool) -> None:
    """The per-turn answer wins in BOTH directions, or it isn't a decision."""
    settings = _settings(reasoning_loop=flag)
    assert _is_loop_topology(create_orchestrator(settings, loop_mode=True)) is True
    assert _is_loop_topology(create_orchestrator(settings, loop_mode=False)) is False


@pytest.mark.parametrize("flag", [True, False])
def test_omitting_loop_mode_is_byte_for_byte_todays_behaviour(flag: bool) -> None:
    """Every caller that does not pass ``loop_mode`` is unaffected by this change."""
    built = create_orchestrator(_settings(reasoning_loop=flag))
    assert _is_loop_topology(built) is flag


# ── the structural pin ──────────────────────────────────────────────────────


def _raw_flag_readers(source: str) -> set[str]:
    """Names of the top-level functions that read ``settings.agent.reasoning_loop``.

    Matches the ATTRIBUTE CHAIN, not the substring: ``reasoning_loop_deadline_s``
    and ``reasoning_loop_specialist_max_turns`` are different settings that the
    loop branch legitimately reads, and a substring scan would flag every one
    of them. (Same mistake the confirm-label guard made against
    ``_lease_unit_label`` — a scan that matches too much manufactures findings
    and gets deleted by the next reader.)
    """
    tree = ast.parse(source)
    found: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Attribute)
                and inner.attr == "reasoning_loop"
                and isinstance(inner.value, ast.Attribute)
                and inner.value.attr == "agent"
            ):
                found.add(node.name)

    return found


def test_only_the_decision_seam_reads_the_raw_flag() -> None:
    readers = _raw_flag_readers(_ORCH_SRC)
    unexpected = readers - _MAY_READ_RAW_FLAG

    assert not unexpected, (
        "these functions read settings.agent.reasoning_loop directly instead of "
        f"the per-turn loop_mode: {sorted(unexpected)}. The flag is a "
        "deployment-wide switch; whether a question needs the loop is a "
        "per-question property. Take loop_mode_for()'s answer and thread it "
        "through — a reader that reaches past it restores the all-or-nothing "
        "cost on that one path, silently."
    )
    assert readers, "scan found no readers at all — the pattern stopped matching"


def test_the_guard_catches_a_raw_read() -> None:
    """Self-test: the scan must fail against the defect it exists to catch.

    Without this, a scan whose pattern quietly stops matching passes forever
    and proves nothing — the failure mode that let two golden cases run zero
    times (gotcha 35).
    """
    offender = "\n".join(
        [
            "def run_agent_streaming():",
            "    if settings.agent.reasoning_loop:",
            "        pass",
        ]
    )
    assert _raw_flag_readers(offender) == {"run_agent_streaming"}

    # …and must NOT fire on the sibling settings the loop branch really uses.
    sibling = "\n".join(
        [
            "def run_agent_streaming():",
            "    deadline = settings.agent.reasoning_loop_deadline_s",
            "    turns = settings.agent.reasoning_loop_specialist_max_turns",
        ]
    )
    assert _raw_flag_readers(sibling) == set()
