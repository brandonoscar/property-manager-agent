"""A transient provider blip must be retried; a spend cap must not be.

#882 gave the runner an ``infrastructure`` failure kind and the rule "never
re-roll it" — correct, because it was written against a SPEND CAP whose error
names the date access returns. Retrying that is guaranteed waste.

But the marker list it keys on also held ``overloaded_error``,
``rate_limit_error`` and friends, which are a MOMENT rather than a state. So
one transient Anthropic capacity blip on ``021_extract_invoice`` red-gated a PR
that had changed a single benchmark script (2026-08-25), and there was no
self-service recovery: the runner refused to re-roll it and gotcha 5 means a
session cannot re-run a failed job.

**A rule that is right for one member of a set is not thereby right for the
set.** These tests pin the split, and specifically pin the two ways the fix
could be quietly inert.
"""

from __future__ import annotations

import asyncio

import pytest

from helixis.evals import runner
from helixis.evals.runner import AssertionResult, CaseResult

# The literal Anthropic body from the CI log that motivated this.
_OVERLOADED = (
    "litellm.InternalServerError: AnthropicError - "
    '{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"},'
    '"request_id":"req_011CePcqB1eoztbhF98HakK6"}'
)
# The literal #882 spend-cap body.
_SPEND_CAP = (
    "litellm.BadRequestError: AnthropicException - You have exceeded your "
    "specified API usage limits. You will regain access on 2026-09-01 at 00:00 UTC"
)


def _errored(error: str) -> CaseResult:
    return CaseResult(case_id="021_extract_invoice", error=error)


# ── the classification split ─────────────────────────────────────────


def test_transient_overload_is_retryable() -> None:
    result = _errored(_OVERLOADED)
    assert result.failure_kind == "infrastructure"
    assert result.retryable_infrastructure is True


def test_spend_cap_is_never_retryable() -> None:
    """The #882 case. Retrying spends the retry against a quota that is gone."""
    result = _errored(_SPEND_CAP)
    assert result.failure_kind == "infrastructure"
    assert result.retryable_infrastructure is False


@pytest.mark.parametrize("marker", runner._TERMINAL_INFRA_MARKERS)
def test_every_terminal_marker_refuses_retry(marker: str) -> None:
    assert _errored(f"provider said: {marker}").retryable_infrastructure is False


@pytest.mark.parametrize("marker", runner._TRANSIENT_INFRA_MARKERS)
def test_every_transient_marker_allows_retry(marker: str) -> None:
    assert _errored(f"provider said: {marker}").retryable_infrastructure is True


def test_the_two_sets_are_disjoint_and_cover_the_whole_list() -> None:
    """The union IS the classification list, so a marker cannot be added to one
    set and silently drop out of ``infrastructure`` classification entirely."""
    terminal = set(runner._TERMINAL_INFRA_MARKERS)
    transient = set(runner._TRANSIENT_INFRA_MARKERS)
    assert not (terminal & transient)
    assert terminal | transient == set(runner._INFRA_FAILURE_MARKERS)


def test_terminal_wins_when_a_run_names_both() -> None:
    """A rate-limit retry that finally exhausts the cap must STOP.

    Getting this backwards spends the retry budget against a dead quota — the
    exact waste #882's rule exists to prevent.
    """
    result = _errored(f"{_OVERLOADED}\n...retried...\n{_SPEND_CAP}")
    assert result.retryable_infrastructure is False


def test_a_real_safety_finding_is_never_laundered_into_a_retry() -> None:
    """#882's asymmetry still holds: a forbidden call that THEN hit a blip is
    still a forbidden call, and must not be re-run until it comes out clean."""
    result = CaseResult(
        case_id="320_should_not_act",
        error=_OVERLOADED,
        assertions=[
            AssertionResult(
                name="tool_calls_must_not_contain",
                passed=False,
                critical=True,
                reason="called buildium_record_payment",
            )
        ],
    )
    assert result.failure_kind == "safety"
    assert result.retryable_infrastructure is False


