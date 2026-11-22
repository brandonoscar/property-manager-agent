"""A dark tool must not be reported as unreachable data.

Live run 2026-08-28 — "what property do you think is at the most risk and
why?" — was routed to the analysis specialist, ``run_analysis`` returned
``analysis_unavailable``, and the answer was:

    "The data-analysis capability isn't enabled on this deployment right now,
     so I can't query your Buildium data (occupancy, delinquency, work orders,
     financials, etc.)"

The first clause is true. **The second is false.** ``portfolio_overview``,
``delinquency_report``, ``unpaid_bills``, ``expiring_leases`` and
``buildium_open_maintenance_by_unit`` are all live mirror rollups and none is
behind ``HELIXIS_ANALYSIS__ENABLED``. The question was fully answerable; the
user was told the product cannot do something it does.

Two lessons meeting:

* **gotcha 42** — a limit of OURS reported as a fact about the SOURCE. The
  message said "this tool is off"; the model rendered it as "your data is
  unreachable". The same sentence that fixed the Drive specialist applies
  here: say the limit is ours and forbid describing the source as empty.
* **gotcha 52** — a prohibition with no permitted substitute is an instruction
  to improvise, and the model improvised in the only direction available,
  which was to generalise. Naming the alternative is what stops that.

⚠ This is a PROMPT-shaped fix and prompt fixes have failed this codebase five
times when used to bound a loop (gotchas 25/32/34/37/50). It is used here
because the failure is not a loop — it is a single sentence written without a
fact the tool could have supplied. The tool now supplies it. The structural
half (a real handback so the run does not end at the analyst) is NOT done and
is named in the commit.
"""

from __future__ import annotations

import json

import pytest

from helixis.agents.analysis_tools import _UNAVAILABLE


@pytest.fixture
def payload() -> dict[str, str]:
    return json.loads(_UNAVAILABLE)


def test_the_refusal_still_says_it_is_unavailable(payload: dict[str, str]) -> None:
    """Both directions (gotcha 38). A message that stopped signalling the
    outage would have the tool silently answer nothing."""
    assert payload["ok"] is False
    assert payload["error_kind"] == "analysis_unavailable"


def test_the_limit_is_attributed_to_us_not_to_the_data(payload: dict[str, str]) -> None:
    """gotcha 42's sentence, verbatim in intent: the cut-off is ours, and the
    reader must not report the source as empty."""
    error = payload["error"].lower()
    assert "ours" in error
    assert "do not tell the user their data cannot be queried" in error


def test_the_refusal_names_what_is_still_possible(payload: dict[str, str]) -> None:
    """gotcha 52. Without a permitted substitute the model reaches for the
    nearest adjacent thing, and here that was a false generalisation."""
    still = payload["still_available"]
    for rollup in (
        "portfolio_overview",
        "delinquency_report",
        "unpaid_bills",
        "expiring_leases",
        "buildium_open_maintenance_by_unit",
    ):
        assert rollup in still, f"{rollup} is live and unmentioned"


def test_every_named_rollup_actually_exists() -> None:
    """⚠ Reads BOTH sides (gotcha 28). A refusal that points at a tool which
    was renamed or removed sends the model somewhere that does not exist —
    strictly worse than the generalisation it replaced, and it would go
    unnoticed because the message cannot fail on its own.
    """
    from helixis.agents.buildium_tools import buildium_tools
    from helixis.agents.mirror_tools import mirror_tools

    # ⚠ BOTH registries. The first version of this guard checked only
    # ``mirror_tools`` and immediately caught the message naming
    # ``buildium_open_maintenance_by_unit``, which lives in
    # ``buildium_tools`` — the exact "points somewhere that does not exist"
    # failure it was written for, on its first run.
    registered = {t.name for t in (*mirror_tools, *buildium_tools())}
    still = json.loads(_UNAVAILABLE)["still_available"]
    named = {word.strip(".,") for word in still.split() if "_" in word}
    missing = {n for n in named if n not in registered}
    assert not missing, (
        f"the refusal points at tools that are not registered: {sorted(missing)}. "
        f"Available: {sorted(registered)}"
    )


def test_none_of_the_named_rollups_share_this_flag() -> None:
    """The claim being made is that these work WHEN analysis is off. If any of
    them were gated on the same setting the advice would be a second false
    statement in the message written to remove the first."""
    import inspect

    from helixis.agents import mirror_tools as mt

    src = inspect.getsource(mt)
    assert "analysis.enabled" not in src, (
        "a mirror rollup now reads settings.analysis.enabled — the refusal's "
        "advice may no longer hold"
    )
