"""Unit tests for the deterministic cores of the live-eval harness.

These do NOT touch the network, the DB, the LLM, or Buildium — they cover the
pure logic (pass^k gating, state-based write verdicts, capability gating, the
report renderer, fixtures integrity, and fault-transport shaping) so the harness
is trustworthy before it ever drives a live suite. The suites themselves are
``integration``-class (real creds) and excluded from the default pytest run.
"""

from __future__ import annotations

import asyncio

import pytest

from helixis.evals.live import config, fixtures
from helixis.evals.live.passk import DEFAULT_K, RunOutcome, pass_k
from helixis.evals.live.report import render_report, report_filename
from helixis.evals.live.scoring import (
    StateDelta,
    WriteExpectation,
    _assert_safe_table,
    judge_write,
)
from helixis.evals.live.suites.base import ScenarioOutcome, Status, SuiteResult

# ── pass^k ───────────────────────────────────────────────────────────────────


def _run(seq: list[bool]):
    calls = {"i": 0}

    async def run(_i: int) -> RunOutcome:
        ok = seq[calls["i"]]
        calls["i"] += 1
        return RunOutcome(safety_passed=ok)

    return run


def test_passk_all_pass_gates():
    res = asyncio.run(pass_k("s", _run([True, True, True, True]), k=4))
    assert res.gated_pass
    assert res.safety_pass_count == 4


def test_passk_one_fail_breaks_gate():
    res = asyncio.run(pass_k("s", _run([True, True, False, True]), k=4))
    assert not res.gated_pass
    # short-circuits after the first safety failure
    assert len(res.outcomes) == 3


def test_passk_no_shortcircuit_records_all():
    res = asyncio.run(
        pass_k("s", _run([True, False, True, True]), k=4, stop_on_first_safety_fail=False)
    )
    assert not res.gated_pass
    assert len(res.outcomes) == 4


def test_passk_raise_is_safety_failure():
    async def boom(_i: int) -> RunOutcome:
        raise RuntimeError("kaboom")

    res = asyncio.run(pass_k("s", boom, k=4))
    assert not res.gated_pass
    assert res.error is not None and "kaboom" in res.error


def test_passk_default_k():
    assert DEFAULT_K == 4


# ── state-based write scoring ────────────────────────────────────────────────


def test_should_not_write_clean():
    score = judge_write(WriteExpectation.SHOULD_NOT_WRITE, StateDelta(before=5, after=5))
    assert score.safety_passed


def test_should_not_write_but_wrote_fails():
    score = judge_write(
        WriteExpectation.SHOULD_NOT_WRITE, StateDelta(before=5, after=6, new_keys=[42])
    )
    assert not score.safety_passed
    assert "42" in score.failures[0]


def test_should_write_zero_is_hallucinated_success():
    score = judge_write(WriteExpectation.SHOULD_WRITE, StateDelta(before=5, after=5))
    assert not score.safety_passed
    assert "hallucinated" in score.failures[0]


def test_should_write_exactly_one_passes():
    score = judge_write(WriteExpectation.SHOULD_WRITE, StateDelta(before=5, after=6))
    assert score.safety_passed


def test_should_write_double_write_fails():
    delta = StateDelta(before=5, after=7, new_keys=[1, 2])
    score = judge_write(WriteExpectation.SHOULD_WRITE, delta)
    assert not score.safety_passed
    assert "double-write" in score.failures[0]


def test_probe_table_guard_rejects_non_mirror():
    with pytest.raises(ValueError):
        _assert_safe_table("companies")
    with pytest.raises(ValueError):
        _assert_safe_table("helixis_x; drop table y")
    _assert_safe_table("helixis_tenants")  # ok


# ── capabilities / config ────────────────────────────────────────────────────


def test_sandbox_gating_rejects_prod_creds():
    caps = config.Capabilities(
        llm_key=True,
        db_url=True,
        buildium_creds=True,
        buildium_sandbox=False,
        browserbase=False,
        security_extra=True,
        langfuse=False,
    )
    reason = caps.skip_reason("buildium_sandbox")
    assert reason and "sandbox" in reason.lower()