def test_a_passing_case_is_not_retryable() -> None:
    assert CaseResult(case_id="x", assertions=[]).retryable_infrastructure is False


# ── the wiring: the two ways this fix could be inert ─────────────────


def _drive(monkeypatch: pytest.MonkeyPatch, outcomes: list[CaseResult], **kw: object) -> tuple:
    """Run the flake-tolerance wrapper over a scripted sequence of outcomes."""
    calls: list[int] = []
    queue = list(outcomes)

    async def fake_run_case(case, judge=False):  # noqa: ANN001, ARG001
        calls.append(1)
        return queue.pop(0) if queue else outcomes[-1]

    slept: list[float] = []

    async def fake_sleep(d: float) -> None:
        slept.append(d)

    monkeypatch.setattr(runner, "run_case", fake_run_case)
    monkeypatch.setattr(runner.asyncio, "sleep", fake_sleep)
    result = asyncio.run(runner._run_case_with_flake_tolerance({"id": "021"}, **kw))  # type: ignore[arg-type]
    return result, len(calls), slept


def test_a_blip_then_a_pass_is_a_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """The headline behaviour: CI stops going red on a 3-second outage."""
    result, calls, slept = _drive(
        monkeypatch, [_errored(_OVERLOADED), CaseResult(case_id="021", assertions=[])]
    )
    assert result.passed
    assert calls == 2
    assert slept == [2.0], "a provider under load needs a pause, not persistence"


def test_a_spend_cap_is_run_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    result, calls, slept = _drive(monkeypatch, [_errored(_SPEND_CAP)])
    assert result.failure_kind == "infrastructure"
    assert calls == 1, "retrying an exhausted quota just spends the retry"
    assert slept == []


def test_the_retry_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuine outage must not multiply the suite's cost without bound."""
    _, calls, slept = _drive(monkeypatch, [_errored(_OVERLOADED)] * 10)
    assert calls == runner._INFRA_RETRIES + 1 == 3
    assert slept == [2.0, 6.0], "backoff must widen, not hammer"


def test_the_retry_is_not_skipped_for_a_quarantined_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ The trap that would have made this whole fix inert.

    ``skip_quarantined_reroll`` returns early for a quarantined case, because a
    quarantined QUALITY failure is non-gating so re-rolling buys nothing. That
    reasoning does not carry to infrastructure, which gates regardless. If the
    infra retry sat after that guard, it would be skipped for exactly the ~40
    model-driven cases most likely to catch a blip — including
    ``021_extract_invoice`` itself.
    """
    monkeypatch.setattr(runner, "_case_is_quarantined", lambda case: True)
    result, calls, _ = _drive(
        monkeypatch,
        [_errored(_OVERLOADED), CaseResult(case_id="021", assertions=[])],
        skip_quarantined_reroll=True,
    )
    assert result.passed
    assert calls == 2, "a quarantined case must still be retried through a provider blip"


def test_quality_reroll_is_still_skipped_for_a_quarantined_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cost guard the fix must not have broken on its way past."""
    monkeypatch.setattr(runner, "_case_is_quarantined", lambda case: True)
    quality = CaseResult(
        case_id="021",
        assertions=[AssertionResult(name="reply_must_contain", passed=False, critical=False)],
    )
    result, calls, _ = _drive(monkeypatch, [quality], skip_quarantined_reroll=True)
    assert result.failure_kind == "quality"
    assert calls == 1


def test_failure_kind_still_reads_infrastructure_after_a_failed_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ ``failure_kind`` has TWO consumers — ``main`` and ``browser-sweep.yml``'s
    bash ``case`` — and gotcha 35 was itself a fix that reached only one. This
    change must alter whether we RUN AGAIN and nothing about the string, or the
    sweep's parser meets a kind it predates all over again."""
    result, _, _ = _drive(monkeypatch, [_errored(_OVERLOADED)] * 5)
    assert result.failure_kind == "infrastructure"
