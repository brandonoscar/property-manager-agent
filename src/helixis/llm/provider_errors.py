"""Is a provider failure a MOMENT or a STATE? One rule, several readers.

A model provider fails in two materially different ways and the difference is
worth money:

* **Transient** — capacity pressure, a connection reset, a 5xx. The same call
  succeeds seconds later, so retrying is the correct response.
* **Terminal** — the monthly spend cap is exhausted (the error names the date
  access returns), the credit balance is zero, the key is wrong. Retrying is
  *guaranteed* waste: it spends latency, burns the retry budget, and on a
  metered endpoint can spend money to be told "no" three more times.

The eval runner has encoded this distinction since #882 and encodes it
correctly. The bug this module fixes is that the distinction was being made
one layer ABOVE where the information exists, and the layer below threw the
information away.

``document_tools._call_llm`` caught every non-specific provider exception,
logged the provider's own words, and returned the bare token
``"llm_unavailable"`` — which the runner (rightly) reads as transient. So on
2026-08-26, with the Anthropic spend cap exhausted until 2026-09-01, the four
multimodal document cases each failed, were classed transient, and were
retried three times apiece against a quota that was gone. Twelve calls that
could not have succeeded, on every pull request that ran them, plus a red
backend job on a frontend-only PR.

This is gotcha 25's rule turned on the rule itself: a field-shape fact must be
handled at every layer that reads it, which is easiest to guarantee when there
is only one place that decides it.

⚠ **A malformed request is deliberately NOT terminal-infrastructure.** A 400
for a bad payload is our defect, not the provider's outage — gotcha 27's
malformed ``input_examples`` 400'd every call in the suite and it would be
actively harmful to launder that into "the API was down". Terminal here means
*the account cannot currently buy tokens*, nothing wider.
"""

from __future__ import annotations

from typing import Literal

#: The account cannot buy tokens right now, and no amount of retrying changes
#: that. Lower-cased substring match against the provider's message.
TERMINAL_PROVIDER_MARKERS: tuple[str, ...] = (
    "usage limits",  # Anthropic spend cap; the message names the reset date
    "credit balance",  # out of credits
    "quota exceeded",
    "insufficient_quota",
    "authenticationerror",
    "invalid_api_key",
    "invalid x-api-key",
    "permission_denied",
)

#: A moment, not a state — the provider was busy or the socket broke.
#:
#: ⚠ ``rate_limit_error`` lives HERE, and that placement is load-bearing. A 429
#: is a burst limit that clears in seconds; it is NOT the monthly spend cap,
#: even though both are colloquially "a rate limit". The chat path used to
#: class it terminal and tell the user "retrying won't help — let your
#: administrator know", which is false and is the single worst thing to say
#: about a condition a retry actually fixes.
TRANSIENT_PROVIDER_MARKERS: tuple[str, ...] = (
    "rate_limit_error",
    "overloaded_error",
    "service_unavailable",
    "apiconnectionerror",
    "internalservererror",
    "timeout",
)

#: The subset of terminal markers that mean "the credential is wrong or
#: unauthorised" rather than "the money ran out". Both are terminal and both
#: need a human, but they need DIFFERENT humans doing different things, so the
#: split has to survive out to the user-facing message.
CREDENTIAL_MARKERS: tuple[str, ...] = (
    "authenticationerror",
    "invalid_api_key",
    "invalid x-api-key",
    "permission_denied",
)

#: Warning token meaning "the account is out of tokens/credit". Distinct from
#: ``llm_unavailable`` (which stays, and still means "no extraction happened")
#: because "temporarily unavailable" is a false statement about a spend cap
#: that lasts until a named date — the same silent-degradation class as
#: gotcha 17, where a broken embedder looked like an empty result for 19 days.
WARN_QUOTA_EXHAUSTED = "llm_quota_exhausted"

#: Warning token for a credential/permission refusal.
WARN_UNAUTHORIZED = "llm_unauthorized"

#: The generic "no answer came back" token. Always emitted, so every existing
#: consumer (the document agent's prompt rule 7, the golden cases'
#: ``warnings_must_not_include``) keeps working unchanged; the specific token
#: rides ALONGSIDE it rather than replacing it.
WARN_UNAVAILABLE = "llm_unavailable"

Classification = Literal["terminal", "transient"]

#: WHY a terminal failure is terminal. ``None`` from :func:`terminal_cause`
#: means "not terminal" — transient, or unrecognised.
TerminalCause = Literal["quota", "credentials"]


def classify_provider_failure(text: str | None) -> Classification | None:
    """Classify a provider error message. ``None`` = not recognisably either.

    ⚠ **Terminal wins.** A call that rate-limited its way into the spend cap
    names both; spending the retry budget against a dead quota is precisely
    the waste this exists to stop. The eval runner's
    ``retryable_infrastructure`` has always had this rule — it just could not
    see the terminal marker, because the message never reached it.
    """
    if not text:
        return None
    lowered = text.lower()
    if any(marker in lowered for marker in TERMINAL_PROVIDER_MARKERS):
        return "terminal"
    if any(marker in lowered for marker in TRANSIENT_PROVIDER_MARKERS):
        return "transient"
    return None


def terminal_cause(text: str | None) -> TerminalCause | None:
    """Which KIND of terminal failure this is, or ``None`` if it is not one.

    The two causes need different sentences said to different people: "the
    account is out of budget" is a billing action, "the key is wrong" is a
    configuration action. Anything transient or unrecognised returns ``None``,
    so a caller that branches on this cannot accidentally describe a five
    second capacity blip as a permanent state.
    """
    if classify_provider_failure(text) != "terminal":
        return None
    lowered = (text or "").lower()
    if any(marker in lowered for marker in CREDENTIAL_MARKERS):
        return "credentials"
    return "quota"


def failure_warnings(text: str | None) -> list[str]:
    """The warning list for a failed model call.

    Always leads with :data:`WARN_UNAVAILABLE` — the extraction did not happen,
    which is what every downstream consumer needs to know first — and appends
    the specific cause when the provider named one. A caller that only checks
    membership of ``llm_unavailable`` behaves exactly as before.
    """
    cause = terminal_cause(text)
    if cause is None:
        return [WARN_UNAVAILABLE]
    if cause == "credentials":
        return [WARN_UNAVAILABLE, WARN_UNAUTHORIZED]
    return [WARN_UNAVAILABLE, WARN_QUOTA_EXHAUSTED]
