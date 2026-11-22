"""The LLM ledger's cache split, and the budget weighting that rides on it.

0050 recorded one number: ``total_tokens``. That made prompt caching — the
largest cost lever we actually ship — invisible, and it made the daily budget
measure the wrong quantity: a cached read counts as a full token in the ledger
and bills at 10% in reality, so the company whose cache works BEST trips its
429 first.

The arithmetic below rests on one fact: ``cached`` and ``cache_write`` are
SUBSETS of ``prompt``. Anthropic's own ``input_tokens`` excludes them and
LiteLLM adds them back in. Getting that backwards would double-count the
cache and make budgets stricter rather than looser — so it is pinned against
the transform itself, not just asserted here.
"""

from __future__ import annotations

from types import SimpleNamespace

from helixis.llm.spend import Usage, _usage_breakdown


def _response(**details: int) -> SimpleNamespace:
    """A LiteLLM-shaped response object with prompt_tokens_details."""
    prompt = details.pop("prompt", 0)
    return SimpleNamespace(
        usage=SimpleNamespace(
            total_tokens=prompt + details.pop("completion", 0),
            prompt_tokens=prompt,
            completion_tokens=details.pop("completion_tokens", 0),
            prompt_tokens_details=SimpleNamespace(**details),
        )
    )


def test_a_warm_cache_costs_a_tenth_of_what_the_raw_total_says() -> None:
    """The whole point. Same raw total, wildly different billable weight."""
    warm = Usage(total=14_000, prompt=13_000, completion=1_000, cached=12_000)
    cold = Usage(total=14_000, prompt=13_000, completion=1_000)

    assert warm.effective == 3_200  # 1000 uncached + 1200 (12k @ 0.1) + 1000 out
    assert cold.effective == 14_000
    assert warm.effective < cold.effective / 4


def test_a_cache_write_costs_more_than_an_uncached_token() -> None:
    """The 25% write premium is real and must not be counted as a discount."""
    write = Usage(total=13_000, prompt=13_000, cache_write=12_000)
    plain = Usage(total=13_000, prompt=13_000)
    assert write.effective > plain.effective


def test_components_are_subtracted_from_prompt_not_added_to_it() -> None:
    """If the subset relationship were read backwards, this would exceed the
    raw total instead of falling below it."""
    usage = Usage(total=10_000, prompt=10_000, cached=9_000)
    assert usage.effective < usage.total


def test_effective_never_goes_negative() -> None:
    """A provider reporting components larger than the prompt total must not
    produce a negative that silently hands out free budget."""
    broken = Usage(total=100, prompt=100, cached=5_000, cache_write=5_000)
    assert broken.effective >= 0


def test_extraction_reads_the_details_wrapper() -> None:
    resp = _response(prompt=13_000, completion_tokens=900, cached_tokens=12_000)
    usage = _usage_breakdown(resp)
    assert usage.prompt == 13_000
    assert usage.cached == 12_000
    assert usage.completion == 900


def test_extraction_accepts_either_cache_write_field_name() -> None:
    """LiteLLM has carried this under two names across versions. Reading only
    one would silently record zero forever — the exact silent-degradation
    class as gotcha 17."""
    new = _usage_breakdown(_response(prompt=100, cache_creation_tokens=40))
    old = _usage_breakdown(_response(prompt=100, cache_write_tokens=40))
    assert new.cache_write == old.cache_write == 40


def test_a_provider_with_no_cache_details_still_records_totals() -> None:
    """Non-Anthropic responses have no details wrapper. They must degrade to
    plain accounting, not to zero."""
    plain = SimpleNamespace(
        usage=SimpleNamespace(total_tokens=500, prompt_tokens=400, completion_tokens=100)
    )
    usage = _usage_breakdown(plain)
    assert usage.total == 500
    assert usage.cached == 0
    assert usage.effective == 500


def test_dict_shaped_usage_works_too() -> None:
    usage = _usage_breakdown(
        {
            "usage": {
                "total_tokens": 500,
                "prompt_tokens": 400,
                "completion_tokens": 100,
                "prompt_tokens_details": {"cached_tokens": 300},
            }
        }
    )
    assert usage.cached == 300


def test_junk_usage_never_raises() -> None:
    """Accounting must never be able to fail an LLM call."""
    for junk in (None, {}, SimpleNamespace(), SimpleNamespace(usage=None), "nonsense"):
        assert _usage_breakdown(junk).total == 0


def test_litellm_still_folds_cache_tokens_into_prompt_tokens() -> None:
    """The pin. Every weight above assumes cached/cache_write are SUBSETS of
    prompt_tokens, which is true because LiteLLM adds Anthropic's separate
    cache counts into it. If a version bump ever stops doing that, the
    weighting silently over-discounts and this is where it surfaces."""
    import inspect

    from litellm.llms.anthropic.chat.transformation import AnthropicConfig

    src = inspect.getsource(AnthropicConfig)
    assert "prompt_tokens += cache_creation_input_tokens" in src
    assert "prompt_tokens += cache_read_input_tokens" in src
