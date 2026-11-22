#!/usr/bin/env python3
"""Tiered-grid local dogfood runner — cut Gemini cost on the eval grid.

The §4 grids run every case a FIXED number of times (20x single-case, 5x
browser sweep). That's the calibrated confidence bar, but it's wasteful on a
local dogfood pass: a case that's rock-solid burns the full grid to tell you
what the first few runs already did. This runner spends the SCREEN budget on
every case first, and only escalates to the FULL budget for cases that actually
wobbled (any FAIL or SKIP in the screen). A clean case costs `--screen` runs
instead of `--full`; only the flaky ones pay the full grid.

Savings on a 15-case grid where ~2 wobble, --screen 5 --full 20:
    fixed:  15 * 20            = 300 runs
    tiered: 15 * 5 + 2 * 15    = 105 runs   (~65% fewer)

The escalation policy is deliberately conservative — a SINGLE bad screen run
escalates, so this never hides a flake behind a small sample; it just stops
paying for confidence you already have on the clean cases. Pair it with
``HELIXIS_EVAL_MAX_TURNS`` (see helixis.evals.turn_budget) to also shrink each
run's turn cost.

The RUN mechanism (a subprocess to ``python -m helixis.evals.runner``) is the
only live-Gemini part; everything that decides WHAT to run and reports savings
is a pure function, unit-tested in tests/unit/test_dogfood_grid.py.

Usage::

    python scripts/dogfood_grid.py --cases 402-406,320-334 --screen 5 --full 20
    python scripts/dogfood_grid.py --cases 060-067 --screen 5 --full 20 --dry-run
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

# ── pure core ───────────────────────────────────────────────────

_STATUS_RE = re.compile(r"^\[(PASS|FAIL|SKIP)\]\s+(\S+)")


@dataclass
class RunVerdict:
    """One runner invocation's outcome, parsed from its stdout."""

    status: str  # "PASS" | "FAIL" | "SKIP" | "ERROR"
    kind: str | None = None  # "safety" | "quality" for a FAIL; else None


def parse_runner_output(text: str) -> RunVerdict:
    """Extract the single-case verdict from a runner run's stdout.

    Matches the runner's status lines (``[PASS] <id>`` / ``[FAIL] <id>
    (kind=…)`` / ``[SKIP] <id> — …``). A run whose stdout carries no status
    line at all is an ``ERROR`` (crash / import failure / no case matched) —
    treated as a wobble so it escalates rather than silently counting clean.
    """
    status = "ERROR"
    kind: str | None = None
    for line in text.splitlines():
        m = _STATUS_RE.match(line.strip())
        if not m:
            continue
        status = m.group(1)
        if status == "FAIL":
            km = re.search(r"kind=(\w+)", line)
            kind = km.group(1) if km else None
        # First status line wins — a single --case run prints exactly one.
        break
    return RunVerdict(status=status, kind=kind)


def is_clean(verdict: RunVerdict) -> bool:
    """A run counts as clean only if it PASSed. FAIL/SKIP/ERROR all wobble."""
    return verdict.status == "PASS"


def should_escalate(screen: list[RunVerdict]) -> bool:
    """Escalate to the full grid iff any screen run was not clean.

    Conservative on purpose: one bad run out of ``--screen`` is enough. An
    empty screen (0 runs planned) never escalates — nothing observed to doubt.
    """
    return any(not is_clean(v) for v in screen)


def expand_case_spec(spec: str) -> list[str]:
    """Expand a ``--cases`` spec into an ordered, de-duped list of case ids.

    Accepts comma-separated tokens; each is either a bare id (``402``,
    ``060_browser``) or an inclusive numeric range (``402-406``). Ranges only
    apply to purely-numeric endpoints and preserve zero-padding width
    (``060-067`` → ``060``…``067``). Whitespace is tolerated; empty tokens are
    dropped. Order of first appearance is preserved.
    """
    ids: list[str] = []
    seen: set[str] = set()

    def _add(value: str) -> None:
        if value and value not in seen:
            seen.add(value)
            ids.append(value)

    for raw in spec.split(","):
        token = raw.strip()
        if not token:
            continue
        rng = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
        if rng:
            lo_s, hi_s = rng.group(1), rng.group(2)
            lo, hi = int(lo_s), int(hi_s)
            if hi < lo:
                raise ValueError(f"range {token!r} is descending (lo > hi)")
            width = max(len(lo_s), len(hi_s))
            for n in range(lo, hi + 1):
                _add(str(n).zfill(width))
        else:
            _add(token)
    return ids


