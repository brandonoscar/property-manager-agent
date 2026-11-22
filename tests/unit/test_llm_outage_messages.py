"""What a user is told when the model is unreachable.

Two live bugs, 2026-08-23/24:

**A spend ceiling was reported as "something went wrong… please try again."**
The account hit its cap; every run failed with LiteLLM's ``BadRequestError``
carrying *"You have reached your specified API usage limits. You will regain
access on 2026-09-01"*. That is a KNOWN operational state with a KNOWN
duration, and "try again" is actively wrong advice for it — the request could
not succeed for eight days. A wrong instruction is worse than no message.

**The transient-outage message named "Google's API"** — nine months after the
migration to Anthropic (gotcha 18). Every transient failure blamed a vendor no
longer in the stack. A user-facing string naming a dependency outlives that
dependency unless something checks; this file is that check.

⚠ The TYPE cannot classify a quota error. LiteLLM wraps Anthropic's quota
refusal in ``BadRequestError``, the same class it uses for genuinely malformed
requests, so the message text is the only available signal.
"""

from __future__ import annotations

import inspect

from helixis.agents.orchestrator import _capacity_message, run_agent_streaming

#: The exact wire text observed in production, 2026-08-23.
_LIVE_QUOTA = (
    "litellm.BadRequestError: AnthropicException - "
    '{"type":"error","error":{"type":"invalid_request_error","message":'
    '"You have reached your specified API usage limits. You will regain '
    'access on 2026-09-01 at 00:00 UTC."}}'
)


def test_the_live_quota_error_is_recognised() -> None:
    """Pinned to the real string, not a paraphrase — a classifier tuned to
    invented text is tuned to nothing."""
    msg = _capacity_message(RuntimeError(_LIVE_QUOTA))
    assert msg is not None
    assert "usage limit" in msg.lower()


def test_the_message_says_retrying_will_not_help() -> None:
    """THE point. Generic text told users to retry a request that could not
    succeed for eight days."""
    msg = _capacity_message(RuntimeError(_LIVE_QUOTA)) or ""
    assert "won't help" in msg or "will not help" in msg
    assert "isn't a problem with your request" in msg


def test_it_does_not_leak_the_provider_or_the_console() -> None:
    """This reaches property managers, not operators. Say the true, useful
    thing; don't name a vendor or a billing page."""
    low = (_capacity_message(RuntimeError(_LIVE_QUOTA)) or "").lower()
    for leak in ("anthropic", "console", "billing", "litellm", "api key"):
        assert leak not in low, f"capacity message leaks {leak!r}"


def test_ordinary_failures_keep_the_generic_message() -> None:
    """Returning a capacity message for everything would be the same bug in
    the other direction — a real crash reported as a billing limit."""
    for exc in (ValueError("boom"), KeyError("nope"), RuntimeError("connection reset")):
        assert _capacity_message(exc) is None


def test_a_burst_rate_limit_is_not_a_capacity_ceiling() -> None:
    """⚠ INVERTED 2026-08-26. This asserted the opposite, and the opposite was
    wrong — the only assertion in this file that carried no reasoning.

    A 429 ``rate_limit_error`` is a BURST limit: too many requests this minute,
    clears in seconds. The monthly spend cap is a different thing that happens
    to share the English word "limit". Collapsing them meant an ordinary 429
    told the user *"retrying won't help until the limit is raised — please let
    your administrator know"*, which is false, and is the advice most likely to
    make someone abandon a product over a condition that fixes itself.

    ``llm/provider_errors`` (#908) had classed ``rate_limit_error`` TRANSIENT
    three days after this line was written, with the reasoning attached, and
    the eval runner's transient/terminal retry split (#883) already depended on
    that. So two layers disagreed about one string. The rule now lives in ONE
    place and this file reads it — the precedent is ``bill_amount`` (gotcha 25/46).

    ``None`` here is not an absence of an answer: it routes the user to the
    caller's generic "please try again", which is the correct advice for a
    moment.

    Replacement coverage: ``test_capacity_message.py`` parametrizes BOTH marker
    sets from ``provider_errors``, so neither direction can drift again.
    """
    assert _capacity_message(RuntimeError("litellm.RateLimitError: rate_limit_error")) is None


def test_low_balance_is_capacity_too() -> None:
    """Unchanged, and note it is a genuinely different case from the one above:
    an empty balance is a STATE that persists until somebody pays."""
    assert _capacity_message(RuntimeError("Your credit balance is too low")) is not None


# ── the stale-vendor guard ────────────────────────────────────────────────


def test_no_user_facing_message_names_a_retired_provider() -> None:
    """The transient message said "Google's API" long after Gemini was gone
    (gotcha 18). Anthropic is the only live provider; a message naming any
    other is stale by construction."""
    src = inspect.getsource(run_agent_streaming)
    for line in src.splitlines():
        if "error_event(" in line or (line.strip().startswith('"') and "API" in line):
            for dead in ("Google", "Gemini", "Vertex", "OpenAI"):
                assert dead not in line, (
                    f"user-facing string names retired provider {dead}: {line.strip()[:90]}"
                )


# ── guard self-test ───────────────────────────────────────────────────────


def test_the_classifier_can_return_false() -> None:
    """Guard self-test — the convention this repo now runs on.

    Four guards in two days passed against code they were written to catch:
    a dead E2B sandbox, an AST scan run on unfixed source, a fixed-width
    window that read 0 as "absent", and an id matcher that saw one idiom of
    four. A guard nobody has watched fail is not evidence.

    So: prove this classifier discriminates, rather than saying yes to
    everything.
    """
    assert _capacity_message(RuntimeError(_LIVE_QUOTA)) is not None
    assert _capacity_message(RuntimeError("a perfectly ordinary crash")) is None
