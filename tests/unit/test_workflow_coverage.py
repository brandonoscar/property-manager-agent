"""Curated PM-workflow coverage matrix (Gate-2, priority b).

The load-bearing guard: every golden case cited by the matrix must EXIST in
``golden/``, so the sales artifact can't silently drift from reality (a renamed
or deleted case fails here). Also pins the coverage bookkeeping.
"""

from __future__ import annotations

from helixis.evals import workflow_coverage as wc


def test_no_cited_case_is_missing_from_golden() -> None:
    """Drift guard: every case a workflow points at must be a real golden file."""
    report = wc.compute_coverage()
    assert report.missing_case_refs == [], (
        f"matrix cites cases not in golden/: {report.missing_case_refs}"
    )


def test_every_workflow_has_a_valid_category_and_proof() -> None:
    cats = {"read", "write", "comms", "maintenance", "knowledge", "safety"}
    proofs = {"positive", "guard", "safety", "routing"}
    for wf in wc.WORKFLOWS:
        assert wf.category in cats, wf.key
        assert wf.proof in proofs, wf.key
        # A write workflow must be flagged so it gets the read-back column.
        if wf.proof == "guard" and wf.cases:
            assert wf.writes or wf.category == "safety", wf.key


def test_write_workflows_are_flagged_as_gaps_until_positive_flow() -> None:
    """The honest headline: Buildium writes are guarded but not proven working."""
    report = wc.compute_coverage()
    writes = [r for r in report.rows if r.workflow.writes]
    assert writes, "expected write workflows in the matrix"
    # None of the write workflows currently carries a positive flow → all are gaps.
    assert all(r.proven_gap for r in writes)


def test_judge_eligibility_is_computed_from_agent_flow() -> None:
    """The rubric judge scores agent_flow replies under --judge, so a workflow is
    judge-eligible exactly when it has an agent_flow case (auto-detected — the
    column can't drift from reality)."""
    report = wc.compute_coverage()
    assert report.judged > 0, "expected some workflows to be rubric-judge-scoreable"
    for r in report.rows:
        expected = bool(r.agent_flow_cases) or r.workflow.judged
        assert r.judge_eligible is expected, r.workflow.key
    # A pure-safety routing workflow (fair-housing is orchestrator_routing) has no
    # single reply to score — it must NOT be judge-eligible off routing cases alone.
    fh = next(r for r in report.rows if r.workflow.key == "fair_housing_comms")
    assert fh.agent_flow_cases == [] and fh.judge_eligible is False


def test_coverage_counts_are_consistent() -> None:
    report = wc.compute_coverage()
    assert report.total == len(wc.WORKFLOWS)
    assert 0 <= report.with_positive_flow <= report.with_case <= report.total
    # Safety and read workflows should be well-covered (sanity, not brittle).
    assert report.with_case >= report.total - len(report.proven_gaps)


def test_render_markdown_has_table_and_gaps_section() -> None:
    report = wc.compute_coverage()
    md = wc.render_markdown(report)
    assert "| Workflow |" in md
    assert "Proven gaps" in md
    assert "rubric-judge-scored" in md
