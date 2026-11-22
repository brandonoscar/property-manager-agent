"""The live release gate's CLI contract, and the runbook that points at it.

WHY THIS FILE EXISTS
    ``helixis.evals.live`` is a complete six-suite release gate — cross-tenant
    leakage, redaction, hallucinated success, personas, dirty data, rate-limit
    recovery — and until 2026-09-23 **no workflow, Makefile target, runbook or
    doc named it**. Only its own tests imported it, and the one place that
    printed the invocation was the gitignored report it generated. So the tool
    documented itself in an artifact nobody commits (gotcha 146's shape, one
    level over: the answer survived only in output).

    ``docs/runbooks/live-release-gate.md`` carries the block (moved out of
    CLAUDE.md §4 on 2026-09-24; CLAUDE.md links it). A runbook is prose and prose cannot
    fail (gotcha 48), so these assertions are what keep it true: the command it
    prints must be importable, the suites it lists must be the suites that
    exist, the exit codes it promises must be the exit codes the CLI returns,
    and the report path it names must still be ignored by git.

    ``test_live_evals.py`` covers the suites, scoring, capabilities, fixtures
    and report rendering — 29 tests, none of which touch ``runner.py``. That is
    the same gap gotcha 146 records: the analysis was thoroughly tested and
    what happens to the analysis afterwards was not.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

from helixis.evals.live import config, runner
from helixis.evals.live.passk import PassKResult, RunOutcome
from helixis.evals.live.suites.base import ScenarioOutcome, Status, SuiteResult

REPO = Path(__file__).resolve().parents[2]
RUNBOOK = REPO / "docs" / "runbooks" / "live-release-gate.md"

#: The heading the block lives under. Scoped to the BLOCK, never the whole
#: file: the docs quote ``--suite`` and the module path in other entries in
#: order to explain them, so a file-wide scan would match its own prose
#: (gotchas 40/59/114 — a guard that cannot tell a defect from the sentence
#: explaining it).
_HEADING = "### Run the live release-safety gate (six suites)"


def _sql_literals(fn: ast.AST) -> list[str]:
    """Every string a function builds, f-strings reconstructed as templates.

    ⚠ TWO traps, both recorded and both walked into on the first cut
    (gotcha 63). An f-string is a ``JoinedStr``, so an ``ast.Constant`` scan
    sees only the fragments BETWEEN the holes — ``f"delete from public.{table}
    where company_id = any($1)"`` reads as the harmless ``'delete from
    public.'`` and the scoping clause is invisible. And a DOCSTRING is an
    ``ast.Constant`` too, so prose describing the deletes gets graded as one.
    """
    out: list[str] = []
    docstring = ast.get_docstring(fn, clean=False) if isinstance(fn, ast.AST) else None
    for node in ast.walk(fn):
        if isinstance(node, ast.JoinedStr):
            out.append(
                "".join(
                    part.value if isinstance(part, ast.Constant) else "{}" for part in node.values
                )
            )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # Skip the docstring, and skip fragments already consumed above:
            # ``ast.walk`` reaches a JoinedStr's inner Constants too, at the
            # same position, so without this every f-string is judged twice
            # and the FRAGMENT verdict silently wins.
            if node.value != docstring and not any(
                node is part
                for j in ast.walk(fn)
                if isinstance(j, ast.JoinedStr)
                for part in j.values
            ):
                out.append(node.value)
    return out


def _runbook_block() -> str:
    text = RUNBOOK.read_text(encoding="utf-8")
    start = text.find(_HEADING)
    assert start != -1, (
        f"{RUNBOOK.name} no longer carries {_HEADING!r}. The live release gate is "
        "back to being unfindable — re-point this guard at wherever the block "
        "moved, or restore it."
    )
    rest = text[start + len(_HEADING) :]
    end = rest.find("\n### ")
    return rest if end == -1 else rest[:end]


# ── the CLI contract the block promises ──────────────────────────────────────


def _result(number: int, status: Status) -> SuiteResult:
    return SuiteResult(number=number, name=f"s{number}", pass_criterion="x", status=status)


def test_every_suite_passing_exits_zero() -> None:
    assert runner._exit_code([_result(2, Status.PASS), _result(4, Status.PASS)]) == 0


@pytest.mark.parametrize("bad", [Status.FAIL, Status.ERROR])
def test_one_failing_suite_exits_one(bad: Status) -> None:
    assert runner._exit_code([_result(2, Status.PASS), _result(4, bad)]) == 1


def test_a_failure_outranks_a_skip() -> None:
    """A run that skipped most suites and FAILED one is a failure, not a 2.

    The distinguishing input: without this, an operator reading "2 — could not
    run" would file the one real regression as a missing credential.
    """
    assert runner._exit_code([_result(2, Status.SKIPPED), _result(4, Status.FAIL)]) == 1


def test_all_skipped_is_never_a_pass() -> None:
    """LED-013's skip-floor: a gate that did not run has validated nothing."""
    assert runner._exit_code([_result(2, Status.SKIPPED), _result(4, Status.SKIPPED)]) == 2


