"""The live tenant-isolation probe's verdict logic, and its skip-floor.

⚠ WHY THE PURE CORE IS SEPARATE. This container cannot reach ``supabase.co``
over asyncpg (gotcha 5's egress allowlist), so the CONNECTION half of the probe
cannot be exercised here at all. Putting the verdict inside the connection would
have made the whole thing untestable until the first cron — and gotcha 23 is the
record of what that costs: a credential-gated check shipped broken and stayed
red for a month because the only host that ever ran it was a cron nobody read.

So ``evaluate()`` takes the numbers and returns the sentences. The SQL that
produces those numbers was executed against helixis-test on 2026-09-04 in the
exact form the module carries (0/0/0/0, 60 of 61 tables RLS-on); the wrapper
around it is proven by the first scheduled run and not before.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from scripts.tenant_isolation_probe import _INVARIANTS_SQL, _WHAT_IT_MEANS, evaluate

_PROBE = pathlib.Path(__file__).resolve().parents[2] / "scripts/tenant_isolation_probe.py"
_WORKFLOW = pathlib.Path(__file__).resolve().parents[2] / ".github/workflows/rls-guard.yml"

_CLEAN = {
    "orphan_chat_rows": 0,
    "orphan_property_rows": 0,
    "users_in_multiple_companies": 0,
    "chat_rows_author_not_member": 0,
}


def test_the_live_state_measured_on_2026_09_04_reads_clean() -> None:
    """The baseline this probe was written against, kept as a regression.

    Measured through the Supabase MCP: 0 orphan chat rows, 0 orphan properties,
    0 users spanning two companies, 0 chat rows whose author is not a member,
    and RLS on 60 of 61 public tables. If ``evaluate`` ever calls that state a
    finding, the probe has become a false alarm — which is how a guard gets
    switched off (gotcha 52's lesson pointed at monitoring).
    """
    assert evaluate(_CLEAN, ["spatial_ref_sys"]) == []


@pytest.mark.parametrize("key", sorted(_WHAT_IT_MEANS))
def test_each_invariant_is_reported_when_violated(key: str) -> None:
    """One at a time, so a violation cannot hide behind a sibling."""
    counts = {**_CLEAN, key: 3}
    findings = evaluate(counts, [])
    assert len(findings) == 1, findings
    assert findings[0].startswith(f"{key} = 3"), findings[0]


def test_a_finding_says_what_it_means_not_just_which_counter_moved() -> None:
    """⚠ The issue lands at 2am in front of somebody who did not write this.

    ``orphan_chat_rows = 4`` is a number; "unreadable by anyone and skipped by
    the Buildium purge" is a reason to act. gotcha 42's lesson — a message
    written for one reader landing on another — applied to an alert.
    """
    finding = evaluate({**_CLEAN, "users_in_multiple_companies": 1}, [])[0]
    assert "409" in finding, (
        "the multi-company finding no longer says that three irreversible "
        "actions are ALREADY refusing for that person — which is the fact that "
        "makes it urgent rather than tidy-up"
    )
    for f in evaluate({k: 1 for k in _CLEAN}, []):
        assert len(f) > 60, f"a finding that is only a counter name: {f!r}"


def test_postgis_is_the_only_rls_exemption_and_it_is_not_a_blanket() -> None:
    """``spatial_ref_sys`` is PostGIS's coordinate reference table — extension
    owned, no tenant rows. A SECOND table appearing here is a finding, and the
    exemption must not quietly cover it."""
    assert evaluate(_CLEAN, ["spatial_ref_sys"]) == []
    findings = evaluate(_CLEAN, ["spatial_ref_sys", "helixis_something_new"])
    assert len(findings) == 1
    assert "helixis_something_new" in findings[0]
    assert "spatial_ref_sys" not in findings[0], (
        "the exemption should not be listed as part of the finding — naming it "
        "alongside a real one invites somebody to exempt both"
    )


def test_missing_credentials_exit_2_is_wired_as_a_failure_not_a_pass() -> None:
    """⚠ THE ONE THAT MATTERS FOR MONITORING, and it reads the workflow.

    Exit 2 means the probe did not run. gotcha 23 is the record of what
    treating that as clean costs: rls-guard was red from 2026-07-13 to
    2026-08-17 and nothing surfaced it, because nothing filed an issue and
    workflow-liveness tracks "ran recently", not "passed".

    A behavioural test of ``main()`` cannot see how the WORKFLOW reacts to its
    exit code, so this reads the yml (gotcha 62 — pair the behaviour with a
    guard on the call site).
    """
    yml = _WORKFLOW.read_text(encoding="utf-8")
    assert "tenant_isolation_probe" in yml, "the probe is not wired into any workflow at all"
    assert "issues: write" in yml, (
        "rls-guard cannot open an issue, so a finding fails a job nobody reads — "
        "which is exactly the state gotcha 23 documents"
    )
    assert "!= '0'" in yml or "-ne 0" in yml, (
        "the issue step must fire on ANY non-zero code. Gating on '== 1' treats "
        "exit 2 (could not run) as success, and a probe that did not run has "
        "validated nothing"
    )


def test_the_probe_returns_counts_and_never_row_content() -> None:
    """⚠ A finding travels into a PUBLIC-ish GitHub issue.

    Every invariant is phrased as ``count(*)``, so the worst an issue can carry
    is a number and a table name — never a tenant's message, name or address.
    A future invariant that selects a row would leak the thing it is meant to
    protect, so the shape is pinned here rather than trusted.
    """
    tree = ast.parse(_PROBE.read_text(encoding="utf-8"))
    sql_literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "select" in node.value
    ]
    assert sql_literals, "the SQL constants moved — this guard is now blind"
    for sql in sql_literals:
        lowered = sql.lower()
        if "from public.helixis" not in lowered and "pg_tables" not in lowered:
            continue
        assert "count(*)" in lowered or "tablename" in lowered, (
            f"a probe query selects something other than a count or a table name:\n{sql}\n"
            "Findings are published to a GitHub issue — keep them countable."
        )


def test_the_shipped_sql_is_the_sql_that_was_measured() -> None:
    """The invariants are only trustworthy because these exact queries were run
    against helixis-test. Pin the four names so a rewrite has to re-measure
    rather than inherit the claim (gotcha 48's expired-justification shape)."""
    for name in _WHAT_IT_MEANS:
        assert name in _INVARIANTS_SQL, (
            f"{name} is reported but no longer produced by the probe's SQL — "
            "the verdict and the measurement have drifted apart"
        )
