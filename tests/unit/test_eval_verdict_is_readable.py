"""The eval gate's verdict has to reach a human, not just the job log.

WHY THIS EXISTS. ``runner.main`` already prints a trailing ``[gate]`` line, and
its comment claims that emitting it last "puts the gating case(s) at the end of
stdout, where the tail always reaches it". That claim was measured against CI
run 2109 (PR #1016, 2026-09-15) and is false: the last 430 lines of that job log
are captured provider logging, the verdict is not among them, and a red gate on
a branch could not be named from the log at all. Two independent causes, either
sufficient — the line goes to stderr rather than the stdout its own comment
names, and ~900 seconds of captured LiteLLM logging sits between it and the end
of the job. The GitHub log API truncates to roughly 12 KB and the raw blob host
is unreachable from a sandboxed session, so reading further back is not an
option either.

``browser-sweep.yml`` solved the same problem for the sweep by writing its
per-case table to ``$GITHUB_STEP_SUMMARY`` (§4: "so the result is readable
without opening the Actions log"). The backend job never got the same, which is
gotcha 67's shape: a capability one lane has and the other was never told about.

Both halves are guarded here, because each is blind to the other's failure —
a behavioural test cannot see a verdict path that forgets to call the writer
(gotchas 62/66/69/103), and an AST guard cannot see the writer being broken.
"""

from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path

import pytest

from helixis.evals import runner
from helixis.evals.runner import (
    AssertionResult,
    CaseResult,
    _write_ci_summary,
    ci_summary_report,
    gate_headline,
    main,
)

RUNNER = Path(__file__).resolve().parents[2] / "src/helixis/evals/runner.py"


def _fail(case_id: str, reason: str, *, error: str | None = None) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        assertions=[AssertionResult(name="an_assertion", passed=False, reason=reason)],
        error=error,
    )


# --------------------------------------------------------------------------
# The report itself
# --------------------------------------------------------------------------


def test_the_report_names_the_failing_case_and_also_why() -> None:
    """Naming the case alone sends a reader to the golden file; naming the
    assertion tells them whether it is a regression or model variance, which is
    the only question a red gate actually poses."""
    out = ci_summary_report(
        verdict="gating FAILURE",
        passed=40,
        total=42,
        skipped=1,
        quarantined=[],
        infrastructure=[],
        failed=[_fail("037_email_query_routes_to_gmail", "must_not_route_to[0] != buildium_agent")],
    )
    assert "037_email_query_routes_to_gmail" in out
    assert "must_not_route_to[0] != buildium_agent" in out, (
        "the summary names the case but not the failing assertion — that is the "
        "half that distinguishes a regression from model variance"
    )


def test_a_provider_outage_is_reported_as_its_own_thing() -> None:
    """Gotcha 35: an outage must never be read as a safety regression, and a
    battery that did not run is not evidence of safety either."""
    infra = CaseResult(case_id="020_extract_lease", error="You have exceeded your usage limits")
    out = ci_summary_report(
        verdict="provider outage",
        passed=0,
        total=4,
        skipped=0,
        quarantined=[],
        infrastructure=[infra],
        failed=[],
    )
    assert "outage" in out.lower()
    assert "not evidence of safety" in out
    assert "Gating" not in out, "an outage must not render under a gating heading"


def test_a_clean_run_still_writes_a_verdict() -> None:
    """Written on green deliberately. If the summary appeared only on failure,
    an absent summary would mean either 'clean' or 'the reporting broke' — the
    ambiguity gotchas 84/93 exist to stop."""
    out = ci_summary_report(
        verdict="clean",
        passed=42,
        total=42,
        skipped=0,
        quarantined=[],
        infrastructure=[],
        failed=[],
    )
    assert out.strip(), "a green run must still say so"
    assert "clean" in out
    assert "42/42" in out


def test_a_forty_case_failure_is_capped_and_says_how_many_it_dropped() -> None:
    """A gate failing on forty cases is one global cause (gotchas 27/35); the
    first few names identify it as well as forty do. But a silent truncation
    reads as 'that was all of them', so the count has to survive."""
    rows = [_fail(f"{i:03d}_case", "boom") for i in range(40)]
    out = ci_summary_report(
        verdict="gating FAILURE",
        passed=0,
        total=40,
        skipped=0,
        quarantined=[],
        infrastructure=[],
        failed=rows,
    )
    assert "**Gating (40):**" in out, "the true total must be stated"
    assert "more" in out, "the dropped remainder must be acknowledged, not hidden"
    assert "039_case" not in out, "expected the list to actually cap"