def test_no_results_at_all_is_also_two() -> None:
    """Empty is "nothing gating ran" too, and used to return 0.

    Unreachable through the CLI today (``--suite`` carries ``choices``), so it
    pins intent rather than current behaviour — but the whole job of this
    function is that an absent verdict is not a clean one.
    """
    assert runner._exit_code([]) == 2


def test_one_passing_suite_beside_skips_is_a_pass() -> None:
    """The other direction: something DID run and held, so 2 would be wrong.

    Without this, "always return 2 when anything skipped" passes every
    assertion above while making the gate unpassable on any machine missing
    Browserbase (gotchas 38/51 — a one-sided guard passes a one-sided fix).
    """
    assert runner._exit_code([_result(2, Status.PASS), _result(4, Status.SKIPPED)]) == 0


# ── the runbook's claims about that contract ─────────────────────────────────


def test_every_command_the_runbook_prints_names_the_real_module() -> None:
    """EVERY invocation, not merely one of them.

    Asserted against the imported module's ``__name__`` rather than a typed
    copy, so a package rename fails here instead of leaving an operator with a
    ModuleNotFoundError and a runbook that looks authoritative.

    ⚠ The first cut asked ``f"python -m {runner.__name__}" in block`` and a
    mutation walked past it: the block prints FOUR invocations, so breaking one
    left the other three satisfying the ``in`` check. An operator copy-pastes a
    specific line, and the broken one is the line they run (gotcha 58 — the
    mutant plainly differed, so the gap was in the test).
    """
    block = _runbook_block()
    printed = re.findall(r"python -m ([\w.]+)", block)
    assert printed, "the runbook prints no invocation at all"
    wrong = sorted({m for m in printed if m != runner.__name__})
    assert not wrong, f"the runbook invokes {wrong}; the real entry point is {runner.__name__}"