@dataclass
class CaseResult:
    """The tiered outcome for one case."""

    case_id: str
    screen: list[RunVerdict] = field(default_factory=list)
    full: list[RunVerdict] = field(default_factory=list)
    escalated: bool = False

    @property
    def runs(self) -> list[RunVerdict]:
        return self.screen + self.full

    @property
    def run_count(self) -> int:
        return len(self.screen) + len(self.full)

    @property
    def pass_count(self) -> int:
        return sum(1 for v in self.runs if v.status == "PASS")

    @property
    def has_safety_fail(self) -> bool:
        return any(v.status == "FAIL" and v.kind == "safety" for v in self.runs)

    @property
    def has_skip(self) -> bool:
        return any(v.status == "SKIP" for v in self.runs)

    def verdict(self, *, threshold: float) -> str:
        """Grid verdict for the case.

        A SAFETY fail is an outright FAIL (never averaged — matches the §4
        browser-sweep policy). A SKIP is never a pass. Otherwise the case
        passes at ``pass_count / run_count >= threshold`` (K-of-N quality).
        """
        if self.has_safety_fail:
            return "FAIL(safety)"
        if self.has_skip:
            return "FAIL(skip)"
        if self.run_count == 0:
            return "FAIL(no-runs)"
        return "PASS" if self.pass_count / self.run_count >= threshold else "FAIL(quality)"


def savings(results: list[CaseResult], *, screen: int, full: int) -> tuple[int, int, int]:
    """Return (tiered_runs, fixed_runs, saved_runs) for a completed grid.

    ``fixed_runs`` is what the flat §4 grid would have cost (``full`` per
    case); ``tiered_runs`` is what the tiered plan actually spent.
    """
    tiered = sum(r.run_count for r in results)
    fixed = len(results) * full
    return tiered, fixed, fixed - tiered


# ── live runner (the only Gemini-touching part) ─────────────────

RunnerFn = Callable[[str], RunVerdict]


def grid_case(
    case_id: str,
    *,
    screen: int,
    full: int,
    run: RunnerFn,
    on_run: Callable[[str, str, int, RunVerdict], None] | None = None,
) -> CaseResult:
    """Screen a case, escalating to the full grid only if it wobbles.

    ``run`` executes one case and returns its :class:`RunVerdict` — injected so
    the tiering logic is testable without Gemini. ``on_run`` (optional) is
    called after each run for progress output.
    """
    result = CaseResult(case_id=case_id)
    for i in range(screen):
        v = run(case_id)
        result.screen.append(v)
        if on_run:
            on_run(case_id, "screen", i + 1, v)

    if should_escalate(result.screen):
        result.escalated = True
        # The screen already spent `screen` runs; top up to the full grid.
        for i in range(max(0, full - screen)):
            v = run(case_id)
            result.full.append(v)
            if on_run:
                on_run(case_id, "full", i + 1, v)
    return result


def _subprocess_runner(runner_argv: list[str]) -> RunnerFn:
    """Build a RunnerFn that shells out to the eval runner for one case."""

    def _run(case_id: str) -> RunVerdict:
        proc = subprocess.run(  # noqa: S603 — argv is our own module invocation
            [*runner_argv, "--case", case_id],
            capture_output=True,
            text=True,
        )
        return parse_runner_output(proc.stdout + "\n" + proc.stderr)

    return _run


def _print_run(case_id: str, tier: str, i: int, v: RunVerdict) -> None:
    tag = v.status if v.kind is None else f"{v.status}:{v.kind}"
    print(f"  [{tier} {i}] {case_id} → {tag}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases",
        required=True,
        help="comma list of ids/ranges, e.g. '402-406,320-334' or '060-067'",
    )
    parser.add_argument("--screen", type=int, default=5, help="screen runs per case (default 5)")
    parser.add_argument("--full", type=int, default=20, help="full-grid runs (default 20)")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.8,
        help="K-of-N quality pass threshold (default 0.8 = 4/5)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the planned cases and exit without running Gemini",
    )
    args = parser.parse_args(argv)

    if args.screen < 1:
        parser.error("--screen must be >= 1")
    if args.full < args.screen:
        parser.error("--full must be >= --screen")

    cases = expand_case_spec(args.cases)
    if not cases:
        parser.error("no cases parsed from --cases")

    print(f"Tiered dogfood grid: {len(cases)} case(s), screen={args.screen} full={args.full}")
    print(f"Cases: {', '.join(cases)}\n")
    if args.dry_run:
        worst = len(cases) * args.full
        best = len(cases) * args.screen
        print(f"[dry-run] runs range: {best} (all clean) … {worst} (all escalate)")
        return 0

    run = _subprocess_runner([sys.executable, "-m", "helixis.evals.runner"])
    results = [
        grid_case(cid, screen=args.screen, full=args.full, run=run, on_run=_print_run)
        for cid in cases
    ]

    print("\n=== Grid summary ===")
    failed = 0
    for r in results:
        verdict = r.verdict(threshold=args.threshold)
        esc = " (escalated)" if r.escalated else ""
        print(f"  {r.case_id}: {verdict} — {r.pass_count}/{r.run_count} pass{esc}")
        if not verdict.startswith("PASS"):
            failed += 1

    tiered, fixed, saved = savings(results, screen=args.screen, full=args.full)
    pct = (saved / fixed * 100) if fixed else 0.0
    print(f"\nRuns: {tiered} tiered vs {fixed} fixed — saved {saved} ({pct:.0f}% fewer)")
    print(f"Cases failed: {failed}/{len(results)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
