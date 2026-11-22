"""Unit tests for the tiered-grid dogfood core (scripts/dogfood_grid.py)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# scripts/ isn't a package — load the module by path. It must be registered in
# sys.modules BEFORE exec, or @dataclass fails to resolve cls.__module__.
_MOD_PATH = Path(__file__).resolve().parents[2] / "scripts" / "dogfood_grid.py"
_spec = importlib.util.spec_from_file_location("dogfood_grid", _MOD_PATH)
assert _spec and _spec.loader
dg = importlib.util.module_from_spec(_spec)
sys.modules["dogfood_grid"] = dg
_spec.loader.exec_module(dg)


# ── parse_runner_output ─────────────────────────────────────────


def test_parse_pass() -> None:
    v = dg.parse_runner_output("Running 1 case\n[PASS] 402_injection\n1/1 passed")
    assert v.status == "PASS" and v.kind is None


def test_parse_fail_safety() -> None:
    v = dg.parse_runner_output("[FAIL] 320_negative (kind=safety)\n    - foo: bar")
    assert v.status == "FAIL" and v.kind == "safety"


def test_parse_fail_quality() -> None:
    v = dg.parse_runner_output("[FAIL] 070_report (kind=quality)")
    assert v.status == "FAIL" and v.kind == "quality"


def test_parse_skip() -> None:
    v = dg.parse_runner_output("[SKIP] 030_route — HELIXIS_LLM__GOOGLE_API_KEY not set")
    assert v.status == "SKIP" and v.kind is None


def test_parse_no_status_is_error() -> None:
    v = dg.parse_runner_output("Traceback (most recent call last):\nImportError: boom")
    assert v.status == "ERROR"


def test_parse_first_status_wins() -> None:
    # A --case run prints one status; guard against a later line changing it.
    v = dg.parse_runner_output("[PASS] 402\n[FAIL] other (kind=safety)")
    assert v.status == "PASS"


# ── is_clean / should_escalate ──────────────────────────────────


def test_is_clean_only_pass() -> None:
    assert dg.is_clean(dg.RunVerdict("PASS"))
    for bad in ("FAIL", "SKIP", "ERROR"):
        assert not dg.is_clean(dg.RunVerdict(bad))


def test_escalate_on_any_wobble() -> None:
    clean = [dg.RunVerdict("PASS")] * 5
    assert not dg.should_escalate(clean)
    wobble = [dg.RunVerdict("PASS"), dg.RunVerdict("FAIL", "quality"), dg.RunVerdict("PASS")]
    assert dg.should_escalate(wobble)


def test_escalate_on_skip() -> None:
    assert dg.should_escalate([dg.RunVerdict("PASS"), dg.RunVerdict("SKIP")])


def test_empty_screen_never_escalates() -> None:
    assert not dg.should_escalate([])


# ── expand_case_spec ────────────────────────────────────────────


def test_expand_single_ids() -> None:
    assert dg.expand_case_spec("402,403,404") == ["402", "403", "404"]


def test_expand_range_preserves_padding() -> None:
    assert dg.expand_case_spec("060-063") == ["060", "061", "062", "063"]


def test_expand_mixed_and_dedup() -> None:
    got = dg.expand_case_spec("402-404, 320, 402")
    assert got == ["402", "403", "404", "320"]  # 402 not repeated, order preserved


def test_expand_slug_ids() -> None:
    assert dg.expand_case_spec("060_browser,061_login") == ["060_browser", "061_login"]


def test_expand_whitespace_and_empties() -> None:
    assert dg.expand_case_spec(" 402 , , 403 ,") == ["402", "403"]


def test_expand_descending_range_raises() -> None:
    with pytest.raises(ValueError, match="descending"):
        dg.expand_case_spec("406-402")


def test_expand_single_element_range() -> None:
    assert dg.expand_case_spec("402-402") == ["402"]


# ── CaseResult.verdict ──────────────────────────────────────────


def _res(case_id: str, *statuses: tuple[str, str | None]) -> dg.CaseResult:
    r = dg.CaseResult(case_id=case_id)
    r.screen = [dg.RunVerdict(s, k) for s, k in statuses]
    return r


def test_verdict_all_pass() -> None:
    r = _res("402", *[("PASS", None)] * 5)
    assert r.verdict(threshold=0.8) == "PASS"
    assert r.pass_count == 5 and r.run_count == 5


def test_verdict_k_of_n_quality() -> None:
    # 4/5 pass, one quality fail → passes at 0.8 threshold.
    r = _res(
        "070", ("PASS", None), ("PASS", None), ("PASS", None), ("PASS", None), ("FAIL", "quality")
    )
    assert r.verdict(threshold=0.8) == "PASS"


def test_verdict_below_threshold() -> None:
    r = _res(
        "070",
        ("PASS", None),
        ("PASS", None),
        ("FAIL", "quality"),
        ("FAIL", "quality"),
        ("FAIL", "quality"),
    )
    assert r.verdict(threshold=0.8) == "FAIL(quality)"


def test_verdict_safety_fail_is_outright() -> None:
    # Even 19/20 pass fails outright if ONE run is a safety fail.
    r = dg.CaseResult(case_id="320")
    r.screen = [dg.RunVerdict("PASS")] * 4 + [dg.RunVerdict("FAIL", "safety")]
    r.full = [dg.RunVerdict("PASS")] * 15
    assert r.verdict(threshold=0.8) == "FAIL(safety)"


def test_verdict_skip_never_passes() -> None:
    r = _res("030", ("PASS", None), ("SKIP", None), ("PASS", None))
    assert r.verdict(threshold=0.5) == "FAIL(skip)"


def test_verdict_no_runs() -> None:
    assert dg.CaseResult(case_id="x").verdict(threshold=0.8) == "FAIL(no-runs)"


# ── grid_case (tiering with an injected runner) ─────────────────


def _scripted_runner(script: dict[str, list[dg.RunVerdict]]) -> dg.RunnerFn:
    calls: dict[str, int] = {}

    def _run(case_id: str) -> dg.RunVerdict:
        i = calls.get(case_id, 0)
        calls[case_id] = i + 1
        seq = script[case_id]
        return seq[min(i, len(seq) - 1)]

    return _run


def test_grid_clean_case_stops_at_screen() -> None:
    run = _scripted_runner({"402": [dg.RunVerdict("PASS")]})
    r = dg.grid_case("402", screen=5, full=20, run=run)
    assert not r.escalated
    assert r.run_count == 5  # never paid the full grid
    assert r.full == []


def test_grid_wobble_escalates_to_full() -> None:
    seq = [dg.RunVerdict("PASS")] * 4 + [dg.RunVerdict("FAIL", "quality")]
    run = _scripted_runner({"070": seq})
    r = dg.grid_case("070", screen=5, full=20, run=run)
    assert r.escalated
    assert r.run_count == 20  # topped up screen(5) → full(20)
    assert len(r.full) == 15


def test_grid_safety_fail_in_screen_escalates_and_fails() -> None:
    run = _scripted_runner({"320": [dg.RunVerdict("PASS"), dg.RunVerdict("FAIL", "safety")]})
    r = dg.grid_case("320", screen=5, full=20, run=run)
    assert r.escalated and r.has_safety_fail
    assert r.verdict(threshold=0.8) == "FAIL(safety)"


def test_grid_equal_screen_full_no_topup() -> None:
    # full == screen: escalation adds zero extra runs.
    run = _scripted_runner({"x": [dg.RunVerdict("FAIL", "quality")]})
    r = dg.grid_case("x", screen=5, full=5, run=run)
    assert r.escalated and r.run_count == 5 and r.full == []


def test_grid_on_run_callback_fires_per_run() -> None:
    run = _scripted_runner({"402": [dg.RunVerdict("PASS")]})
    seen: list[tuple[str, str, int]] = []
    dg.grid_case(
        "402", screen=3, full=10, run=run, on_run=lambda c, t, i, v: seen.append((c, t, i))
    )
    assert seen == [("402", "screen", 1), ("402", "screen", 2), ("402", "screen", 3)]


# ── savings ─────────────────────────────────────────────────────


def test_savings_all_clean() -> None:
    results = [_res(str(i), *[("PASS", None)] * 5) for i in range(15)]
    tiered, fixed, saved = dg.savings(results, screen=5, full=20)
    assert tiered == 75 and fixed == 300 and saved == 225


def test_savings_mixed() -> None:
    clean = [_res(str(i), *[("PASS", None)] * 5) for i in range(13)]
    esc = dg.CaseResult(case_id="e1")
    esc.screen = [dg.RunVerdict("PASS")] * 4 + [dg.RunVerdict("FAIL", "quality")]
    esc.full = [dg.RunVerdict("PASS")] * 15  # full grid
    esc.escalated = True
    esc2 = dg.CaseResult(case_id="e2")
    esc2.screen = [dg.RunVerdict("PASS")] * 4 + [dg.RunVerdict("FAIL", "quality")]
    esc2.full = [dg.RunVerdict("PASS")] * 15
    esc2.escalated = True
    results = clean + [esc, esc2]
    tiered, fixed, saved = dg.savings(results, screen=5, full=20)
    assert tiered == 13 * 5 + 2 * 20  # 65 + 40 = 105
    assert fixed == 15 * 20  # 300
    assert saved == 195
