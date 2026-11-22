"""Every agent was told "Today is … in America/New_York" whoever it worked for.

FOUND 2026-09-10 by a code-reading audit of all 73 coverage-map partials
(item 107). `with_current_date(tz_name=...)` defaulted to `DEFAULT_TIMEZONE`
and **all eleven production call sites passed no zone at all** — the eight
specialist factories, the two orchestrator branches and the fast-routed path.
So a Honolulu property manager's agent resolved "the last day of next month",
"today", "in 30 days" against New York, and stated the absolute date it used.

⚠ **THE SHARP PART IS WHOSE FIX THIS IS.** Migration 0098 added
`companies.timezone` *because this file was hardcoding the zone* — gotcha 91
quotes `clock_prompt`'s own docstring as the reason the clock matters. The rule
reached `reminders/producers`, `mirror_tools` and `email_poll`, and never
reached the agent prompt. Gotcha 25's fan-out recurring inside the fix for
gotcha 25, with gotcha 48 on top: the docstring went on asserting "the timezone
matters and is not cosmetic" while every caller took the default.

⚠ **What these tests can and cannot see.** The behaviour is exercised directly.
What that cannot see is the ORDER of the binding inside `run_agent_streaming` —
the date block is baked into each agent's instructions at construction, so a
bind that lands after `create_orchestrator` reads correctly from the variable
and changes no prompt (gotcha 39: present in the source, absent in effect).
That half is an AST check on statement order.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from helixis.agents.clock_prompt import (
    bind_run_timezone,
    current_date_block,
    reset_run_timezone,
    run_timezone,
    with_current_date,
)
from helixis.common.company_clock import DEFAULT_TIMEZONE

_ORCH = Path("src/helixis/agents/orchestrator.py")

#: A fixed instant, because a test that computes "today" the same way the code
#: does proves only that two identical expressions agree — the module's own rule.
#:
#: ⚠ **06:00 UTC IS CHOSEN BY MEASUREMENT, and my first attempt at it was
#: wrong.** The point of the pair below is that the two zones disagree about the
#: DATE; at 03:00 UTC they do not (New York is UTC-4 in September and Honolulu
#: UTC-10, so both are still on the 9th) and the "they must differ" assertion
#: was comparing two identical strings. Measured across the day: 06:00 and
#: 09:00 UTC separate them, 03:00 and 12:00 do not. Move this constant and you
#: have to re-check that, or the test passes while proving nothing.
FIXED = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)


# ── the zone reaches the prompt ──────────────────────────────────────────


def test_the_bound_zone_is_what_the_prompt_says() -> None:
    """The whole point. Before this, every prompt said America/New_York."""
    token = bind_run_timezone("Pacific/Honolulu")
    try:
        block = current_date_block(now=FIXED)
    finally:
        reset_run_timezone(token)
    assert "Pacific/Honolulu" in block
    assert "America/New_York" not in block


def test_the_bound_zone_changes_the_date_not_just_the_label() -> None:
    """⚠ The assertion that matters. A block naming the right zone while
    printing the wrong day would pass a label check and still make the agent
    resolve "the last day of next month" against the wrong month. At 06:00 UTC
    on the 10th, New York has turned over and Honolulu has not."""
    hono = bind_run_timezone("Pacific/Honolulu")
    try:
        honolulu = current_date_block(now=FIXED)
    finally:
        reset_run_timezone(hono)
    ny = bind_run_timezone("America/New_York")
    try:
        newyork = current_date_block(now=FIXED)
    finally:
        reset_run_timezone(ny)

    assert "2026-09-09" in honolulu, "Honolulu is still on the 9th at 06:00 UTC"
    assert "2026-09-10" in newyork, "New York has already turned over"
    assert honolulu != newyork, (
        "the two zones produced an identical date block — the zone is being "
        "named but not used, which is the defect wearing the fix"
    )


def test_no_binding_behaves_exactly_as_before() -> None:
    """A run with no zone bound — every test, every script, a company row that
    could not be read — must be byte-for-byte what it was. This is what makes
    the change safe to ship rather than a behaviour change for everybody."""
    assert run_timezone() == DEFAULT_TIMEZONE
    assert DEFAULT_TIMEZONE in current_date_block(now=FIXED)


def test_an_explicit_zone_still_wins() -> None:
    """The fixed-clock tests elsewhere pass a zone directly; the ContextVar must
    not quietly override a caller who said what they wanted."""
    token = bind_run_timezone("Pacific/Honolulu")
    try:
        assert "Europe/London" in current_date_block(now=FIXED, tz_name="Europe/London")
    finally:
        reset_run_timezone(token)


def test_an_empty_zone_falls_back_rather_than_breaking_the_block() -> None:
    """⚠ Same posture as `company_clock` and for the same reason: an
    unresolvable zone must cost the CUSTOMISATION, never the turn. `_run_timezone_for`
    returns None for a company we could not read, and that has to land here as
    the default rather than as an empty zone name."""
    token = bind_run_timezone("")
    try:
        assert DEFAULT_TIMEZONE in current_date_block(now=FIXED)
    finally:
        reset_run_timezone(token)


def test_with_current_date_carries_the_zone_through() -> None:
    token = bind_run_timezone("Pacific/Honolulu")
    try:
        out = with_current_date("SYSTEM PROMPT", now=FIXED)
    finally:
        reset_run_timezone(token)
    assert out.startswith("SYSTEM PROMPT")
    assert "Pacific/Honolulu" in out


def test_the_binding_survives_asyncio_to_thread() -> None:
    """⚠ THE MECHANISM THE WHOLE FIX RESTS ON, asserted rather than recalled.
    `run_agent_streaming` builds the orchestrator via `asyncio.to_thread`, and
    every specialist's date block is rendered inside that thread. If a
    ContextVar did not cross that boundary the bind would be perfectly correct
    and reach nothing."""

    async def _drive() -> str:
        token = bind_run_timezone("Pacific/Honolulu")
        try:
            return await asyncio.to_thread(current_date_block, now=FIXED)
        finally:
            reset_run_timezone(token)

    assert "Pacific/Honolulu" in asyncio.run(_drive())


# ── it is bound in the run, in the right order, and released ─────────────


def _fn(name: str) -> ast.AST:
    for node in ast.walk(ast.parse(_ORCH.read_text(encoding="utf-8"))):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is gone from orchestrator.py")


def _first_line_of_call(fn: ast.AST, names: set[str]) -> int | None:
    hits = [
        n.lineno
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", "") in names or getattr(n.func, "attr", "") in names)
    ]
    return min(hits) if hits else None


def test_the_zone_is_bound_before_any_agent_is_constructed() -> None:
    """⚠ ORDER IS LOAD-BEARING AND INVISIBLE. The date block is baked into each
    agent's `instructions` at construction. A bind placed after
    `create_orchestrator` would read back perfectly from the ContextVar and
    change not one prompt — gotcha 39's "a context marker must span the await,
    not the dispatch", arriving as statement order."""
    fn = _fn("run_agent_streaming")
    bind = _first_line_of_call(fn, {"bind_run_timezone"})
    build = _first_line_of_call(
        fn, {"create_orchestrator", "create_buildium_agent", "create_analysis_agent"}
    )
    assert bind is not None, "run_agent_streaming no longer binds the run timezone"
    assert build is not None, "no agent construction found — re-point this guard"
    assert bind < build, (
        f"the timezone is bound at line {bind}, after the first agent is built at "
        f"{build} — every prompt keeps the default zone while the variable reads "
        "correctly, so nothing fails and nothing is fixed"
    )


