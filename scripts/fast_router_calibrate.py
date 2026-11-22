"""Is the fast router safe to switch on? Measure it, don't reason about it.

``render.yaml`` enables ``HELIXIS_AGENT__FAST_ROUTER`` with a comment asking
for exactly this and nothing ever ran it:

    ⚠ Calibration: validate the 03x routing golden cases hold under live
    Anthropic before trusting it broadly (the "8× fail-open reps").

⚠ THE GOLDEN SUITE CANNOT DO THIS. ``evals/orchestrator_routing.py`` calls
``create_orchestrator()`` → ``Runner.run()`` directly, while
``fast_router.route()`` runs inside ``run_agent_streaming``. So flipping the
flag and re-running the 03x cases exercises the router NOT AT ALL and reports
green either way — gotcha 66's shape, where a binding that lives only in the
production path is inert in the harness that is supposed to measure it. This
script calls ``route()`` itself, which is the only way to see its verdicts.

WHAT COUNTS AS SAFE, derived from each case rather than hardcoded:

* ``ORCHESTRATOR`` is ALWAYS acceptable. That is the fail-open design — the
  router may only ever save a hop, never change where a question lands.
* ``BUILDIUM`` is acceptable ONLY where the case already expects
  ``buildium_agent``. Anywhere else it is a misroute that the turn cannot
  recover from: on that verdict the specialist REPLACES the orchestrator for
  the whole turn, so a wrong answer is the entire answer.
* ``ANALYST`` likewise, only where the case expects ``analysis_agent``.

Deriving it from ``expected`` rather than a hand-written table means a new 03x
case is covered the day it lands, and a changed expectation cannot silently
disagree with this file (gotcha 48).

⚠ RUN IT MORE THAN ONCE PER CASE. The router is a model, so a single clean
pass proves very little: 037 sat at a 20% misroute for weeks while reading as
"1 fail / 0 flake over 3 run(s)" — at n=3 a one-in-five rate and a fluke are
indistinguishable. Default is 5 reps; use --reps 10 before actually flipping
the flag in production.

Usage (needs HELIXIS_LLM__ANTHROPIC_API_KEY; costs ~1 cheap call per rep):

    python scripts/fast_router_calibrate.py
    python scripts/fast_router_calibrate.py --reps 10 --pattern '0[35]'

Exit 0 safe · 1 a misroute OR an inert router · 2 could not run. A run that
did not happen is never a pass.

⚠ SAFE AND INERT ARE DIFFERENT ANSWERS. Every fail-open verdict counts as
safe — correctly, that is the design — so a router that classified NOTHING
scores a clean sweep while buying zero saved hops. Measured 2026-09-03: a
10-rep sweep returned 100/100 ``orchestrator``, which is indistinguishable
from a dead router on verdicts alone. So this also checks the flag actually
took inside the process, counts real classifications against fail-open
errors by watching ``route()``'s own log lines, and reports SAFE BUT SAVES
NOTHING separately from SAFE.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GOLDEN = REPO / "src" / "helixis" / "evals" / "golden"

#: Specialist a fast-route verdict hands the whole turn to.
_VERDICT_AGENT = {"buildium": "buildium_agent", "analyst": "analysis_agent"}


def _expected_targets(case: dict) -> set[str]:
    exp = case.get("expected") or {}
    one = exp.get("must_route_to")
    many = exp.get("must_route_to_one_of") or []
    return {t for t in ([one] if isinstance(one, str) else []) + list(many) if isinstance(t, str)}


def _cases(pattern: str) -> list[tuple[str, str, set[str]]]:
    out = []
    for path in sorted(GOLDEN.glob("*.json")):
        if not re.match(pattern, path.name):
            continue
        case = json.loads(path.read_text(encoding="utf-8"))
        if case.get("case_type") != "orchestrator_routing":
            continue
        msg = (case.get("input") or {}).get("user_message") or ""
        if msg:
            out.append((path.name[:3], msg, _expected_targets(case)))
    return out


def _contract_write_verbs() -> set[str]:
    """The write verbs the router's OWN prompt promises to send to the
    orchestrator — parsed from it, never copied.

    ``_ROUTER_SYSTEM_PROMPT`` says: "ANY action or write (create, update,
    close, assign, send, remind, draft, schedule)". Reading them out of the
    constant means editing that list edits this check too, instead of the two
    drifting until one of them is wrong (gotcha 28's both-sides rule).
    """
    from helixis.agents.fast_router import _ROUTER_SYSTEM_PROMPT

    m = re.search(r"ANY action or write \(([^)]+)\)", _ROUTER_SYSTEM_PROMPT)
    if not m:  # pragma: no cover — the prompt was reworded
        raise SystemExit(
            "could not find the write-verb list in _ROUTER_SYSTEM_PROMPT — this "
            "check parses it rather than duplicating it, so reword carefully."
        )
    return {v.strip().lower() for v in m.group(1).split(",") if v.strip()}


def _is_safe(verdict: str, expected: set[str], message: str, write_verbs: set[str]) -> bool:
    """Two independent bounds; a specialist verdict must clear BOTH.

    1. The CASE — a specialist verdict has to be where the case already
       expects the turn to land. ``orchestrator`` is always fine: that is the
       fail-open design, and the router may only ever save a hop.
    2. The ROUTER'S OWN CONTRACT — it promises writes go to the orchestrator.
       036 ("Create a maintenance task.") is the case that needs this: its
       expected set permits ``buildium_agent``, so bound 1 alone would wave a
       BUILDIUM verdict through even though the router's prompt says it must
       not. Bound 1 asks "did it land somewhere the case allows"; bound 2 asks
       "did it obey what it says it does". A router breaking its own stated
       contract is worth knowing about even where the outcome is tolerable.
    """
    if verdict == "orchestrator":
        return True
    if _VERDICT_AGENT.get(verdict, "") not in expected:
        return False
    words = set(re.findall(r"[a-z]+", message.lower()))
    return not (words & write_verbs)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reps", type=int, default=5, help="runs per case (default 5)")
    ap.add_argument("--pattern", default=r"03\d", help="case-file prefix regex (default 03x)")
    args = ap.parse_args()

    if not os.environ.get("HELIXIS_LLM__ANTHROPIC_API_KEY"):
        print("HELIXIS_LLM__ANTHROPIC_API_KEY is not set — cannot measure.", file=sys.stderr)
        print("A calibration that did not run is not one that passed.", file=sys.stderr)
        return 2

    # Force the flag ON for this process regardless of deployment state: the
    # point is to measure the router BEFORE trusting it in production.
    os.environ["HELIXIS_AGENT__FAST_ROUTER"] = "true"
    from helixis.agents import fast_router
    from helixis.settings import Settings

    settings = Settings()
    cases = _cases(args.pattern)
    if not cases:
        print(f"no routing cases matched {args.pattern!r} — check the pattern.", file=sys.stderr)
        return 2

    # ⚠ PRECONDITION, and the whole run is theatre without it. `route()`
    # returns "orchestrator" at its first line when the flag is off — no model
    # call, no error, instantly. A full sweep of fail-open verdicts then reads
    # as SAFE while nothing was ever classified. Measured 2026-09-03: a 10-rep
    # sweep came back 100/100 orchestrator, which is indistinguishable from a
    # dead router unless this is checked.
    if not settings.agent.fast_router:
        print(
            "HELIXIS_AGENT__FAST_ROUTER did not take inside this process, so "
            "route() short-circuits and every verdict would be a no-op.",
            file=sys.stderr,
        )
        return 2

    # `route()` swallows its own failures by design. The INFO line fires only
    # on a real classification and the WARNING only on a fail-open error, so
    # counting them is the only way to tell "judged cautiously" from "never
    # ran" — the distinction this script exists to make.
    decided = 0
    errored = 0

    class _Probe(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            nonlocal decided, errored
            msg = record.getMessage()
            if "fast_router decision=" in msg:
                decided += 1
            elif "classification failed" in msg:
                errored += 1

    probe = _Probe()
    router_log = logging.getLogger("helixis.agents.fast_router")
    router_log.addHandler(probe)
    router_log.setLevel(logging.INFO)

    write_verbs = _contract_write_verbs()
    print(f"{len(cases)} case(s) x {args.reps} rep(s) — ORCHESTRATOR is always safe")
    print(f"router's own write verbs (parsed from its prompt): {sorted(write_verbs)}\n")
    unsafe: list[str] = []
    specialist_verdicts = 0
    for num, msg, expected in cases:
        verdicts = Counter(
            [await fast_router.route(msg, settings) for _ in range(args.reps)]  # noqa: S101
        )
        bad = {v: n for v, n in verdicts.items() if not _is_safe(v, expected, msg, write_verbs)}
        specialist_verdicts += sum(n for v, n in verdicts.items() if v != "orchestrator")
        tally = " ".join(f"{v}x{n}" for v, n in verdicts.most_common())
        if bad:
            unsafe.append(f"{num}: {bad} (expected {sorted(expected) or 'orchestrator'})")
            print(f"  MISROUTE {num}  {tally:34s} {msg[:44]}")
        else:
            print(f"  ok       {num}  {tally:34s} {msg[:44]}")

    router_log.removeHandler(probe)
    total = len(cases) * args.reps
    print(f"\nclassified {decided}/{total} · fell open on error {errored}/{total}")

    # ⚠ SAFE AND INERT ARE NOT THE SAME ANSWER, and only one of them is worth
    # acting on. Every fail-open verdict counts as safe above — correctly, that
    # is the design — so a router that classified NOTHING scores a clean sweep
    # while buying exactly zero saved hops. Report it as its own outcome rather
    # than letting it wear the word SAFE.
    if decided == 0:
        print("\nINERT — the router never classified anything.")
        print(
            f"  {errored}/{total} fell open on an ERROR (check the model id, the key, "
            f"and agent.fast_router_timeout_s={settings.agent.fast_router_timeout_s}s)."
            if errored
            else "  No errors either, which is stranger — route() returned without deciding."
        )
        print("  Enabling the flag would be free and would also do nothing. Fix this first.")
        return 1

    print()
    if unsafe:
        print("UNSAFE — do NOT enable HELIXIS_AGENT__FAST_ROUTER:")
        for line in unsafe:
            print(f"  {line}")
        print(
            "\nA specialist verdict REPLACES the orchestrator for the whole turn, "
            "so these are not recoverable detours."
        )
        return 1
    if specialist_verdicts == 0:
        # The founder's catch, 2026-09-03: a clean sweep of fail-open
        # verdicts is SAFE and buys nothing. Turning the flag on would be
        # free and pointless — the hop it exists to skip was never skipped.
        print("SAFE BUT SAVES NOTHING — the router classified every case, and")
        print("chose the orchestrator every time. Enabling the flag is risk-free")
        print("and buys no latency on this sample. If the point is speed, measure")
        print("a set where a buildium/analyst verdict is the DESIRED outcome.")
        print(f"\n⚠ n={args.reps} per case.")
        return 0
    print(f"SAFE — {specialist_verdicts} specialist verdict(s), all matching their case.")
    print(f"⚠ n={args.reps} per case. A 20% defect passes a 5-rep sample ~1 time in 3.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