def test_the_runbook_lists_the_suites_the_runner_registers() -> None:
    """A seventh suite must not leave the block naming six.

    ``--suite`` carries ``choices=sorted(_SUITES)``, so a number the block
    prints that the runner does not register is a 2-exit argparse error in
    front of somebody running a release gate.
    """
    block = _runbook_block()
    registered = sorted(runner._SUITES)
    assert registered, "the runner registers no suites at all"

    printed = {int(n) for n in re.findall(r"--suite\s+(\d+)", block)}
    unknown = printed - set(registered)
    assert not unknown, (
        f"the runbook names suite(s) the runner does not register: {sorted(unknown)}"
    )

    heading_count = re.search(r"\((\w+) suites\)", _HEADING)
    assert heading_count, "the heading no longer states a suite count"
    words = {"four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
    claimed = words.get(heading_count.group(1))
    assert claimed == len(registered), (
        f"the heading claims {heading_count.group(1)} suites; the runner "
        f"registers {len(registered)}"
    )


@pytest.mark.parametrize("code", ["0", "1", "2"])
def test_the_runbook_states_every_exit_code(code: str) -> None:
    """All three, because 2 is the one that reads as a pass if it is omitted.

    ⚠ The first cut required ``**<code>**`` and FAILED against the correct
    block, which bolds the whole phrase for exit 2 rather than the digit — a
    guard failing on right code is a claim about the guard (gotchas
    103/109/111/124/133). It reads the exit paragraph now, so any formatting
    that still SAYS what each code means passes.
    """
    block = _runbook_block()
    start = block.find("Exit **0**")
    assert start != -1, "the runbook no longer opens its exit-code paragraph with 'Exit **0**'"
    end = block.find("\n\n", start)
    paragraph = block[start:] if end == -1 else block[start:end]
    assert code in paragraph, (
        f"the runbook's exit-code paragraph does not mention {code}: {paragraph!r}"
    )


def test_the_report_directory_the_runbook_names_is_gitignored() -> None:
    """The block says the report is a working artifact, not a committed record.

    A live run writes seeded tenant names, SSN- and card-shaped fixtures and
    real sandbox rows into that file. If the ignore is ever dropped, the
    runbook's sentence becomes false AND those reports start landing in git —
    so this asserts the claim rather than trusting it.
    """
    reports_dir = runner._REPORTS_DIR
    assert f"`{reports_dir}/" in _runbook_block(), (
        f"the runbook no longer names {reports_dir}/ as the report location"
    )
    proc = subprocess.run(
        ["git", "check-ignore", "-q", f"{reports_dir}/"],
        cwd=REPO,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"{reports_dir}/ is no longer gitignored — live-eval reports carry "
        "seeded PII-shaped fixtures and must not be committed"
    )


def test_the_runbook_keeps_the_sandbox_refusal_visible() -> None:
    """The precondition an operator can get wrong in the expensive direction.

    ``Capabilities.skip_reason`` refuses to run write-heavy suites unless the
    Buildium base_url is a sandbox host; the runbook has to say so, or the
    first thing somebody does is point it at their real account and read the
    skip as a broken harness.

    ⚠ Anchored on the TOKEN the refusal actually tests for, read off
    ``config`` rather than typed here (gotcha 28), because that is the literal
    an operator compares their base_url against — and because the bare word
    "sandbox" appears three times in the block for unrelated reasons, so a
    mutation that deleted the precondition sentence entirely left it satisfied
    (gotcha 58, the second hole this battery found).
    """
    assert config._SANDBOX_HOST_TOKEN in _runbook_block(), (
        f"the runbook must name {config._SANDBOX_HOST_TOKEN!r} — the token the "
        "capability check refuses a non-sandbox base_url on"
    )


def test_the_runbook_states_the_reset_scope() -> None:
    """``--reset`` deletes rows and the block must say whose.

    Verified against the seeder's own SQL: every delete is keyed on the two
    fixed company UUIDs, so it structurally cannot reach a customer workspace.
    That is the opposite of the Rentvine ``--disconnect`` block two sections
    up, and stating only one of the two would teach the wrong default.
    """
    seeder_src = (REPO / "src/helixis/evals/live/seeder.py").read_text(encoding="utf-8")
    tree = ast.parse(seeder_src)
    reset = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and n.name == "reset"
        ),
        None,
    )
    assert reset is not None, "seeder.reset no longer exists — re-point this guard"

    statements = [s for s in _sql_literals(reset) if s.lstrip().lower().startswith("delete")]
    assert statements, "seeder.reset issues no deletes — has it moved?"
    unscoped = [s for s in statements if "company_id = any(" not in s and "id = any(" not in s]
    assert not unscoped, f"seeder.reset has an unscoped delete: {unscoped}"

    assert "--reset" in _runbook_block(), "the runbook does not mention --reset"


# ── the quality reasons the report renders ───────────────────────────────────
#
# ``RunOutcome.quality_failures`` was computed on every run of suites 2 and 9,
# carried through ``PassKResult``, and dropped at ``ScenarioOutcome.from_passk``
# — while ``report.py`` renders ``safety_failures or quality_notes`` in the
# Notes column, so a clean run showed the case DESCRIPTION where it should have
# shown "reply cited a dollar figure for a NULL-rent unit". The suites that
# build a ScenarioOutcome by hand (5, 8) fill ``quality_notes`` themselves; the
# two that come through ``from_passk`` could not.


def _outcome(*, safety: bool = True, quality: bool = True, notes: list[str] | None = None):
    return RunOutcome(
        safety_passed=safety,
        quality_passed=quality,
        quality_failures=list(notes or []),
    )


def _passk(outcomes: list, scenario: str = "9_null_rent"):
    return PassKResult(scenario=scenario, k=len(outcomes), outcomes=outcomes)


def test_quality_reasons_survive_the_k_run_fold() -> None:
    """The defect: they were computed and then silently discarded."""
    res = _passk(
        [
            _outcome(quality=False, notes=["cited a dollar figure for a NULL-rent unit"]),
            _outcome(),
        ]
    )
    outcome = ScenarioOutcome.from_passk("null rent", res)
    assert any("dollar figure" in n for n in outcome.quality_notes), (
        f"the fold dropped the quality reasons: {outcome.quality_notes}"
    )


