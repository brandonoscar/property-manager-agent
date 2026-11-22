"""HELIXIS_EVAL_MODEL override (Gemini-cost lever) — flagged OFF by default."""

from __future__ import annotations

import pytest

from helixis.evals.model_override import (
    apply_eval_model_override,
    eval_default_model,
    eval_models,
)


def test_unset_returns_base_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HELIXIS_EVAL_MODEL", raising=False)
    assert eval_default_model("gemini/gemini-2.5-pro") == "gemini/gemini-2.5-pro"


def test_blank_and_whitespace_are_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # CI passes '' when the HELIXIS_EVAL_PR_MODEL repo variable is unset —
    # that must mean "no change", never an empty model id handed to LiteLLM.
    for blank in ("", "   "):
        monkeypatch.setenv("HELIXIS_EVAL_MODEL", blank)
        assert eval_default_model("gemini/gemini-2.5-pro") == "gemini/gemini-2.5-pro"


def test_set_overrides_base_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HELIXIS_EVAL_MODEL", "gemini/gemini-2.5-flash")
    assert eval_default_model("gemini/gemini-2.5-pro") == "gemini/gemini-2.5-flash"


def test_eval_models_rewrites_every_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    # The router resolves real calls via the "reasoning" slot of llm.models,
    # NOT default_model — the first Haiku-tier CI run proved an override that
    # touches only default_model silently never applies (440/440 calls stayed
    # on Gemini). So the override must rewrite the map's slots.
    base = {
        "reasoning": {"model": "gemini/gemini-3.1-pro-preview", "description": "d"},
        "browser": {"model": "gemini/gemini-3.5-flash", "description": "d"},
    }
    monkeypatch.setenv("HELIXIS_EVAL_MODEL", "anthropic/claude-haiku-4-5")
    out = eval_models(base)
    assert out["reasoning"]["model"] == "anthropic/claude-haiku-4-5"
    assert out["browser"]["model"] == "anthropic/claude-haiku-4-5"
    assert out["reasoning"]["description"] == "d"  # non-model keys untouched
    assert base["reasoning"]["model"] == "gemini/gemini-3.1-pro-preview"  # no mutation

    monkeypatch.delenv("HELIXIS_EVAL_MODEL")
    assert eval_models(base)["reasoning"]["model"] == "gemini/gemini-3.1-pro-preview"
    assert eval_models(None) == {}


def test_apply_override_swaps_model_and_stale_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Direct-LiteLLM eval callers (planner/summarizer/document tools) pass an
    # api_key resolved for their ORIGINAL model — sending that key to a
    # different provider would 401, so a cross-provider override must drop it
    # and re-resolve: from HELIXIS_LLM__ANTHROPIC_API_KEY when set, else fall
    # through to LiteLLM's native env lookup (ANTHROPIC_API_KEY).
    monkeypatch.setenv("HELIXIS_EVAL_MODEL", "anthropic/claude-haiku-4-5")
    monkeypatch.delenv("HELIXIS_LLM__ANTHROPIC_API_KEY", raising=False)
    kwargs = {"model": "gemini/gemini-3.1-pro-preview", "api_key": "gk", "messages": []}
    apply_eval_model_override(kwargs)
    assert kwargs["model"] == "anthropic/claude-haiku-4-5"
    assert "api_key" not in kwargs  # stale key dropped; LiteLLM env lookup takes over

    monkeypatch.setenv("HELIXIS_LLM__ANTHROPIC_API_KEY", "ak")
    kwargs = {"model": "gemini/gemini-3.1-pro-preview", "api_key": "gk", "messages": []}
    apply_eval_model_override(kwargs)
    assert kwargs["api_key"] == "ak"  # re-resolved for the override's provider

    # A Gemini-tier override re-resolves the Google key from env instead.
    monkeypatch.setenv("HELIXIS_EVAL_MODEL", "gemini/gemini-3.5-flash")
    monkeypatch.setenv("HELIXIS_LLM__GOOGLE_API_KEY", "gk2")
    kwargs = {"model": "gemini/gemini-3.1-pro-preview", "api_key": "gk", "messages": []}
    apply_eval_model_override(kwargs)
    assert kwargs["model"] == "gemini/gemini-3.5-flash"
    assert kwargs["api_key"] == "gk2"

    # Unset override → untouched (including the caller's api_key).
    monkeypatch.delenv("HELIXIS_EVAL_MODEL")
    kwargs = {"model": "gemini/gemini-3.1-pro-preview", "api_key": "gk"}
    apply_eval_model_override(kwargs)
    assert kwargs == {"model": "gemini/gemini-3.1-pro-preview", "api_key": "gk"}


def test_eval_settings_builders_route_through_override() -> None:
    # Source-level pin: both eval Settings builders must pass default_model
    # AND the models map through the override helpers, and the runner's
    # completion chokepoint must apply it — or the CI knob silently stops
    # working for one of the three eval call paths.
    import inspect

    from helixis.evals import agent_flow, orchestrator_routing, runner

    for mod in (agent_flow, orchestrator_routing):
        src = inspect.getsource(mod)
        assert "eval_default_model(base.llm.default_model)" in src, mod.__name__
        assert "eval_models(base.llm.models)" in src, mod.__name__
    assert "apply_eval_model_override(kwargs)" in inspect.getsource(runner._eval_completion_fn)
