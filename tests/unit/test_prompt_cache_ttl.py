"""The prompt cache must outlive the gap between two questions.

MEASURED, on the first real turn after the 2026-08-26 boot fix — "what's the
rent on unit 4B", 4 calls, 13.3s, ``cost_usd`` 0.146:

    prompt_tokens        118,879
    cached_tokens         65,906   ($0.20/M)  -> $0.0132
    cache_write_tokens    44,148   ($2.50/M)  -> $0.1104   <- 76% of the turn
    fresh_prompt_tokens    8,825   ($2.00/M)  -> $0.0177
    completion_tokens        480  ($10.00/M)  -> $0.0048
                                               = $0.1461, vs LiteLLM's 0.146

**Three quarters of that turn was cache WRITES.** Anthropic's default cache
lives five minutes, refreshed on use — so it only helps under sustained
traffic. A property manager asking something every twenty minutes never gets a
hit and re-pays the write on every single turn, which is the realistic shape
of this product's usage. The 55% hit rate above is what that looks like.

A 1-hour write costs $4.00/M — 1.6x a five-minute write for a cache that lives
twelve times longer — so it pays for itself on the SECOND turn inside the hour:

    turns/hr   5m TTL    1h TTL   saving
       2       5.00·W    4.20·W     16%
       3       7.50·W    4.40·W     41%
       6      15.00·W    4.60·W     69%

Nothing about the answers changes. Same model, same tools, same prompt — only
how long the cached prefix survives.

⚠ Those are a PROVIDER's prices, not ours, and gotcha 25's rule is to confirm
a value rather than recall it. So the economics are asserted against LiteLLM's
live model map, not against the numbers in this docstring: if Anthropic
reprices the extended write, the test fails and the decision gets revisited
instead of silently becoming wrong.
"""

from __future__ import annotations

import pytest

from helixis.llm.prompt_cache import _VALID_TTLS, _cache_mark, inject_cache_control

ANTHROPIC_KWARGS = {
    "model": "anthropic/claude-sonnet-5",
    "messages": [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "hi"}],
    "tools": [
        {"type": "function", "function": {"name": "a", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "b", "parameters": {"type": "object"}}},
    ],
}


def _marks(kwargs: dict) -> list[dict]:
    marked = inject_cache_control(kwargs)
    system = marked["messages"][0]["content"]
    return [marked["tools"][-1]["cache_control"], system[-1]["cache_control"]]


# ── the economics, checked against the provider's live rates ────────────────


def test_the_extended_write_still_pays_for_itself_on_the_second_turn() -> None:
    """⚠ The decision this whole change rests on, asserted rather than quoted.

    A 1-hour cache is worth buying only while one extended write costs less
    than the two five-minute writes it replaces. At today's rates that is
    $4.00/M vs 2x$2.50/M — comfortably true. If Anthropic reprices it, this
    fails and says so, instead of leaving a cost "optimisation" that quietly
    became a cost increase.
    """
    import litellm

    rates = litellm.model_cost.get("claude-sonnet-5", {})
    five_min = rates.get("cache_creation_input_token_cost")
    one_hour = rates.get("cache_creation_input_token_cost_above_1hr")
    assert five_min and one_hour, (
        "LiteLLM no longer publishes both cache-write rates for claude-sonnet-5 — "
        "the 1h TTL decision cannot be verified, so re-check it by hand."
    )
    ratio = one_hour / five_min
    assert ratio < 2.0, (
        f"an extended cache write now costs {ratio:.2f}x a five-minute one. Above 2x it "
        f"is cheaper to let the cache expire and rewrite it, and llm.cache_ttl should go "
        f"back to '5m'."
    )


def test_a_cache_read_is_still_far_cheaper_than_a_write() -> None:
    """The other half: the saving comes from turning writes into reads. If a
    read were not dramatically cheaper, holding the cache longer would buy
    nothing."""
    import litellm

    rates = litellm.model_cost.get("claude-sonnet-5", {})
    read, write = rates["cache_read_input_token_cost"], rates["cache_creation_input_token_cost"]
    assert read < write / 5, f"cache read {read} is not meaningfully cheaper than write {write}"


# ── what actually goes on the wire ──────────────────────────────────────────


def test_the_default_marks_a_one_hour_cache() -> None:
    for mark in _marks(ANTHROPIC_KWARGS):
        assert mark == {"type": "ephemeral", "ttl": "1h"}


def test_the_ttl_survives_litellms_anthropic_transformation() -> None:
    """⚠ Reads the SEAM, not our own dict (gotcha 28). ``cache_control`` is
    typed as a pass-through dict, but "we set a key" and "the provider receives
    it" are different claims — and only the second one saves money."""
    import json

    from litellm.llms.anthropic.chat.transformation import AnthropicConfig

    marked = inject_cache_control(dict(ANTHROPIC_KWARGS))
    payload = AnthropicConfig().transform_request(
        model="claude-sonnet-5",
        messages=marked["messages"],
        optional_params={"tools": marked["tools"]},
        litellm_params={},
        headers={},
    )
    blob = json.dumps(payload)
    assert '"ttl": "1h"' in blob or '"ttl":"1h"' in blob, (
        "the TTL did not survive into the Anthropic payload — the cache is still "
        f"five minutes and the saving is imaginary. payload={blob[:400]}"
    )


def test_one_request_carries_exactly_one_ttl() -> None:
    """Anthropic keys a cache entry on the prefix AND its lifetime, so two
    breakpoints with different TTLs are two entries — which would double the
    write this change exists to halve. The mark is resolved once per request
    for that reason."""
    marks = _marks(ANTHROPIC_KWARGS)
    assert len({m.get("ttl", "5m") for m in marks}) == 1, f"mixed TTLs in one request: {marks}"


# ── the kill switch, and its failure posture ────────────────────────────────


def test_five_minutes_sends_the_bare_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "5m" is Anthropic's default, so it goes out as the plain mark — the
    exact payload that was already working before this change. Reverting must
    not introduce a new shape."""
    monkeypatch.setattr("helixis.settings.get_settings", _settings_with("5m"))
    assert _cache_mark() == {"type": "ephemeral"}


def test_an_unrecognised_ttl_falls_back_rather_than_failing_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ Anthropic rejects the WHOLE request over an unknown ``ttl``. A typo in
    an env var must cost the saving, never the turn — the same rule the
    malformed ``input_examples`` incident wrote down (gotcha 27), where one bad
    hint 400'd every call in the suite."""
    monkeypatch.setattr("helixis.settings.get_settings", _settings_with("1 hour"))
    assert _cache_mark() == {"type": "ephemeral"}


def test_unreadable_settings_fall_back_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> object:
        raise RuntimeError("settings exploded")

    monkeypatch.setattr("helixis.settings.get_settings", _boom)
    assert _cache_mark() == {"type": "ephemeral"}


def test_only_lifetimes_anthropic_accepts_are_allowed() -> None:
    assert _VALID_TTLS == {"5m", "1h"}


def test_non_anthropic_models_are_untouched() -> None:
    kwargs = {**ANTHROPIC_KWARGS, "model": "openai/gpt-4o"}
    assert inject_cache_control(kwargs) == kwargs


def _settings_with(ttl: str):
    """A stand-in ``get_settings`` whose ``llm.cache_ttl`` is ``ttl``."""

    class _LLM:
        cache_ttl = ttl

    class _S:
        llm = _LLM()

    return lambda: _S()
