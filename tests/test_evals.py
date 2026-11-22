"""Pytest entry that wires the evals harness into CI.

Always runs. The runner internally SKIPs cases whose required env
isn't set (planner_summarizer cases skip without a Gemini key,
tool_invocation cases skip when their ``requires_env`` is missing),
so CI without any secrets still exercises the deterministic
tool_invocation cases (011, 012) and asserts the runner exits clean.

⚠ THE ASSERTION MESSAGE IS THE ONLY PLACE A RED GATE RELIABLY NAMES ITSELF.
It used to read "evals runner exited with code 1", which carries nothing the
exit code did not already say — so four consecutive red runs on PR #1016 could
not be diagnosed at all. The runner's own trailing ``[gate]`` line goes to
stderr and lands in pytest's FAILURE REPORT, which the warnings summary then
pushes out of the ~12 KB the log API will return; the raw log host is refused
from a sandboxed session; and reading the step summary needs a check-run id
only a webhook delivers.

pytest's ``short test summary info`` line survives all of that: it prints this
message verbatim, two lines from the end of the run, inside every tail window
by construction. So the verdict rides here.

⚠ FIRST LINE ONLY. That summary shows one line per failure, so the headline
leads and the generic wording follows it — the reverse puts the useful half
where nothing displays it.
"""

from __future__ import annotations

import asyncio

from helixis.evals.runner import last_gate_headline, main


def test_evals_pass() -> None:
    exit_code = asyncio.run(main())
    # ``or`` rather than an unconditional join: a runner that exited before
    # reporting a verdict has no headline, and an empty lead would push the
    # only remaining information off the displayed line.
    headline = last_gate_headline() or f"evals runner exited with code {exit_code}"
    assert exit_code == 0, headline