def test_an_error_without_assertions_still_explains_itself() -> None:
    """A case that raised has no failing assertion to quote. Rendering an empty
    reason there would be a summary that names a case and says nothing."""
    out = ci_summary_report(
        verdict="gating FAILURE",
        passed=0,
        total=1,
        skipped=0,
        quarantined=[],
        infrastructure=[],
        failed=[CaseResult(case_id="001_x", error="ValueError: not a valid EventType")],
    )
    assert "not a valid EventType" in out


# --------------------------------------------------------------------------
# The writer
# --------------------------------------------------------------------------


def test_it_appends_rather_than_replaces(tmp_path: Path, monkeypatch) -> None:
    """One job runs many steps. Truncating would delete whatever ran before."""
    target = tmp_path / "summary.md"
    target.write_text("earlier step\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))

    assert _write_ci_summary("### Eval gate — clean\n") is True
    body = target.read_text(encoding="utf-8")
    assert "earlier step" in body and "Eval gate" in body


def test_outside_ci_it_is_a_no_op(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert _write_ci_summary("anything") is False


def test_an_unwritable_target_never_raises(tmp_path: Path, monkeypatch) -> None:
    """Gotcha 7: an enhancement is never load-bearing. A reporting failure must
    not change the exit code of the safety gate it is reporting on."""
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "no-such-dir" / "s.md"))
    assert _write_ci_summary("anything") is False  # and, crucially, did not raise


# --------------------------------------------------------------------------
# The wiring — neither half sees what the other does
# --------------------------------------------------------------------------


def test_the_headline_names_the_case_and_the_reason() -> None:
    """One line, because pytest's short summary shows exactly one."""
    line = gate_headline(
        verdict="gating FAILURE",
        passed=40,
        total=42,
        skipped=1,
        infrastructure=[],
        failed=[_fail("037_email_query_routes_to_gmail", "must_not_route_to[0] != buildium_agent")],
    )
    assert "\n" not in line, "a multi-line headline is truncated to its first line by pytest"
    assert "037_email_query_routes_to_gmail" in line
    assert "must_not_route_to[0] != buildium_agent" in line


def test_a_reason_containing_a_newline_cannot_truncate_the_headline() -> None:
    """The distinguishing input: a traceback-shaped reason.

    Naming the case is the whole point, and the case name trails the reason —
    so a raw newline in the reason would cut the line before it.
    """
    line = gate_headline(
        verdict="gating FAILURE",
        passed=0,
        total=1,
        skipped=0,
        infrastructure=[],
        failed=[
            CaseResult(
                case_id="001_x", error="Traceback (most recent call last):\n  boom\nValueError"
            )
        ],
    )
    assert "\n" not in line
    assert "001_x" in line


def test_more_than_one_failure_says_so_rather_than_hiding_them() -> None:
    rows = [_fail(f"{i:03d}_case", "boom") for i in range(4)]
    line = gate_headline(
        verdict="gating FAILURE",
        passed=0,
        total=4,
        skipped=0,
        infrastructure=[],
        failed=rows,
    )
    assert "+3 more" in line, f"a silent truncation reads as 'that was all of them': {line}"


def test_the_headline_is_stored_even_outside_ci(monkeypatch) -> None:
    """⚠ The step-summary writer is a no-op without the env var — which is
    every LOCAL run, i.e. exactly where a developer reads pytest's message.
    Storing inside the writer would make the headline empty there."""
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    runner._report_gate(
        verdict="gating FAILURE",
        passed=0,
        total=1,
        skipped=0,
        quarantined=[],
        infrastructure=[],
        failed=[_fail("122_trivial_no_overloop", "specialist_call_count_max exceeded")],
    )
    assert "122_trivial_no_overloop" in runner.last_gate_headline()


