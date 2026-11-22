"""Tests for the LiteLLM model router.

The contract that matters: every LitellmModel constructed by Helixis
must carry the right ``api_key`` for its provider, sourced from the
matching Helixis-prefixed setting (HELIXIS_LLM__GOOGLE_API_KEY etc.).
This avoids forcing operators to mirror the same secret under LiteLLM's
own env var convention (GEMINI_API_KEY etc.).
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from helixis.llm.router import _api_key_for_model, get_model
from helixis.settings import Settings


def _settings(
    *,
    google: str = "",
    openai: str = "",
    anthropic: str = "",
    default_model: str = "openai/gpt-4o",
    models: dict | None = None,
) -> Settings:
    return Settings(
        env="test",
        memory={"enabled": False},
        llm={
            "default_model": default_model,
            "models": models or {},
            "google_api_key": google,
            "openai_api_key": openai,
            "anthropic_api_key": anthropic,
        },
    )


# ── _api_key_for_model — unit-level prefix matching ──────────────────


@pytest.mark.parametrize(
    "model_name",
    [
        "gemini/gemini-2.5-flash",
        "gemini/gemini-2.5-pro",
        "google/some-model",
        "vertex_ai/gemini-1.5-pro",
        # Case-insensitive matching.
        "GEMINI/gemini-2.5-flash",
    ],
)
def test_api_key_for_gemini_models_picks_google_key(model_name: str) -> None:
    s = _settings(google="goog123")
    assert _api_key_for_model(s, model_name) == "goog123"


def test_api_key_for_openai_picks_openai_key() -> None:
    s = _settings(openai="sk-openai")
    assert _api_key_for_model(s, "openai/gpt-4o") == "sk-openai"


@pytest.mark.parametrize(
    "model_name",
    ["anthropic/claude-sonnet-4", "claude/claude-3-5-sonnet"],
)
def test_api_key_for_anthropic_picks_anthropic_key(model_name: str) -> None:
    s = _settings(anthropic="ant-key")
    assert _api_key_for_model(s, model_name) == "ant-key"


def test_unknown_prefix_returns_none() -> None:
    s = _settings(openai="x", google="y", anthropic="z")
    assert _api_key_for_model(s, "foo/bar") is None


def test_empty_string_treated_as_none() -> None:
    """An explicitly-empty key shouldn't smuggle '' into LitellmModel —
    that would override LiteLLM's own env var fallback with an empty
    string and surface as a confusing auth error."""
    s = _settings(google="")
    assert _api_key_for_model(s, "gemini/gemini-2.5-flash") is None


# ── get_model — wiring through the LitellmModel constructor ──────────


def test_get_model_passes_google_api_key_for_gemini_default() -> None:
    s = _settings(google="goog123", default_model="gemini/gemini-2.5-flash")
    with patch("helixis.llm.router.LitellmModel") as litellm_mock:
        get_model(s, "reasoning")
    litellm_mock.assert_called_once_with(model="gemini/gemini-2.5-flash", api_key="goog123")


def test_get_model_passes_openai_api_key_for_openai_alias() -> None:
    s = _settings(
        openai="sk-openai",
        default_model="gemini/gemini-2.5-flash",
        models={"reasoning": {"model": "openai/gpt-4o"}},
    )
    with patch("helixis.llm.router.LitellmModel") as litellm_mock:
        get_model(s, "reasoning")
    litellm_mock.assert_called_once_with(model="openai/gpt-4o", api_key="sk-openai")


def test_get_model_omits_api_key_and_warns_for_unknown_prefix(
    caplog: pytest.LogCaptureFixture,
) -> None:
    s = _settings(default_model="foo/bar")
    caplog.set_level(logging.WARNING, logger="helixis.llm.router")
    with patch("helixis.llm.router.LitellmModel") as litellm_mock:
        get_model(s, "reasoning")
    # api_key kwarg must NOT be passed — letting LiteLLM use its own
    # fallback is at least diagnosable, smuggling None is not.
    litellm_mock.assert_called_once_with(model="foo/bar")
    assert any("No Helixis-configured API key matched" in r.message for r in caplog.records)


def test_get_model_falls_back_to_default_when_task_type_missing() -> None:
    s = _settings(google="goog", default_model="gemini/gemini-2.5-flash")
    with patch("helixis.llm.router.LitellmModel") as litellm_mock:
        get_model(s, "nonexistent-task-type")
    litellm_mock.assert_called_once_with(model="gemini/gemini-2.5-flash", api_key="goog")


# ── dev local-model override (Ollama enabler) ────────────────────────


def _dev_local_settings(local_model: str, env: str) -> Settings:
    return Settings(
        env=env,
        memory={"enabled": False},
        llm={"default_model": "anthropic/claude-sonnet-5", "anthropic_api_key": "sk-ant"},
        dev={"local_model": local_model},
    )


def test_dev_local_model_overrides_in_non_production() -> None:
    # Set + non-prod → the reasoning model is the local one, no api_key
    # (ollama needs none), and the Anthropic default is bypassed.
    s = _dev_local_settings("ollama/llama3.3", env="development")
    with patch("helixis.llm.router.LitellmModel") as litellm_mock:
        get_model(s, "reasoning")
    litellm_mock.assert_called_once_with(model="ollama/llama3.3")


def test_dev_local_model_is_ignored_in_production(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The hard gate: even if the var is set, a production env IGNORES it and
    # uses the Anthropic model — the override can never accidentally ship.
    s = _dev_local_settings("ollama/llama3.3", env="production")
    caplog.set_level(logging.WARNING, logger="helixis.llm.router")
    with patch("helixis.llm.router.LitellmModel") as litellm_mock:
        get_model(s, "reasoning")
    litellm_mock.assert_called_once_with(model="anthropic/claude-sonnet-5", api_key="sk-ant")
    assert any("IGNORING it" in r.message for r in caplog.records)


def test_dev_local_model_empty_is_a_noop() -> None:
    # The default everywhere: empty override → normal Anthropic resolution.
    s = _dev_local_settings("", env="development")
    with patch("helixis.llm.router.LitellmModel") as litellm_mock:
        get_model(s, "reasoning")
    litellm_mock.assert_called_once_with(model="anthropic/claude-sonnet-5", api_key="sk-ant")


def test_ollama_prefix_resolves_no_api_key() -> None:
    assert _api_key_for_model(_settings(), "ollama/llama3.3") is None


# ── PR #71 — configure_litellm_retries (num_retries + fallback policy) ─────
# Anthropic-only since the 2026-07-21 Gemini exit.


def _anthropic_settings_with_siblings() -> Settings:
    """Mirror the prod ``config/default.yaml`` shape: reasoning +
    browser + analysis all Claude SKUs."""
    return _settings(
        anthropic="ant-key",
        default_model="anthropic/claude-sonnet-5",
        models={
            "reasoning": {"model": "anthropic/claude-sonnet-5"},
            "browser": {"model": "anthropic/claude-haiku-4-5"},
            "analysis": {"model": "anthropic/claude-sonnet-4-6"},
        },
    )


def test_configure_litellm_retries_sets_num_retries_from_settings() -> None:
    from helixis.llm.router import configure_litellm_retries

    result = configure_litellm_retries(_anthropic_settings_with_siblings())
    assert result["num_retries"] == 3  # default max_retries
    import litellm

    assert litellm.num_retries == 3


def test_configure_litellm_wires_request_timeout_and_max_retries() -> None:
    """The configured timeout + retries must actually reach the litellm globals
    — before this they were defined in settings but never applied, leaving
    litellm on its ~6000s default timeout."""
    import litellm

    from helixis.llm.router import configure_litellm_retries

    s = _settings(
        anthropic="ant-key",
        default_model="anthropic/claude-sonnet-5",
        models={"reasoning": {"model": "anthropic/claude-sonnet-5"}},
    )
    s.llm.max_retries = 5
    s.llm.request_timeout = 180

    result = configure_litellm_retries(s)
    assert result["num_retries"] == 5
    assert result["request_timeout"] == 180
    assert litellm.num_retries == 5
    assert litellm.request_timeout == 180


def test_configure_litellm_retries_installs_anthropic_only_fallbacks() -> None:
    from helixis.llm.router import configure_litellm_retries

    result = configure_litellm_retries(_anthropic_settings_with_siblings())
    # Exactly one primary→fallbacks mapping, and every fallback is Claude.
    assert len(result["fallbacks"]) == 1
    entry = result["fallbacks"][0]
    assert list(entry.keys()) == ["anthropic/claude-sonnet-5"]
    fallbacks = entry["anthropic/claude-sonnet-5"]
    assert fallbacks  # non-empty
    for name in fallbacks:
        assert name.lower().startswith(("anthropic/", "claude/")), (
            f"non-Anthropic model leaked into fallback chain: {name!r}"
        )


def test_configure_litellm_retries_installs_the_policy_the_call_path_reads() -> None:
    """⚠ The assertions above pin ``litellm.fallbacks`` / ``litellm.num_retries``,
    and BOTH are inert on this product's call path — they were green for months
    while the Haiku failover had never once fired (see ``llm/call_policy``).
    A test that only reads the globals cannot tell a working failover from a
    decorative one, so this reads the side that ``acompletion`` actually
    consults."""
    from helixis.llm.call_policy import current_call_policy, set_call_policy
    from helixis.llm.router import configure_litellm_retries

    set_call_policy(None)
    try:
        configure_litellm_retries(_anthropic_settings_with_siblings())
        policy = current_call_policy()
        assert policy is not None, "boot configured the globals and left the call path bare"
        assert policy.fallbacks["anthropic/claude-sonnet-5"], (
            "the primary resolves to no sibling — failover is wired but empty"
        )
        assert policy.num_retries > 0
    finally:
        set_call_policy(None)


def test_configure_litellm_retries_leaves_no_policy_when_no_sibling_exists() -> None:
    """A primary with no declared sibling must not acquire a fallback chain
    pointing at itself — retrying the model that just failed is a no-op that
    doubles the latency of every outage."""
    from helixis.llm.call_policy import current_call_policy, set_call_policy
    from helixis.llm.router import configure_litellm_retries

    set_call_policy(None)
    try:
        s = _settings(
            anthropic="ant-key",
            default_model="anthropic/claude-sonnet-5",
            models={"reasoning": {"model": "anthropic/claude-sonnet-5"}},
        )
        configure_litellm_retries(s)
        policy = current_call_policy()
        assert policy is not None
        assert policy.fallbacks == {}
        # The retry budget still applies — a 503 retries at the same model.
        assert policy.num_retries == 3
    finally:
        set_call_policy(None)


def test_configure_litellm_retries_refuses_openai_fallback() -> None:
    """Defensive: a future edit that adds an OpenAI sibling under the
    ``models`` block must trip the policy guard rather than silently
    install a non-Anthropic fallback."""
    from helixis.llm.router import configure_litellm_retries

    bad = _settings(
        anthropic="ant-key",
        openai="openai-key",
        default_model="anthropic/claude-sonnet-5",
        models={
            "reasoning": {"model": "anthropic/claude-sonnet-5"},
            "browser": {"model": "openai/gpt-4o-mini"},
        },
    )
    with pytest.raises(ValueError, match="non-Anthropic fallback"):
        configure_litellm_retries(bad)


def test_configure_litellm_retries_refuses_gemini_fallback() -> None:
    """The Gemini exit made Gemini itself a forbidden fallback provider —
    the account is gone, so a Gemini sibling would 403 at failover time."""
    from helixis.llm.router import configure_litellm_retries

    bad = _settings(
        anthropic="ant-key",
        google="goog-key",
        default_model="anthropic/claude-sonnet-5",
        models={
            "reasoning": {"model": "anthropic/claude-sonnet-5"},
            "browser": {"model": "gemini/gemini-3.5-flash"},
        },
    )
    with pytest.raises(ValueError, match="non-Anthropic fallback"):
        configure_litellm_retries(bad)


def test_configure_litellm_retries_handles_empty_fallback_list() -> None:
    """Reasoning declared alone with no siblings — the primary still
    gets retries (so a 503 retries 3x at the same model), but the
    fallback list is empty rather than referencing the primary as its
    own fallback (which would be a no-op infinite loop)."""
    import litellm

    from helixis.llm.router import configure_litellm_retries

    s = _settings(
        google="goog-key",
        default_model="gemini/gemini-2.5-flash",
        models={"reasoning": {"model": "gemini/gemini-2.5-flash"}},
    )
    result = configure_litellm_retries(s)
    assert result["fallbacks"] == []
    assert litellm.fallbacks == []
    assert result["num_retries"] == 3


def test_configure_litellm_retries_ignores_sibling_equal_to_primary() -> None:
    """If a sibling slot accidentally points at the same model as
    reasoning, we drop it from the fallback list — falling back to
    yourself accomplishes nothing."""
    from helixis.llm.router import configure_litellm_retries

    s = _settings(
        anthropic="ant-key",
        default_model="anthropic/claude-sonnet-5",
        models={
            "reasoning": {"model": "anthropic/claude-sonnet-5"},
            "browser": {"model": "anthropic/claude-sonnet-5"},  # same as primary
            "analysis": {"model": "anthropic/claude-haiku-4-5"},
        },
    )
    result = configure_litellm_retries(s)
    assert result["fallbacks"][0]["anthropic/claude-sonnet-5"] == ["anthropic/claude-haiku-4-5"]