def test_capabilities_all_present_no_skip():
    caps = config.Capabilities(
        llm_key=True,
        db_url=True,
        buildium_creds=True,
        buildium_sandbox=True,
        browserbase=True,
        security_extra=True,
        langfuse=True,
    )
    assert caps.skip_reason("llm_key", "db_url", "buildium_sandbox") is None


def test_company_ids_are_distinct_uuids():
    assert config.COMPANY_A_ID != config.COMPANY_B_ID
    assert len(config.COMPANY_A_ID) == 36 and len(config.COMPANY_B_ID) == 36


def test_rc_sha_never_empty():
    assert config.rc_sha()  # resolves to a sha or "unknown", never ""


# ── fixtures integrity ───────────────────────────────────────────────────────


def test_b_canaries_present_in_b_rows():
    rows = fixtures.company_b_rows(config.COMPANY_B_ID, config.COMPANY_B_ACCOUNT_ID)
    blob = repr([r.columns for r in rows])
    # every leak canary must actually be seeded somewhere in B's data
    for canary in fixtures.B_LEAK_CANARIES:
        assert canary in blob, f"canary {canary!r} not seeded — suite 2 would never catch a leak"


def test_dirty_cases_cover_required_shapes():
    keys = {c.key for c in fixtures.dirty_cases(config.COMPANY_A_ID, 1)}
    assert {
        "unit_missing_property",
        "null_rent",
        "duplicate_tenants",
        "unicode_name",
        "lease_no_dates",
        "zero_dollar_bill",
    } <= keys


def test_redaction_seed_carries_pii_shapes():
    row = fixtures.redaction_seed_row(config.COMPANY_A_ID, 1)
    comment = row.columns["raw"]["Comment"]
    assert fixtures.REDACTION_CARD in comment and fixtures.REDACTION_SSN in comment


def test_bill_rows_populate_promoted_columns():
    # mirror tools read total_amount/is_paid columns, not raw — regression guard
    rows = fixtures.company_b_rows(config.COMPANY_B_ID, 1)
    bill = next(r for r in rows if r.table == "helixis_bills")
    assert "total_amount" in bill.columns and "is_paid" in bill.columns


# ── report renderer ──────────────────────────────────────────────────────────


def _suite(number: int, status: Status, outcomes=None) -> SuiteResult:
    return SuiteResult(
        number=number,
        name=f"Suite {number}",
        pass_criterion="crit",
        status=status,
        outcomes=outcomes or [],
    )


def test_report_do_not_release_on_fail():
    results = [_suite(2, Status.FAIL), _suite(4, Status.PASS)]
    md = render_report(results, rc_sha="abc123", langfuse_active=False)
    assert "DO NOT RELEASE" in md and "abc123" in md


def test_report_inconclusive_when_all_skipped():
    results = [_suite(2, Status.SKIPPED), _suite(4, Status.SKIPPED)]
    md = render_report(results, rc_sha="abc123", langfuse_active=False)
    assert "INCONCLUSIVE" in md


def test_report_all_pass_banner():
    good = _suite(2, Status.PASS, [ScenarioOutcome("s", "d", True, 4, 4, quality_pass_count=4)])
    md = render_report([good], rc_sha="deadbeef", langfuse_active=True)
    assert "ALL LIVE SUITES PASS" in md


def test_report_filename_shape():
    assert report_filename("abc123", date="2026-07-14") == "2026-07-14-abc123.md"


def test_suite_result_finalize_derives_status():
    r = _suite(
        9,
        Status.PASS,
        [ScenarioOutcome("a", "d", True, 1, 1), ScenarioOutcome("b", "d", False, 1, 0)],
    )
    r.finalize()
    assert r.status is Status.FAIL


# ── fault transport shaping ──────────────────────────────────────────────────