def test_the_binding_is_released() -> None:
    """Per-run state left bound hands one company's zone to whatever runs next
    in this context."""
    fn = _fn("run_agent_streaming")
    assert _first_line_of_call(fn, {"reset_run_timezone"}) is not None, (
        "the run timezone is never reset — the next run in this context inherits it"
    )


def test_resolving_the_zone_never_raises() -> None:
    """⚠ `get_pool()` raises outright with no pool — every test, every script,
    the browser degraded path. A turn that died because it could not look up a
    display preference would be a far worse bug than the one this fixes."""
    from helixis.agents.orchestrator import _run_timezone_for

    assert asyncio.run(_run_timezone_for("")) is None
    # A real company id with no pool initialised must degrade, not explode.
    assert asyncio.run(_run_timezone_for("co-1")) is None


# ── nothing bypasses it ──────────────────────────────────────────────────


def test_no_call_site_hardcodes_a_timezone() -> None:
    """The CORPUS check (gotcha 45 — the failure mode is "silently permits
    more"). A twelfth agent that passes `tz_name="America/New_York"` is exactly
    the defect this closed, reintroduced in one file, and the per-behaviour
    tests above cannot see it."""
    offenders: list[str] = []
    for path in Path("src/helixis").rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        if "with_current_date" not in src and "current_date_block" not in src:
            continue
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", "") not in {"with_current_date", "current_date_block"}:
                continue
            for kw in node.keywords:
                if kw.arg == "tz_name" and isinstance(kw.value, ast.Constant):
                    offenders.append(f"{path}:{node.lineno} tz_name={kw.value.value!r}")
    assert not offenders, (
        "a production call site pins a literal timezone, so that agent ignores "
        "the company's own zone:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize("agent_module", ["buildium_agent", "gmail_agent", "drive_agent"])
def test_the_specialists_still_go_through_the_one_helper(agent_module: str) -> None:
    """Reads the OTHER side (gotcha 28). The fix works by every prompt passing
    through `with_current_date`; a specialist that builds its own date line
    would be silently exempt and look identical."""
    src = Path(f"src/helixis/agents/{agent_module}.py").read_text(encoding="utf-8")
    assert "with_current_date(" in src, (
        f"{agent_module} no longer uses with_current_date, so its prompt is "
        "outside the run-timezone fix entirely"
    )
