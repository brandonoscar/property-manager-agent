"""mem0's llm config is forwarded verbatim into ``messages.create(**params)``.

``Messages.create`` declares its parameters explicitly and takes **no**
``**kwargs``, so any key mem0 passes through that the installed Anthropic SDK
does not declare becomes a hard ``TypeError`` — not a warning, not a silently
ignored option.

That is what happened on 2026-08-23 (Sentry PYTHON-FASTAPI-B / -3B / -3C):
``"temperature": 0.0`` had been in this config since #22 and every post-turn
memory extraction started failing with

    Messages.create() got an unexpected keyword argument 'temperature'

Nothing had touched ``memory/manager.py`` since #718. ``anthropic>=0.40`` is
unpinned, so a rebuild resolved an SDK that no longer accepts the key —
dependency drift, surfacing as an application error, attributed by Sentry to
whichever deploy happened to be live.

The general rule this pins: **a config dict forwarded into a third-party
signature is an API contract with that signature.** Only keys the SDK is
known to declare may appear here.

⚠ **That rule is necessary and NOT sufficient, which cost a second incident.**
#878 removed the key and this file went green — and the daily smoke failed
with the identical error eleven hours later, because mem0 supplies its own
``temperature`` default (``BaseLlmConfig.temperature = 0.1``) regardless of
what we write. Controlling our dict does not control the call. The tests at
the bottom assert on the params mem0 WOULD SEND, which is the side that
decides; the root cause is issue #535 (``anthropic`` unpinned at ``>=0.40``,
and **1.0.0 removed temperature/top_p/top_k**).
"""

from __future__ import annotations

import inspect

import pytest

from helixis.memory import manager


def _llm_config() -> dict:
    """The llm block as it is literally written, read from source.

    Deliberately source-level: ``_build_config`` needs settings and live keys,
    and the property under test is what the dict LITERAL contains — which is
    what mem0 forwards.
    """
    src = inspect.getsource(manager)
    start = src.index('"llm": {')
    # Anchored to the end of the config dict, NOT a character count — a fixed
    # window silently shrank past the keys under test the moment the comment
    # above them grew, and a zero count reads as "key absent" rather than
    # "you measured the wrong region".
    end = src.index("db_url = settings", start)
    return {"src": src[start:end]}


def test_temperature_is_not_passed_to_mem0() -> None:
    """THE regression. Its presence broke every extraction in production."""
    block = _llm_config()["src"]
    assert '"temperature"' not in block, (
        "temperature is back in the mem0 llm config — this raised "
        "TypeError: Messages.create() got an unexpected keyword argument "
        "'temperature' on every post-turn extraction (PYTHON-FASTAPI-B)"
    )


def test_max_tokens_survives_and_is_not_duplicated() -> None:
    """The 4000 cap fixed truncated-JSON parse errors (#542) and must not be
    lost while removing a neighbouring key — nor duplicated, which is what a
    careless edit does first."""
    block = _llm_config()["src"]
    assert block.count('"max_tokens"') == 1
    assert '"max_tokens": 4000' in block


@pytest.mark.parametrize("key", ["model", "api_key", "max_tokens"])
def test_every_forwarded_key_is_declared_by_the_sdk(key: str) -> None:
    """Read BOTH sides (gotcha 28): each key we forward must exist in the
    installed ``Messages.create`` signature. If a future SDK drops one, this
    fails HERE with the key named, rather than in production as an opaque
    TypeError on a best-effort path nobody is watching.

    ``model`` and ``max_tokens`` go straight through; ``api_key`` is consumed
    by mem0 to build the client and never reaches ``create``.
    """
    if key == "api_key":
        pytest.skip("consumed by mem0 when constructing the client")
    try:
        import anthropic
    except ImportError:  # pragma: no cover - the [memory] extra is optional
        pytest.skip("anthropic not installed")

    params = inspect.signature(anthropic.resources.messages.Messages.create).parameters
    accepts_kwargs = any(p.kind is p.VAR_KEYWORD for p in params.values())
    assert key in params or accepts_kwargs, (
        f"the installed anthropic SDK does not accept {key!r} — mem0 forwards "
        "it into messages.create() and it will TypeError at runtime"
    )


