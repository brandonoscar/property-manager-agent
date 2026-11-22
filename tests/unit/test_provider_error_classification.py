"""A terminal provider failure must survive the trip from the call site to
whatever decides to retry.

The defect (2026-08-26, found in a red backend job on a FRONTEND-ONLY PR):

``document_tools._call_llm``'s catch-all logged the provider's message and
returned the bare token ``"llm_unavailable"``. The eval runner reads that as
TRANSIENT — correctly, since it is the generic degraded-path token — and
retried. So when the Anthropic monthly spend cap was exhausted, with the 400
literally saying *"You will regain access on 2026-09-01"*, the four multimodal
document cases each burned three attempts against a quota that could not
answer. Twelve impossible calls per run.

The runner has named ``"usage limits"`` as terminal since #882 and its
TERMINAL-WINS rule was correct the whole time. It simply never received the
string. **A classification is only as good as the worst layer that forwards
its input** — the same shape as gotcha 25, where a field-shape fix reached the
agent tool and not the mirror.

⚠ These read BOTH sides (gotcha 28): the producer (``failure_warnings``, and
the tool call path that emits it) and the consumer (the runner's marker tuples
and ``retryable_infrastructure``). A test covering only the classifier would
have passed happily throughout the bug — ``classify_provider_failure`` did not
exist, and the runner's own list was already right.
"""

from __future__ import annotations

import pytest

from helixis.agents.document_tools import _call_llm, _empty_extraction
from helixis.evals.runner import (
    _TERMINAL_INFRA_MARKERS,
    _TRANSIENT_INFRA_MARKERS,
    AssertionResult,
    CaseResult,
)
from helixis.llm.provider_errors import (
    WARN_QUOTA_EXHAUSTED,
    WARN_UNAUTHORIZED,
    WARN_UNAVAILABLE,
    classify_provider_failure,
    failure_warnings,
)
from helixis.settings import get_settings

#: The exact body Anthropic returned on 2026-08-26, kept verbatim. A
#: paraphrase would let the substring drift out from under the check — the
#: message is the contract here, not our summary of it.
SPEND_CAP_400 = (
    "litellm.BadRequestError: AnthropicException - "
    '{"type":"error","error":{"type":"invalid_request_error","message":'
    '"You have reached your specified API usage limits. You will regain '
    'access on 2026-09-01 at 00:00 UTC."},"request_id":"req_011CeRYqF6ku"}'
)

OVERLOADED_529 = (
    "litellm.InternalServerError: AnthropicException - "
    '{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'
)


# ── the classifier, both directions ─────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        SPEND_CAP_400,
        "Your credit balance is too low to access the Anthropic API",
        "AuthenticationError: invalid x-api-key",
        "PERMISSION_DENIED: caller does not have permission",
    ],
)
def test_terminal_failures_are_terminal(text: str) -> None:
    assert classify_provider_failure(text) == "terminal"


@pytest.mark.parametrize(
    "text",
    [OVERLOADED_529, "rate_limit_error", "APIConnectionError: connection reset", "Timeout"],
)
def test_transient_failures_are_transient(text: str) -> None:
    assert classify_provider_failure(text) == "transient"


def test_a_malformed_request_is_not_laundered_into_an_outage() -> None:
    """⚠ Deliberate exclusion. Gotcha 27's malformed ``input_examples`` 400'd
    EVERY call in the suite; classing our own bad payload as "the provider was
    down" would suppress the one signal that finds it. Terminal here means the
    account cannot buy tokens, nothing wider."""
    malformed = (
        "litellm.BadRequestError: AnthropicException - tools.41.custom: "
        'Example at index 0 is invalid: true is not of type "string"'
    )
    assert classify_provider_failure(malformed) is None


def test_terminal_wins_when_a_message_names_both() -> None:
    """A call that rate-limited its way into the spend cap is terminal. This
    is the rule the runner has always had and could never apply."""
    both = "rate_limit_error ... eventually: You have reached your specified API usage limits."
    assert classify_provider_failure(both) == "terminal"


# ── the producer: what a failed call actually emits ─────────────────────────


def test_the_generic_token_always_leads() -> None:
    """Every existing consumer checks membership of ``llm_unavailable`` — the
    document agent's prompt rule 7, ``company_knowledge._FAILURE_WARNINGS``,
    three golden cases' ``warnings_must_not_include``. The cause rides
    ALONGSIDE it; it never replaces it, so none of them change behaviour."""
    for text in (SPEND_CAP_400, OVERLOADED_529, "something unrecognised", None):
        assert failure_warnings(text)[0] == WARN_UNAVAILABLE


def test_a_spend_cap_names_itself() -> None:
    assert failure_warnings(SPEND_CAP_400) == [WARN_UNAVAILABLE, WARN_QUOTA_EXHAUSTED]


def test_a_credential_refusal_names_itself() -> None:
    assert failure_warnings("AuthenticationError: invalid x-api-key") == [
        WARN_UNAVAILABLE,
        WARN_UNAUTHORIZED,
    ]


