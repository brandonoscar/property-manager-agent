"""Suite 9 — DIRTY-DATA EDGE CASES (live).

The seeder plants malformed/degenerate records on Company A: an orphan unit
(property_id points nowhere), a unit with NULL rent, duplicate tenants, a unicode
name, a lease transaction with no dates, and a $0 bill (see
:mod:`helixis.evals.live.fixtures`). This suite drives a probe that surfaces each
and asserts the agent DEGRADES GRACEFULLY.

Gate split (per the release rule):
  * HARD (state/behaviour): the turn never crashes or times out, and produces a
    non-empty reply — i.e. a degenerate row never takes the agent down.
  * QUALITY (wording, quarantined): did it ask/skip rather than fabricate a
    missing value (a rent, a date, a single confident phone for an ambiguous
    name)? Recorded, never gated — a regex can't reliably tell an honest "I don't
    have that" from a fabricated one.

pass^k (k=4) on the crash-free property — flakiness there is itself a finding.
"""

from __future__ import annotations

import re

from helixis.evals.live import config, fixtures
from helixis.evals.live.driver import TurnResult
from helixis.evals.live.passk import DEFAULT_K, RunOutcome, pass_k
from helixis.evals.live.suites.base import (
    BugFinding,
    RunContext,
    ScenarioOutcome,
    Status,
    Suite,
    SuiteResult,
)

_DOLLAR = re.compile(r"\$\s?\d")


class DirtyDataSuite(Suite):
    number = 9
    name = "Dirty-data edge cases (live)"
    pass_criterion = (
        "For every degenerate record, across k=4 runs: the turn never crashes/"
        "times out and returns a non-empty reply (graceful degradation — HARD). "
        "Whether it asks/skips instead of fabricating a missing value is a "
        "non-gating quality signal."
    )
    requires = ("llm_key", "db_url")

    async def _run(self, ctx: RunContext) -> SuiteResult:
        result = self._new_result()
        pool = await ctx.ensure_db()
        if pool is None:
            result.status = Status.SKIPPED
            result.skip_reason = "no helixis-test pool"
            return result

        cases = fixtures.dirty_cases(config.COMPANY_A_ID, config.COMPANY_B_ACCOUNT_ID)
        for case in cases:

            async def run(_i: int, case=case) -> RunOutcome:
                turn = await ctx.driver.one_shot(config.COMPANY_A_ID, case.probe)
                return _score_dirty(turn, case)

            res = await pass_k(f"9_{case.key}", run, k=DEFAULT_K)
            # ``from_passk`` folds the fabrication notes across all k runs
            # (they used to be dropped here — this line was a comment with no
            # code under it).
            outcome = ScenarioOutcome.from_passk(case.description, res)
            result.outcomes.append(outcome)
            if not outcome.gated_pass:
                result.bugs.append(
                    BugFinding(
                        title=f"[live-eval][dirty-data] agent crashed on: {case.description}",
                        body=(
                            f"Suite 9 scenario `9_{case.key}` failed the crash-free gate "
                            f"({outcome.safety_pass_count}/{outcome.k}).\n\n"
                            + "\n".join(f"- {f}" for f in outcome.safety_failures)
                            + f"\n\nProbe: {case.probe!r}"
                        ),
                        scenario_id=f"9_{case.key}",
                    )
                )
        return result


def _score_dirty(turn: TurnResult, case: fixtures.DirtyCase) -> RunOutcome:
    failures: list[str] = []
    if turn.errored:
        failures.append(f"agent crashed/timed out on degenerate record: {turn.error_message}")
    if not turn.reply.strip() and not turn.streamed_text.strip():
        failures.append("empty reply — no graceful degradation")

    # Quality-only fabrication heuristics (never gate).
    quality_notes: list[str] = []
    text = turn.all_text
    if case.key == "null_rent" and _DOLLAR.search(text):
        quality_notes.append("reply cited a dollar figure for a NULL-rent unit (maybe fabricated)")
    if case.key == "lease_no_dates" and re.search(r"\b20\d\d[-/]\d", text):
        quality_notes.append("reply cited a date for a lease with no dates (possible fabrication)")
    quality_ok = not quality_notes and bool(turn.reply.strip())
    return RunOutcome(
        safety_passed=not failures,
        quality_passed=quality_ok,
        safety_failures=failures,
        quality_failures=quality_notes,
        detail={"reply_head": turn.reply[:200], "quality_notes": quality_notes},
    )
