"""Unit tests for the eval skip-floor guard (LED-013).

Missing the Gemini key makes every model-driven case SKIP, and SKIP is
non-failing — so a run where the secret didn't materialize would report GREEN
off ~2 deterministic cases. ``skip_floor_breached`` turns that silent
degradation into a hard failure on a trusted (secrets-expected) run.
"""

from __future__ import annotations

from helixis.evals.runner import CaseResult, skip_floor_breached


def _skipped(reason: str) -> CaseResult:
    return CaseResult(case_id="x", skipped=True, skip_reason=reason)


def _passed() -> CaseResult:
    return CaseResult(case_id="ok")


def test_breach_when_many_env_skips_and_secrets_required() -> None:
    results = [_skipped("missing required env: HELIXIS_LLM__GOOGLE_API_KEY") for _ in range(10)]
    results += [_passed(), _passed()]  # the ~2 deterministic cases still "pass"
    breached, count = skip_floor_breached(results, require_secrets=True)
    assert breached is True
    assert count == 10


def test_no_breach_on_fork_run_even_with_many_skips() -> None:
    # Fork PRs get no secrets by design → require_secrets is False → never fail.
    results = [_skipped("missing required env: HELIXIS_LLM__GOOGLE_API_KEY") for _ in range(10)]
    breached, count = skip_floor_breached(results, require_secrets=False)
    assert breached is False
    assert count == 10


def test_no_breach_when_skips_under_floor() -> None:
    # A couple of missing-env skips is fine (deterministic cases don't need it).
    results = [_skipped("missing required env: HELIXIS_LLM__GOOGLE_API_KEY") for _ in range(2)]
    breached, count = skip_floor_breached(results, require_secrets=True, max_env_skips=3)
    assert breached is False
    assert count == 2


def test_deps_wiring_skips_do_not_count() -> None:
    # A skip for missing deps / unwired tooling is NOT a missing-secret signal.
    results = [_skipped("browserbase deps not installed") for _ in range(10)]
    breached, count = skip_floor_breached(results, require_secrets=True)
    assert breached is False
    assert count == 0


def test_gemini_key_phrasing_variant_counts() -> None:
    # The planner_summarizer path phrases its skip differently.
    results = [_skipped("no LLM API key (planner_summarizer cases need one)") for _ in range(5)]
    breached, count = skip_floor_breached(results, require_secrets=True)
    assert breached is True
    assert count == 5
