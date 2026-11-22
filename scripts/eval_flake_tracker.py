#!/usr/bin/env python3
"""Eval-stability tracker — run the golden sweep N times and surface cases that
are FAILING or getting FLAKY, before a one-off flake red-gates an unrelated PR
(case 070 did exactly that on #336).

The runner already re-rolls a quality-only failure up to a cap and prints
``[note] <case> passed after K re-roll(s) (quality flake)`` when it recovers —
so we don't disable re-rolls; we just count, per case across N runs:
  * hard FAILs (and whether any was kind=safety — that's serious), and
  * flake events (needed a re-roll to pass).

A case is flagged when it hard-fails at all, or needs re-rolls in too large a
share of runs (it's trending toward a red gate). The pure parse/aggregate/flag
functions are unit-tested; ``main()`` is the subprocess glue + JSON output the
nightly workflow consumes.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

# The golden corpus, read straight off disk. Deliberately NOT via
# ``helixis.evals.runner._load_cases``: this script runs as a bare subprocess
# in CI and must not drag the agents SDK into its own process just to list
# filenames. ``test_eval_flake_tracker`` compares the two sides so they cannot
# drift apart (gotcha 28's both-sides rule).
_GOLDEN_DIR = Path(__file__).resolve().parents[1] / "src" / "helixis" / "evals" / "golden"

_PASS_RE = re.compile(r"^\[PASS\]\s+(\S+)")
_FAIL_RE = re.compile(r"^\[FAIL\]\s+(\S+)\s+\(kind=(\w+)\)")
_SKIP_RE = re.compile(r"^\[SKIP\]\s+(\S+)")
_NOTE_RE = re.compile(r"^\[note\]\s+(\S+)\s+passed after")

# A case needing a re-roll in >= this share of runs is "getting flaky".
_FLAKE_SHARE = 0.4

# Kill a single sweep after this long — one hanging case (or a slower model,
# see #531) must not silently eat the whole job budget. Partial results are
# kept and the timeout itself is surfaced as a finding-grade signal.
_SWEEP_TIMEOUT_S = 1500


def parse_run(output: str) -> dict[str, dict]:
    """Parse one sweep's stdout → {case_id: {status, kind, flaked}}."""
    cases: dict[str, dict] = {}
    for line in output.splitlines():
        if m := _PASS_RE.match(line):
            cases.setdefault(m.group(1), {})["status"] = "pass"
        elif m := _FAIL_RE.match(line):
            c = cases.setdefault(m.group(1), {})
            c["status"] = "fail"
            c["kind"] = m.group(2)
        elif m := _SKIP_RE.match(line):
            cases.setdefault(m.group(1), {})["status"] = "skip"
        elif m := _NOTE_RE.match(line):
            cases.setdefault(m.group(1), {})["flaked"] = True
    return cases


def corpus_case_ids(golden_dir: Path | None = None) -> set[str]:
    """Every case id the golden corpus defines — what a COMPLETE sweep reports on.

    This exists because the tracker learns about a case only when that case
    prints a line. A case the sweep never reached prints nothing, so it is
    indistinguishable from one that passed: both are simply absent from
    ``aggregate``, and ``find_findings`` says nothing about either. With 3 of 3
    sweeps hitting the per-sweep timeout every night, that silence was covering
    a fixed ~20% tail of the corpus — and the tail is the release-safety series.

    Holding the corpus turns "we never looked" into a finding.
    """
    ids: set[str] = set()
    for path in sorted((golden_dir or _GOLDEN_DIR).glob("*.json")):
        try:
            with path.open() as f:
                case = json.load(f)
        except (OSError, json.JSONDecodeError):
            # A corpus file we cannot read is the runner's problem to report,
            # not a reason for the tracker to die before it writes its report.
            continue
        if case_id := case.get("id"):
            ids.add(case_id)
    return ids


def aggregate(runs: list[dict[str, dict]]) -> dict[str, dict]:
    """Roll up per-case stats across N parsed runs."""
    agg: dict[str, dict] = {}
    for run in runs:
        for case_id, info in run.items():
            a = agg.setdefault(
                case_id, {"runs": 0, "fails": 0, "safety_fails": 0, "flakes": 0, "skips": 0}
            )
            a["runs"] += 1
            status = info.get("status")
            if status == "fail":
                a["fails"] += 1
                if info.get("kind") == "safety":
                    a["safety_fails"] += 1
            elif status == "skip":
                a["skips"] += 1
            if info.get("flaked"):
                a["flakes"] += 1
    return agg