# ── what mem0 ACTUALLY sends (added 2026-08-24) ─────────────────────────────
#
# ⚠ Everything above inspects OUR dict. That is only half the seam, and the
# missing half is why production stayed broken: #878 removed "temperature"
# from the config at 00:53 UTC, `test_temperature_is_not_passed_to_mem0`
# went green, and the daily smoke failed with the IDENTICAL error at 11:32
# UTC — eleven hours later.
#
# Omitting the key was inert. mem0 supplies its own: BaseLlmConfig.temperature
# defaults to 0.1, and AnthropicLLM._get_common_params injects it whenever
# _enable_sampling_parameters() is true — which for a model whose family parses
# as "haiku" is unconditionally true. Our config never had a vote.
#
# So these tests assert on the params mem0 WOULD PASS to messages.create,
# built from our real config, rather than on what we wrote. Same rule the file
# already invokes (gotcha 28, read BOTH sides) — applied to the side that
# actually decides.


def _params_mem0_would_send(**overrides):
    """The kwargs mem0's Anthropic provider builds from a given config."""
    from mem0.configs.llms.anthropic import AnthropicConfig
    from mem0.llms.anthropic import AnthropicLLM

    cfg = AnthropicConfig(
        model=manager.MEM0_LLM_MODEL, api_key="unused", max_tokens=4000, **overrides
    )
    # Bypass __init__: it builds a real anthropic client. We want the pure
    # parameter-assembly logic, which is what reaches the SDK.
    llm = AnthropicLLM.__new__(AnthropicLLM)
    llm.config = cfg
    return AnthropicLLM._get_common_params(llm)


def _requires_mem0():
    pytest.importorskip("mem0", reason="the [memory] extra is optional")


def test_mem0_does_not_send_temperature_with_our_config() -> None:
    """THE real regression — the one #878's version could not see."""
    _requires_mem0()
    sent = _params_mem0_would_send(enable_sampling_parameters=False)
    assert "temperature" not in sent, (
        "mem0 would still forward temperature into messages.create — anthropic "
        "1.0.0 removed that parameter (along with top_p/top_k) and takes no "
        "**kwargs, so this is a hard TypeError on every post-turn extraction"
    )
    assert "top_p" not in sent, "top_p was removed in anthropic 1.0.0 too"


def test_the_config_carries_the_switch_that_makes_that_true() -> None:
    """Names the mechanism, so deleting the flag fails with the reason
    attached rather than as a mysterious downstream break."""
    block = _llm_config()["src"]
    assert '"enable_sampling_parameters": False' in block, (
        "the mem0 llm config must set enable_sampling_parameters=False — "
        "omitting 'temperature' does NOT stop mem0 sending its own default "
        "(BaseLlmConfig.temperature = 0.1); that was the #878 fix and it "
        "did not work"
    )


def test_guard_self_test_omitting_the_key_is_not_enough() -> None:
    """Feeds the checker the PRE-FIX configuration and requires a positive.

    Without this, the pair above would pass just as happily against a build
    where mem0 had quietly stopped sending sampling params for its own
    reasons — proving nothing about our config. The two inputs differ ONLY in
    the flag, so the flag is demonstrably what does the work.
    """
    _requires_mem0()
    pre_fix = _params_mem0_would_send()  # #878's state: just omit temperature
    post_fix = _params_mem0_would_send(enable_sampling_parameters=False)
    assert "temperature" in pre_fix, (
        "could not reproduce the production failure — this guard is not "
        "measuring the thing it claims to measure"
    )
    assert "temperature" not in post_fix
    assert pre_fix != post_fix