def test_the_eval_test_puts_the_headline_first_in_its_assertion_message() -> None:
    """A behavioural test of the helper cannot see that nothing calls it
    (gotchas 62/66/69/103). This is the call site.

    ⚠ ITS FIRST CUT WAS A SUBSTRING CHECK AND A MUTATION WALKED PAST IT —
    gotcha 40, inside the guard written for this defect, and gotcha 94's
    identical finding one module over. ``"last_gate_headline" in src`` is
    satisfied by a dead ``_ = last_gate_headline`` beside a hardcoded message,
    which is the whole defect wearing the fix's name.

    So the shape is asserted instead: the message must be a name bound from
    ``last_gate_headline() or <fallback>``. The ``or`` is what puts the
    verdict FIRST — pytest's summary line is cut to terminal width, so a
    message leading with the generic wording pushes the case id off the one
    surface that displays it.
    """
    src = (Path(__file__).resolve().parents[1] / "test_evals.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    asserts = [n for n in ast.walk(tree) if isinstance(n, ast.Assert) and n.msg is not None]
    assert asserts, "tests/test_evals.py has no assertion carrying a message"

    def _leads_with_the_headline(node: ast.expr | None) -> bool:
        return (
            isinstance(node, ast.BoolOp)
            and isinstance(node.op, ast.Or)
            and isinstance(node.values[0], ast.Call)
            and isinstance(node.values[0].func, ast.Name)
            and node.values[0].func.id == "last_gate_headline"
        )

    bindings = {
        target.id: node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    carried = [
        a.msg
        for a in asserts
        if _leads_with_the_headline(a.msg)
        or (isinstance(a.msg, ast.Name) and _leads_with_the_headline(bindings.get(a.msg.id)))
    ]
    assert carried, (
        "tests/test_evals.py's assertion message does not LEAD with "
        "last_gate_headline(), so a red gate is either unnameable from CI or "
        "named past the width pytest's summary line displays"
    )


def test_every_verdict_exit_in_main_reports_the_gate() -> None:
    """A behavioural test exercises ONE path; a fourth exit added later would be
    invisible to it. Gotcha 96's technique: count the returns in the AST and
    fail when one is not covered.

    ``return 2`` is exempt BY NAME, not by accident: it fires when no golden
    case matched the filter, before any case has run, so there is no verdict to
    report. Exempting it keeps the rule exact rather than weakening it.
    """
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "main")

    def _is_usage_exit(node: ast.Return) -> bool:
        return isinstance(node.value, ast.Constant) and node.value.value == 2

    returns = sorted(
        (n for n in ast.walk(fn) if isinstance(n, ast.Return) and not _is_usage_exit(n)),
        key=lambda n: n.lineno,
    )
    # ⚠ RE-POINTED, never deleted (gotchas 89/93/95). The property is unchanged
    # — every verdict exit must report — but the reporting moved behind one
    # ``_report_gate`` call so a fourth exit cannot pick up the step summary and
    # forget the headline. Pinning the old name would fail against correct code.
    writes = sorted(
        n.lineno
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_report_gate"
    )
    assert returns, "expected main() to have verdict exits — the scan found none"

    previous = 0
    for ret in returns:
        covered = [w for w in writes if previous < w < ret.lineno]
        assert covered, (
            f"main() returns a verdict at line {ret.lineno} without writing a step "
            "summary first. Every verdict exit must report, or a red gate becomes "
            "un-diagnosable from CI again — which is the defect this file exists for."
        )
        previous = ret.lineno


def test_the_wiring_actually_runs(tmp_path: Path, monkeypatch) -> None:
    """The AST guard above proves the call SITE exists; only running it proves
    the call works. 072 is deterministic and needs no provider key, so this
    costs nothing and cannot flake on a model."""
    target = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
    # A targeted --case run is exempt from the PR gate and the skip-floor, so
    # this exercises the ordinary green exit.
    code = asyncio.run(main(case_filter="072"))

    assert code == 0
    assert target.exists(), "main() completed without writing any step summary"
    body = target.read_text(encoding="utf-8")
    assert "Eval gate" in body and "clean" in body


@pytest.mark.parametrize("var", ["GITHUB_STEP_SUMMARY"])
def test_the_env_var_is_the_one_github_actually_sets(var: str) -> None:
    """Spelled once, here and in the writer. A typo'd variable name is a writer
    that silently no-ops on every run — the failure mode this whole file is
    about, wearing the fix's name."""
    assert f'os.environ.get("{var}")' in RUNNER.read_text(encoding="utf-8")
    assert os.environ.get(var) is None or isinstance(os.environ.get(var), str)