def find_findings(
    agg: dict[str, dict],
    *,
    flake_share: float = _FLAKE_SHARE,
    expected_ids: set[str] | None = None,
) -> list[dict]:
    """Cases worth a human's attention, worst first.

    ``expected_ids`` (the corpus) adds the class this could not previously
    express: a case that appeared in NO sweep. Without it the report is a
    picture of however much of the suite happened to run, presented as a
    picture of the suite.
    """
    findings: list[dict] = []
    for case_id in sorted(expected_ids or ()):
        if case_id in agg:
            continue
        findings.append(
            {
                "case": case_id,
                "kind": "unreached",
                "fails": 0,
                "safety_fails": 0,
                "flakes": 0,
                "runs": 0,
                "detail": (
                    f"{case_id}: never reached — no sweep got far enough to run it, "
                    "so it is neither passing nor failing, it is unmeasured"
                ),
            }
        )
    for case_id, a in agg.items():
        ran = a["runs"] - a["skips"]  # skips aren't real runs
        if ran <= 0:
            continue
        if a["safety_fails"] > 0:
            kind = "safety_fail"
        elif a["fails"] > 0:
            kind = "fail"
        elif a["flakes"] / ran >= flake_share:
            kind = "flaky"
        else:
            continue
        findings.append(
            {
                "case": case_id,
                "kind": kind,
                "fails": a["fails"],
                "safety_fails": a["safety_fails"],
                "flakes": a["flakes"],
                "runs": ran,
                "detail": (
                    f"{case_id}: {a['fails']} fail / {a['flakes']} flake over {ran} run(s)"
                    f"{' [SAFETY]' if a['safety_fails'] else ''}"
                ),
            }
        )
    # Safety first, then the coverage hole, then ordinary fails, then flaky.
    # `unreached` outranks `fail` because an unmeasured case may BE a safety
    # regression nobody has seen; it sits below a CONFIRMED safety failure,
    # which is a thing we know rather than a thing we failed to look at.
    order = {"safety_fail": 0, "unreached": 1, "fail": 2, "flaky": 3}
    findings.sort(key=lambda f: (order[f["kind"]], -f["flakes"]))
    return findings


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _run_sweep_once(timeout_s: float | None) -> tuple[str, bool]:
    """One full golden sweep → (combined output, timed_out).

    On timeout the child is killed and whatever it printed so far is parsed —
    partial coverage beats none, and the timeout itself is reported upstream.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "helixis.evals.runner"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        return _as_text(exc.stdout) + "\n" + _as_text(exc.stderr), True
    return proc.stdout + "\n" + proc.stderr, False


def main() -> int:
    ap = argparse.ArgumentParser(description="Eval-stability tracker.")
    ap.add_argument("--runs", type=int, default=3, help="how many full sweeps to run")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument(
        "--sweep-timeout",
        type=float,
        default=_SWEEP_TIMEOUT_S,
        help="seconds before a single sweep is killed (0 = no limit)",
    )
    args = ap.parse_args()

    # Progress goes to stderr: the workflow redirects stdout into flake.json,
    # so stderr is the only live signal in the Actions log. Before this, a
    # 30-minute cancellation showed a blank step (4 silent nights, Jul 2026).
    parsed: list[dict[str, dict]] = []
    sweep_timeouts = 0
    for i in range(args.runs):
        print(f"[tracker] sweep {i + 1}/{args.runs} starting", file=sys.stderr, flush=True)
        started = time.monotonic()
        output, timed_out = _run_sweep_once(args.sweep_timeout or None)
        elapsed = time.monotonic() - started
        if timed_out:
            sweep_timeouts += 1
            print(
                f"[tracker] sweep {i + 1}/{args.runs} TIMED OUT after {elapsed:.0f}s"
                " — partial results kept",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(
                f"[tracker] sweep {i + 1}/{args.runs} done in {elapsed:.0f}s",
                file=sys.stderr,
                flush=True,
            )
        parsed.append(parse_run(output))
    agg = aggregate(parsed)
    expected = corpus_case_ids()
    findings = find_findings(agg, expected_ids=expected)
    unreached = [f["case"] for f in findings if f["kind"] == "unreached"]
    if unreached:
        print(
            f"[tracker] {len(unreached)} of {len(expected)} case(s) were never reached"
            " by any sweep — coverage is partial and those cases are unmeasured,"
            " not passing",
            file=sys.stderr,
            flush=True,
        )

    if args.json:
        print(
            json.dumps(
                {
                    "runs": args.runs,
                    "sweep_timeouts": sweep_timeouts,
                    "corpus_size": len(expected),
                    "unreached": unreached,
                    "findings": findings,
                    "aggregate": agg,
                },
                indent=2,
            )
        )
    else:
        if not findings and not sweep_timeouts:
            print(f"eval-stability: OK over {args.runs} run(s) — no failing/flaky cases")
        else:
            print(f"eval-stability: {len(findings)} case(s) need attention:")
            for f in findings:
                print(f"  - [{f['kind']}] {f['detail']}")
            if sweep_timeouts:
                print(f"  - [sweep_timeout] {sweep_timeouts} sweep(s) killed at the time limit")
    # Exit 1 on any finding — a timed-out sweep counts (coverage was partial).
    return 1 if findings or sweep_timeouts else 0


if __name__ == "__main__":
    raise SystemExit(main())
