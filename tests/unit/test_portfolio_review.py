"""The flagship question must be answerable in one call.

"Give me a full portfolio review: how many properties and units we have, total
delinquency and the largest debtors, total unpaid vendor bills, all open work
orders grouped by how overdue they are, and any leases expiring in the next 90
days" is 12 of the 14 most recent wall-clock deaths and has **never once
succeeded** (measured 2026-08-24). It is the demo question at a 0% success rate.

Every rollup it needs already existed as its own bulk mirror tool, and the model
still fanned out to ``buildium_agent`` instead — 195 of 232 tool calls across
the stalled runs. That is the fourth time discoverable-tool-plus-prose failed to
bound this loop (#850 docstrings, #860 result footer, #863 TRIAGE, #866 refusal
escalation), and gotchas 25/32/34 each end with the same sentence: only a
structural stop works. The structural form of "call these five" is one tool that
calls these five.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from helixis.agents import mirror_tools as mt


@pytest.fixture
def sections(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace every section impl with a marker, recording concurrency."""
    state: dict[str, Any] = {"started": 0, "peak": 0, "order": []}

    def _make(name: str, *, fail: bool = False, delay: float = 0.01) -> Any:
        async def _impl() -> str:
            state["started"] += 1
            state["peak"] = max(state["peak"], state["started"])
            await asyncio.sleep(delay)
            state["started"] -= 1
            state["order"].append(name)
            if fail:
                raise RuntimeError(f"{name} exploded")
            return f"{name}-DATA"

        return _impl

    state["make"] = _make
    monkeypatch.setattr(
        mt,
        "_PORTFOLIO_REVIEW_SECTIONS",
        tuple((n, _make(n)) for n in ("Portfolio", "Delinquency", "Bills", "Work", "Leases")),
    )
    return state


@pytest.mark.asyncio
async def test_one_call_returns_every_section(sections: dict[str, Any]) -> None:
    out = await mt._portfolio_review_impl()
    for name in ("Portfolio", "Delinquency", "Bills", "Work", "Leases"):
        assert f"## {name}" in out
        assert f"{name}-DATA" in out


@pytest.mark.asyncio
async def test_sections_run_concurrently(sections: dict[str, Any]) -> None:
    """Sequentially these are five round trips on a question that already
    cannot meet its 240s budget. Gathered they are one."""
    await mt._portfolio_review_impl()
    assert sections["peak"] == 5, f"sections ran {sections['peak']}-at-a-time, not together"


@pytest.mark.asyncio
async def test_the_result_tells_the_model_to_stop_looking(sections: dict[str, Any]) -> None:
    """The whole point is that no further lookup is needed after this returns."""
    out = await mt._portfolio_review_impl()
    assert "Do NOT call further lookups" in out


@pytest.mark.asyncio
async def test_a_failed_section_degrades_and_says_so(
    monkeypatch: pytest.MonkeyPatch, sections: dict[str, Any]
) -> None:
    """Four of five sections is useful; an exception is the NULL reply again.

    ⚠ And the gap must be STATED. A section silently omitted reads as "nothing
    to report" — a missing result presented as an empty one, which is gotcha 26
    and the worst failure this product has.
    """
    make = sections["make"]
    monkeypatch.setattr(
        mt,
        "_PORTFOLIO_REVIEW_SECTIONS",
        (
            ("Portfolio", make("Portfolio")),
            ("Delinquency", make("Delinquency", fail=True)),
            ("Bills", make("Bills")),
        ),
    )
    out = await mt._portfolio_review_impl()

    assert "Portfolio-DATA" in out and "Bills-DATA" in out
    assert "## Delinquency" in out
    assert "MISSING" in out
    assert "do not report it as empty" in out


@pytest.mark.asyncio
async def test_every_section_failing_still_returns_a_string(
    monkeypatch: pytest.MonkeyPatch, sections: dict[str, Any]
) -> None:
    make = sections["make"]
    monkeypatch.setattr(
        mt,
        "_PORTFOLIO_REVIEW_SECTIONS",
        tuple((n, make(n, fail=True)) for n in ("A", "B")),
    )
    out = await mt._portfolio_review_impl()
    assert isinstance(out, str) and "MISSING" in out


# ── wiring (both sides, per gotcha 28) ──────────────────────────────────────


def test_the_tool_is_registered_on_the_agent() -> None:
    names = {getattr(t, "name", "") for t in mt.mirror_tools}
    assert "portfolio_review" in names, "built the tool and never registered it"


def test_the_tool_is_budgeted_as_a_mirror_read() -> None:
    """A bulk read must be memoized and never mistaken for a per-record call.

    ``call_budget`` keys on the tool NAME, so a tool added to the agent but not
    to ``_MIRROR_READS`` falls through the ledger untouched — it would not be
    memoized, and an unrecognised name is treated as a write (memo-invalidating).
    """
    from helixis.agents.call_budget import _MIRROR_READS, _is_budgeted

    assert "portfolio_review" in _MIRROR_READS
    assert _is_budgeted("portfolio_review") is True


def test_sections_point_at_real_impls() -> None:
    """The registry is the production one here — not the fixture's stand-in.

    A section naming a renamed impl fails exactly as silently as one that was
    never registered (gotcha 35: registration and RESOLUTION are separate
    checks), so this asserts the real tuple is callable and complete.
    """
    assert len(mt._PORTFOLIO_REVIEW_SECTIONS) == 5
    for heading, impl in mt._PORTFOLIO_REVIEW_SECTIONS:
        assert heading and callable(impl)

    covered = " ".join(h.lower() for h, _ in mt._PORTFOLIO_REVIEW_SECTIONS)
    for topic in ("portfolio", "delinquen", "bill", "work order", "expiring"):
        assert topic in covered, f"the review has no {topic} section"
