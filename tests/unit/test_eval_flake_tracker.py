"""Unit tests for the eval-stability tracker's pure parse/aggregate/flag logic.

Loaded via ``importlib`` (mirrors test_doc_drift_check / test_composio_diagnose)
so it works under plain ``pytest tests/`` without ``scripts`` being a package.
The subprocess/live-sweep half is exercised only by the nightly workflow.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load():
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "_helixis_eval_flake_tracker", root / "scripts" / "eval_flake_tracker.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_m = _load()
parse_run = _m.parse_run
aggregate = _m.aggregate
find_findings = _m.find_findings

_PASS_RUN = (
    "[PASS] 040_buildium_property_lookup_flow\n[PASS] 070_buildium_read_emits_cited_report\n"
)
_FLAKE_RUN = (
    "[note] 070_buildium_read_emits_cited_report passed after 1 re-roll(s) (quality flake)\n"
    "[PASS] 070_buildium_read_emits_cited_report\n"
    "[PASS] 040_buildium_property_lookup_flow\n"
)
_SAFETY_FAIL_RUN = "[FAIL] 066_submit_denied_reports_not_filed (kind=safety)\n[PASS] 040_x\n"
_QUALITY_FAIL_RUN = "[FAIL] 063_browser_answer_not_present (kind=quality)\n"
_SKIP_RUN = "[SKIP] 060_browser_denylist_hard_block — browserbase key not set\n"


def test_parse_run_reads_all_line_types():
    parsed = parse_run(
        _PASS_RUN
        + "[FAIL] 066_x (kind=safety)\n"
        + "[SKIP] 060_y — no key\n"
        + "[note] 070_buildium_read_emits_cited_report passed after 2 re-roll(s) (quality flake)\n"
    )
    assert parsed["040_buildium_property_lookup_flow"]["status"] == "pass"
    assert parsed["066_x"] == {"status": "fail", "kind": "safety"}
    assert parsed["060_y"]["status"] == "skip"
    assert parsed["070_buildium_read_emits_cited_report"]["flaked"] is True


def test_aggregate_counts_across_runs():
    agg = aggregate([parse_run(_PASS_RUN), parse_run(_FLAKE_RUN)])
    seven = agg["070_buildium_read_emits_cited_report"]
    assert seven["runs"] == 2 and seven["flakes"] == 1 and seven["fails"] == 0


def test_findings_clean_when_all_pass():
    agg = aggregate([parse_run(_PASS_RUN), parse_run(_PASS_RUN)])
    assert find_findings(agg) == []


def test_findings_flags_safety_fail_first():
    agg = aggregate([parse_run(_SAFETY_FAIL_RUN), parse_run(_QUALITY_FAIL_RUN)])
    findings = find_findings(agg)
    assert findings[0]["kind"] == "safety_fail"
    assert findings[0]["case"] == "066_submit_denied_reports_not_filed"
    assert any(f["kind"] == "fail" for f in findings)


def test_findings_flags_persistent_flake():
    # Flaked in 2 of 2 runs (share 1.0 ≥ 0.4) → flagged as flaky.
    agg = aggregate([parse_run(_FLAKE_RUN), parse_run(_FLAKE_RUN)])
    findings = find_findings(agg)
    flaky = [f for f in findings if f["kind"] == "flaky"]
    assert flaky and flaky[0]["case"] == "070_buildium_read_emits_cited_report"


def test_occasional_flake_below_threshold_not_flagged():
    # Flaked once in 3 runs (0.33 < 0.4) → tolerated, not flagged.
    agg = aggregate([parse_run(_FLAKE_RUN), parse_run(_PASS_RUN), parse_run(_PASS_RUN)])
    assert find_findings(agg) == []


def test_skips_do_not_count_as_runs():
    agg = aggregate([parse_run(_SKIP_RUN), parse_run(_SKIP_RUN)])
    # Only skips → no real runs → no findings (and no div-by-zero).
    assert find_findings(agg) == []


def test_run_sweep_once_keeps_partial_output_on_timeout(monkeypatch):
    import subprocess

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=args[0], timeout=1, output="[PASS] 001_x\n", stderr=b"late warn\n"
        )

    monkeypatch.setattr(_m.subprocess, "run", _timeout)
    output, timed_out = _m._run_sweep_once(1)
    assert timed_out is True
    # Partial stdout survives (str) and bytes stderr is decoded, not dropped.
    assert "[PASS] 001_x" in output and "late warn" in output


def test_run_sweep_once_normal_path(monkeypatch):
    class _Proc:
        stdout = "[PASS] 001_x\n"
        stderr = ""

    monkeypatch.setattr(_m.subprocess, "run", lambda *a, **k: _Proc)
    output, timed_out = _m._run_sweep_once(None)
    assert timed_out is False and "[PASS] 001_x" in output


# ── coverage holes: "never ran" must not read as "passed" ──────────────
#
# The tracker learns about a case only when that case prints a line, so a
# sweep killed at its timeout leaves the rest of the corpus silent — and
# silence was previously indistinguishable from success. With 3 of 3 sweeps
# timing out nightly against a `sorted()` name order, that silence sat over a
# fixed ~20% tail (the 41x/42x/43x release-safety series) and the report said
# nothing. These pin the distinction in BOTH directions, because a one-sided
# check passes against a version that calls every case unreached.

corpus_case_ids = _m.corpus_case_ids


def test_an_unreached_case_is_reported_rather_than_silently_omitted():
    agg = aggregate([parse_run(_PASS_RUN)])
    findings = find_findings(agg, expected_ids={"040_buildium_property_lookup_flow", "999_never"})
    unreached = [f for f in findings if f["kind"] == "unreached"]
    assert [f["case"] for f in unreached] == ["999_never"]
    assert "unmeasured" in unreached[0]["detail"]


def test_a_case_that_ran_and_passed_is_not_called_unreached():
    """The other direction. A guard that only checks the absent case passes
    happily against a version that flags the whole corpus every night."""
    agg = aggregate([parse_run(_PASS_RUN)])
    findings = find_findings(agg, expected_ids={"040_buildium_property_lookup_flow"})
    assert findings == []


def test_a_case_that_ran_and_failed_is_a_fail_not_unreached():
    agg = aggregate([parse_run(_QUALITY_FAIL_RUN)])
    findings = find_findings(agg, expected_ids={"063_browser_answer_not_present"})
    assert [f["kind"] for f in findings] == ["fail"]


def test_expected_ids_absent_keeps_the_old_behaviour_exactly():
    """No corpus supplied → byte-for-byte the pre-change report."""
    agg = aggregate([parse_run(_QUALITY_FAIL_RUN)])
    assert find_findings(agg) == find_findings(agg, expected_ids=None)


def test_a_confirmed_safety_fail_outranks_an_unmeasured_case():
    """Ordering is a claim about what to read first: something we KNOW is
    broken beats something we failed to look at, which in turn beats an
    ordinary quality fail."""
    agg = aggregate([parse_run(_SAFETY_FAIL_RUN + _QUALITY_FAIL_RUN)])
    findings = find_findings(agg, expected_ids={"999_never"})
    assert [f["kind"] for f in findings] == ["safety_fail", "unreached", "fail"]


def test_corpus_ids_are_the_same_set_the_runner_would_load():
    """BOTH sides (gotcha 28). The tracker reads the golden files itself so it
    need not import the agents SDK in CI; this is the only thing stopping the
    two spellings of "the corpus" drifting apart."""
    from helixis.evals.runner import _load_cases

    assert corpus_case_ids() == {c["id"] for c in _load_cases(None)}


def test_corpus_is_not_empty():
    """A scan that finds zero is a claim to check, not a result (gotchas
    49/100/101). An empty corpus would make every unreached check vacuous and
    the tracker would report a clean night forever."""
    assert len(corpus_case_ids()) > 100


def test_the_workflow_title_does_not_count_unreached_cases_as_failing():
    """The report's own arithmetic, which no Python test can otherwise see.

    `COUNT` is every finding. Folding unreached into "quality case(s) failing"
    is a false statement AND the wrong instruction: you tune a failing case,
    you give a coverage hole more budget. The nightly title is the one line
    most people read, so it is the one that must not lie (gotchas 35/117 —
    reporting a thing under a louder or simply wrong label).
    """
    import re

    root = Path(__file__).resolve().parents[2]
    wf = (root / ".github" / "workflows" / "eval-flake.yml").read_text()
    # Strip comment-only lines first: the block now EXPLAINS this rule in
    # prose, and a guard that cannot tell a defect from its own explanation is
    # the wrong guard (gotchas 40/59/114).
    body = "\n".join(ln for ln in wf.splitlines() if not ln.lstrip().startswith("#"))

    assign = re.search(r"QUALITY=\$\(\((.+?)\)\)", body)
    assert assign, "the workflow no longer computes a separate QUALITY count"
    expr = assign.group(1)
    assert "COUNT" in expr and "SAFETY" in expr and "UNREACHED" in expr, (
        f"QUALITY must exclude BOTH safety and unreached findings; got {expr!r}"
    )
    assert "${QUALITY}" in body, "the title must report QUALITY, not raw COUNT"
    assert "COUNT - SAFETY))" not in body, (
        "the old arithmetic is back: it labels unmeasured cases as failing"
    )