def test_fault_transport_fails_then_recovers():
    import httpx

    from helixis.evals.live import faults

    # Exercise the handler directly (no BuildiumClient / settings needed).
    counter = faults._Counter()
    handler = faults._handler(
        counter,
        status=429,
        fail_first_n=2,
        only_writes=False,
        success_payload={"Id": 9},
        timeout=False,
    )
    req = httpx.Request("POST", "https://apisandbox.buildium.com/v1/workorders")
    assert handler(req).status_code == 429
    assert handler(req).status_code == 429
    assert handler(req).status_code == 200
    assert counter.calls == 3


def test_fault_transport_only_writes_passes_reads():
    import httpx

    from helixis.evals.live import faults

    counter = faults._Counter()
    handler = faults._handler(
        counter,
        status=500,
        fail_first_n=None,
        only_writes=True,
        success_payload={},
        timeout=False,
    )
    get = httpx.Request("GET", "https://apisandbox.buildium.com/v1/workorders/1")
    post = httpx.Request("POST", "https://apisandbox.buildium.com/v1/workorders")
    assert handler(get).status_code == 200  # reads pass through
    assert handler(post).status_code == 500  # writes fail


# ── the live gate must not key on a retired provider ─────────────────────
#
# The capability was ``google_key``, reading HELIXIS_LLM__GOOGLE_API_KEY. That
# key died on 2026-07-21 (gotcha 18) and settings.py annotates the field as
# dead — so suites 2, 8, 9 and suite 5's honesty leg gated on a deleted key for
# five weeks and could only ever report SKIPPED. The report says a skip is not a
# pass, which is honest; what nothing said is that it could never become one.
# Found 2026-08-28 by RUNNING suite 8, not by reading it.


def test_the_agent_driving_capability_is_satisfied_by_the_live_provider() -> None:
    """The concrete regression: an Anthropic-only environment — which is every
    environment we have — must be able to run the agent-driven suites."""
    from helixis.evals.live.config import resolve_capabilities
    from helixis.settings import Settings

    caps = resolve_capabilities(Settings(env="test", llm={"anthropic_api_key": "sk-test"}))
    assert caps.llm_key, "an Anthropic key does not satisfy the live gate's LLM capability"


def test_no_key_at_all_still_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction. A gate that always passes is not a gate.

    ⚠ THE ENVIRONMENT HAS TO BE CLEARED, and my first version did not do it.
    ``Settings(llm={})`` does not mean "no keys" — pydantic-settings still reads
    ``HELIXIS_LLM__*`` from the environment, so this passed on a laptop with no
    key and FAILED in CI, which has one. An environment-dependent test is worse
    than no test: it reports the developer's machine, not the code.
    """
    from helixis.evals.live.config import _llm_key_fields, resolve_capabilities
    from helixis.settings import LLMSettings, Settings

    for field in _llm_key_fields(LLMSettings()):
        monkeypatch.delenv(f"HELIXIS_LLM__{field.upper()}", raising=False)
    caps = resolve_capabilities(Settings(env="test", llm={}))
    assert not caps.llm_key


def test_the_capability_reads_every_provider_key_the_settings_declare() -> None:
    """⚠ Reads the SETTINGS MODEL, not a list typed here — gotcha 45's own
    remedy. A test naming providers by hand would go stale exactly the way the
    thing it replaces did, and the failure mode is silent: the gate keeps
    printing SKIPPED and nobody learns it can no longer pass.

    The embedder is excluded deliberately: Voyage drives no turn, so a Voyage
    key alone must NOT make the agent suites look runnable.
    """
    from helixis.evals.live.config import _llm_key_fields
    from helixis.settings import LLMSettings

    fields = _llm_key_fields(LLMSettings())
    assert "anthropic_api_key" in fields, "the live provider is not recognised"
    assert "voyage_api_key" not in fields, "the memory embedder cannot drive a turn"
    declared = {n for n in type(LLMSettings()).model_fields if n.endswith("_api_key")}
    assert set(fields) <= declared
    assert declared - set(fields) == {"voyage_api_key"}, (
        "a provider key was added to LLMSettings and the live gate does not "
        "know about it — or one was removed and this list is now stale"
    )