def test_a_transient_blip_adds_nothing() -> None:
    """Nothing to say beyond "it didn't happen" — and a spurious cause would
    make a retryable failure look terminal, which is this bug inverted."""
    assert failure_warnings(OVERLOADED_529) == [WARN_UNAVAILABLE]


@pytest.mark.asyncio
async def test_the_call_site_forwards_the_cause() -> None:
    """⚠ The assertion that would have caught the live bug.

    Not "does the classifier work" — it does, and did not exist. This is the
    layer that was dropping the message: an exception carrying the spend-cap
    text must come out of ``_call_llm`` still saying so.
    """

    async def _explode(**_kwargs: object) -> object:
        raise RuntimeError(SPEND_CAP_400)

    content, warning = await _call_llm(
        messages=[{"role": "user", "content": "x"}],
        settings=get_settings(),
        timeout_seconds=5.0,
        completion_fn=_explode,
    )
    assert content is None
    assert warning == [WARN_UNAVAILABLE, WARN_QUOTA_EXHAUSTED], (
        f"the provider named a terminal state and the call site returned {warning!r} — "
        "this is the drop that bought a dead quota three retries per case"
    )


def test_a_degraded_result_carries_both_tokens() -> None:
    """The list has to survive into the result object, or the runner still
    never sees it — the failure surfaces as a WARNING on a returned result,
    not as an exception."""
    result = _empty_extraction(None, failure_warnings(SPEND_CAP_400))
    assert result.warnings == [WARN_UNAVAILABLE, WARN_QUOTA_EXHAUSTED]

    # And a plain string still works — every other call site passes one.
    assert _empty_extraction(None, "empty_document").warnings == ["empty_document"]


# ── the consumer: the runner's retry decision ───────────────────────────────


def _case_from_warnings(warnings: list[str]) -> CaseResult:
    """Reproduce what a ``tool_invocation`` document case produces: the
    ``warnings_must_not_include['llm_unavailable']`` prohibition fires, with
    the reason the runner now builds."""
    return CaseResult(
        case_id="020_extract_lease",
        assertions=[
            AssertionResult(
                name="warnings_must_not_include[0]='llm_unavailable'",
                passed=False,
                reason=f"forbidden warning 'llm_unavailable' present in warnings={warnings!r}",
                critical=True,
            )
        ],
    )


def test_a_spend_cap_case_is_infrastructure_and_is_not_retried() -> None:
    """End to end, the property that was violated: twelve impossible calls."""
    case = _case_from_warnings([WARN_UNAVAILABLE, WARN_QUOTA_EXHAUSTED])
    assert case.failure_kind == "infrastructure"
    assert not case.retryable_infrastructure, (
        "the spend cap was visible in the reason and the runner still chose to retry"
    )


def test_a_transient_blip_is_still_retried() -> None:
    """⚠ The other direction. The whole reason #883 split the marker lists was
    that a capacity blip with no self-service re-run left a PR stuck. Making
    the spend cap terminal must not take that back."""
    case = _case_from_warnings([WARN_UNAVAILABLE])
    assert case.failure_kind == "infrastructure"
    assert case.retryable_infrastructure


def test_the_runner_and_the_tools_agree_on_what_terminal_means() -> None:
    """Reads BOTH sides. If a token the tools can emit is not in the runner's
    terminal set, the tools are labelling something the runner will retry
    anyway — the exact disagreement this PR fixes, re-opened."""
    for token in (WARN_QUOTA_EXHAUSTED, WARN_UNAUTHORIZED):
        assert token in _TERMINAL_INFRA_MARKERS, (
            f"{token!r} is emitted on a terminal failure but the runner does not treat it as one"
        )
    assert WARN_UNAVAILABLE in _TRANSIENT_INFRA_MARKERS
    overlap = set(_TERMINAL_INFRA_MARKERS) & set(_TRANSIENT_INFRA_MARKERS)
    assert not overlap, f"a marker cannot be both: {overlap}"


def test_a_real_forbidden_call_is_not_laundered_by_a_concurrent_outage() -> None:
    """⚠ #882's asymmetry, re-pinned because this PR widened the terminal set.

    An agent that called a forbidden tool and THEN hit the spend cap made a
    forbidden call. The 400 does not absolve it, so the case stays at
    ``safety`` — the loudest word the harness has, and the one that must never
    be spendable on an outage.
    """
    case = CaseResult(
        case_id="130_no_write_via_code_execution",
        assertions=[
            AssertionResult(
                name="tool_calls_must_not_contain[0]='apply_actions'",
                passed=False,
                reason="forbidden tool 'apply_actions' was called",
                critical=True,
            ),
            AssertionResult(
                name="reply_must_not_be_empty",
                passed=False,
                reason=SPEND_CAP_400,
                critical=False,
            ),
        ],
    )
    assert case.failure_kind == "safety"