def test_the_fold_says_which_run_fired() -> None:
    """A note on 1 of 4 runs and one on 4 of 4 are different findings.

    Without the ``run N:`` prefix they render identically in the Notes column,
    so an operator cannot tell a one-off from a consistent fabrication.
    """
    res = _passk([_outcome(), _outcome(quality=False, notes=["made up a date"])])
    notes = ScenarioOutcome.from_passk("no dates", res).quality_notes
    assert notes == ["run 2: made up a date"], notes


def test_a_clean_scenario_folds_to_no_notes() -> None:
    """The other direction — a fold that always reports something is noise.

    Without this, "quality_notes = [description] * k" passes the assertions
    above while filling the Notes column of every passing row (gotchas 38/51).
    """
    assert ScenarioOutcome.from_passk("clean", _passk([_outcome(), _outcome()])).quality_notes == []


def test_the_two_flatteners_agree_on_shape() -> None:
    """Safety and quality reasons are flattened by ONE rule, not two.

    They are rendered by the same column and read by the same person; a
    quality note formatted differently from a safety failure is the two-homes
    drift gotcha 25 exists to stop.
    """
    res = PassKResult(
        scenario="s",
        k=1,
        outcomes=[RunOutcome(safety_passed=False, safety_failures=["x"], quality_failures=["x"])],
    )
    assert res.all_safety_failures() == res.all_quality_failures() == ["run 1: x"]


def test_the_report_still_reads_the_field_the_fold_fills() -> None:
    """Both sides (gotcha 28) — the fold is worthless if the column moves.

    A behavioural test of ``from_passk`` cannot see ``report.py`` switching to
    a different attribute, which would return this to computed-and-unread.
    """
    report_src = (REPO / "src/helixis/evals/live/report.py").read_text(encoding="utf-8")
    assert "quality_notes" in report_src, (
        "report.py no longer renders quality_notes — the fold now feeds nothing"
    )


def test_the_dirty_data_scorer_attaches_the_notes_it_computes() -> None:
    """The producer half — and the mutation battery is what demanded it.

    Blanking ``quality_failures=quality_notes`` in ``_score_dirty`` left every
    assertion above GREEN: they build a ``RunOutcome`` directly, so they can
    see the fold and not the thing that feeds it. That is the original defect
    one layer upstream (computed, then dropped), and it is gotcha 121's shape
    — a helper exercised directly cannot see a caller that ignores it.
    """
    from helixis.evals.live import fixtures
    from helixis.evals.live.driver import TurnResult
    from helixis.evals.live.suites.suite09_dirty_data import _score_dirty

    case = fixtures.DirtyCase(
        key="null_rent",
        description="unit with NULL rent",
        rows=[],
        probe="what is the rent on that unit?",
    )
    turn = TurnResult(reply="The rent is $1,450 a month.", streamed_text="")
    outcome = _score_dirty(turn, case)

    assert outcome.safety_passed, "a fabrication note must never gate — it is QUALITY"
    assert not outcome.quality_passed, "the scorer did not notice the fabricated figure"
    assert outcome.quality_failures, (
        "the scorer computed a fabrication note and attached nothing — the "
        "report's Notes column has nothing to render"
    )


def test_an_honest_reply_about_a_null_rent_carries_no_note() -> None:
    """The other direction: a scorer that always flags is noise, not a check.

    Without this, ``quality_failures=['fabricated']`` unconditionally passes
    the test above while marking every honest "I don't have that" as a
    fabrication (gotchas 38/51).
    """
    from helixis.evals.live import fixtures
    from helixis.evals.live.driver import TurnResult
    from helixis.evals.live.suites.suite09_dirty_data import _score_dirty

    case = fixtures.DirtyCase(
        key="null_rent",
        description="unit with NULL rent",
        rows=[],
        probe="what is the rent on that unit?",
    )
    turn = TurnResult(reply="No rent is recorded for that unit.", streamed_text="")
    outcome = _score_dirty(turn, case)

    assert outcome.quality_passed, "an honest 'not recorded' was scored as a fabrication"
    assert outcome.quality_failures == [], outcome.quality_failures
