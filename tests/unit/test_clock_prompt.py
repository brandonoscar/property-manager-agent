"""The current date reaches every specialist's prompt, and it is the real one.

Live-found 2026-08-23. Three role-matrix probes carrying a relative date never
reached the tool under test: the agent stopped to ask what today was, because
`none of my tools expose the system clock directly`. That was accurate — no
clock tool exists — while the orchestrator's routing prompt told it not to ask
because "today's date is always available to the specialists via their tools".

⚠ Every assertion here pins an INJECTED clock, never `datetime.now()`. A test
that computes today the same way the code does compares two identical
expressions and agrees with itself; it passes just as happily against a prompt
that says nothing at all, which is the state this file exists to prevent.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

from helixis.agents.clock_prompt import (
    CURRENT_DATE_MARKER,
    current_date_block,
    with_current_date,
)

_REPO = Path(__file__).resolve().parents[2]
_AGENTS = _REPO / "src" / "helixis" / "agents"

#: A Sunday, chosen so the weekday is checkable and is not today.
_FIXED = datetime(2026, 8, 23, 14, 5, tzinfo=UTC)


def test_the_block_states_the_injected_date_not_the_real_one() -> None:
    block = current_date_block(now=_FIXED, tz_name="UTC")
    assert "2026-08-23" in block
    assert "Sunday" in block
    assert "23 August 2026" in block
    assert datetime.now(UTC).strftime("%Y-%m-%d") not in block or _FIXED.strftime(
        "%Y-%m-%d"
    ) == datetime.now(UTC).strftime("%Y-%m-%d")


def test_the_timezone_is_named_because_today_is_local() -> None:
    """A move-out dated 2026-09-30 is a different lease month depending which
    side of midnight you compute from, so the zone is part of the fact."""
    assert "in UTC" in current_date_block(now=_FIXED, tz_name="UTC")


def test_an_unavailable_zone_still_yields_a_date_and_says_so() -> None:
    """A host with no tzdata must not cost the agent its date. The fallback is
    ``datetime.UTC``, not ``ZoneInfo("UTC")`` — both fail on such a host, and a
    fallback that raises the error it is catching is not a fallback."""
    block = current_date_block(now=_FIXED, tz_name="Not/AZone")
    assert "2026-08-23" in block
    assert "unavailable" in block, "the substitution must be visible, not silent"


def test_the_block_forbids_asking_the_user() -> None:
    block = current_date_block(now=_FIXED, tz_name="UTC")
    assert "NEVER ask the user what today's date is" in block
    for phrase in ("next month", "tomorrow", "today"):
        assert phrase in block, "the relative forms it must resolve are named"


def test_with_current_date_appends_rather_than_replaces() -> None:
    out = with_current_date("ORIGINAL PROMPT", now=_FIXED, tz_name="UTC")
    assert out.startswith("ORIGINAL PROMPT")
    assert CURRENT_DATE_MARKER in out


def test_every_agent_construction_injects_the_date() -> None:
    """The structural pin. A NEW specialist that builds an ``Agent`` without
    routing its instructions through ``with_current_date`` fails here even if
    no test exercises its path — which is exactly where this defect hid: the
    calendar agent has a clock tool, so the gap was invisible until a probe
    asked a different specialist for a relative date.
    """
    offenders: list[str] = []
    for path in sorted(_AGENTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in [n for n in ast.walk(tree) if isinstance(n, ast.Call)]:
            if getattr(call.func, "id", None) != "Agent":
                continue
            kw = next((k for k in call.keywords if k.arg == "instructions"), None)
            if kw is None:
                offenders.append(f"{path.name}:{call.lineno} Agent(...) with no instructions")
                continue
            names = {
                n.func.id
                for n in ast.walk(kw.value)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
            if "with_current_date" not in names:
                offenders.append(
                    f"{path.name}:{call.lineno} instructions= misses with_current_date"
                )
    assert not offenders, (
        "every Agent must be constructed with the current date in its prompt; "
        "there is no clock tool to fall back on:\n  " + "\n  ".join(offenders)
    )


def test_the_orchestrator_no_longer_claims_a_clock_tool_exists() -> None:
    src = (_AGENTS / "orchestrator.py").read_text(encoding="utf-8")
    assert "always available to the specialists via their tools" not in src, (
        "the routing prompt asserted a clock tool that does not exist, which is "
        "why the agent both refused to ask AND could not answer"
    )
