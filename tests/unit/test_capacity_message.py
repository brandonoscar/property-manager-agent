"""What a user is told when the model provider refuses.

Three outcomes, and getting them confused costs real things:

* **quota** — the account is out of budget until a date the provider names.
  Retrying is guaranteed waste. A human has to raise a limit.
* **credentials** — the key is wrong, rotated, or unauthorised. Retrying is
  guaranteed waste. A different human has to fix configuration.
* **transient** — a 429 burst limit, an overload, a dropped socket. Retrying is
  exactly right, and telling someone it is hopeless is a false statement about
  a condition that fixes itself in seconds.

⚠ WHAT WENT WRONG (found 2026-08-26 on live ``orchestrator_runs`` rows)
------------------------------------------------------------------------
``_capacity_message`` carried its OWN marker list, written 2026-08-23, three
days before ``llm/provider_errors`` centralised the rule. The two disagreed in
both directions at once — the shape gotcha 38 warns about, where one direction
hides the other:

* the chat list called ``rate_limit_error`` a capacity ceiling; the shared rule
  calls it TRANSIENT. So an ordinary 429 told the user to go find an
  administrator.
* the chat list missed ``authenticationerror`` / ``invalid_api_key`` /
  ``permission_denied`` / a bare ``credit balance``, so a dead key produced
  "Something went wrong… please try again" — gotcha 46's original bug, on the
  surface every user actually reads.

The fix is not a longer list. It is ONE list, in the module that owns the
question, exactly as ``buildium/money.py::bill_amount`` owns "what is a bill
worth" (gotcha 25/46's precedent). This file reads BOTH sides (gotcha 28) so
they cannot drift apart again.
"""

from __future__ import annotations

import litellm
import pytest

from helixis.agents.orchestrator import _capacity_message
from helixis.llm.provider_errors import (
    CREDENTIAL_MARKERS,
    TERMINAL_PROVIDER_MARKERS,
    TRANSIENT_PROVIDER_MARKERS,
    terminal_cause,
)

#: The exact wire text Anthropic returned on 2026-08-25/26, from the
#: ``orchestrator_runs.error_message`` column. Not paraphrased — the whole
#: point is that the message is the only signal, so the test has to use the
#: real one.
LIVE_QUOTA_TEXT = (
    "litellm.BadRequestError: AnthropicException - "
    '{"type":"error","error":{"type":"invalid_request_error","message":'
    '"You have reached your specified API usage limits. You will regain '
    'access on 2026-09-01 at 00:00 UTC."}}'
)


def test_the_live_spend_cap_says_a_human_must_act() -> None:
    """The production error, verbatim, must not read as "try again"."""
    msg = _capacity_message(litellm.BadRequestError(LIVE_QUOTA_TEXT, "anthropic", "claude"))
    assert msg is not None, "the real spend-cap error fell through to the generic message"
    assert "retrying won't help" in msg
    assert "administrator" in msg


def test_a_burst_rate_limit_is_not_a_spend_cap() -> None:
    """⚠ THE regression. A 429 clears in seconds.

    Telling a user "retrying won't help until the limit is raised — let your
    administrator know" about a burst limit is false, and it is the advice most
    likely to make them stop using the product over a condition that fixes
    itself. ``None`` here means the caller keeps its generic "please try
    again", which is the correct thing to say.
    """
    for text in (
        '{"type":"error","error":{"type":"rate_limit_error","message":"Number of '
        'requests has exceeded your rate limit"}}',
        '{"error":{"type":"overloaded_error","message":"Overloaded"}}',
    ):
        assert _capacity_message(litellm.RateLimitError(text, "anthropic", "claude")) is None, (
            f"a transient failure was described as permanent: {text[:60]}"
        )


def test_a_dead_key_does_not_tell_the_user_to_try_again() -> None:
    """The other direction. Gotchas 1, 11 and 18 are all credential deaths —
    a rotated Composio key, a rotated Fernet key, an entire provider account
    cut off. When it happens the whole product fails this way at once, and
    "please try again" is what every user would have seen."""
    exc = litellm.AuthenticationError("invalid x-api-key", "anthropic", "claude")
    msg = _capacity_message(exc)
    assert msg is not None, "a credential failure fell through to 'please try again'"
    assert "retrying won't help" in msg
    assert "administrator" in msg


def test_quota_and_credentials_do_not_say_the_same_thing() -> None:
    """Both are terminal and both need a human — but different humans doing
    different things. Collapsing them sends someone to the billing page for a
    key problem."""
    quota = _capacity_message(litellm.BadRequestError(LIVE_QUOTA_TEXT, "anthropic", "claude"))
    creds = _capacity_message(litellm.AuthenticationError("invalid_api_key", "a", "c"))
    assert quota != creds
    assert "usage limit" in (quota or "")
    assert "usage limit" not in (creds or "")


def test_an_ordinary_bug_keeps_the_generic_message() -> None:
    """⚠ A malformed request is OUR defect, not an outage (gotcha 46's rule).
    Laundering a 400 for a bad payload into "we're out of budget" would hide
    exactly the failure gotcha 27 describes, where one malformed tool example
    400'd every call in the suite."""
    assert _capacity_message(ValueError("some ordinary bug")) is None
    assert (
        _capacity_message(
            litellm.BadRequestError(
                "tools.41.custom: Example at index 0 is invalid", "anthropic", "claude"
            )
        )
        is None
    )


# ── both sides ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("marker", TERMINAL_PROVIDER_MARKERS)
def test_every_terminal_marker_reaches_the_user_message(marker: str) -> None:
    """⚠ Reads the shared rule's OWN list, not a hand-written copy.

    A hand-written list is how the drift happened: both functions were
    self-consistent and correct on their own terms, and simply disagreed. If
    someone adds a terminal marker to ``provider_errors`` and the chat path
    cannot see it, that user gets "please try again" forever.
    """
    msg = _capacity_message(litellm.BadRequestError(f"error: {marker}", "anthropic", "claude"))
    assert msg is not None, (
        f"{marker!r} is terminal in provider_errors but produces the generic "
        "'please try again' message in chat"
    )


@pytest.mark.parametrize("marker", TRANSIENT_PROVIDER_MARKERS)
def test_no_transient_marker_is_described_as_permanent(marker: str) -> None:
    """The mirror assertion, and the one that would have caught the live bug.
    Testing only the terminal direction is what let ``rate_limit_error`` sit on
    the wrong side for three days."""
    assert _capacity_message(litellm.APIError(429, f"error: {marker}", "anthropic", "claude")) is (
        None
    ), f"{marker!r} is transient in provider_errors but chat calls it a permanent limit"


def test_the_two_marker_sets_are_disjoint() -> None:
    """The invariant underneath both tests above. If a string were in both
    lists, ``classify_provider_failure``'s terminal-wins ordering would decide
    it silently and the two tests would contradict each other."""
    overlap = {t for t in TERMINAL_PROVIDER_MARKERS for x in TRANSIENT_PROVIDER_MARKERS if t == x}
    assert not overlap, f"a marker is both terminal and transient: {overlap}"


def test_credential_markers_are_a_subset_of_terminal() -> None:
    """``terminal_cause`` returns "credentials" only after
    ``classify_provider_failure`` has already said terminal. A credential
    marker missing from the terminal list would be classified transient and
    retried against a key that will never work."""
    missing = [m for m in CREDENTIAL_MARKERS if m not in TERMINAL_PROVIDER_MARKERS]
    assert not missing, f"credential markers unreachable because they aren't terminal: {missing}"
    for marker in CREDENTIAL_MARKERS:
        assert terminal_cause(f"boom {marker}") == "credentials"
