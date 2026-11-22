"""Helixis policy: Gemini-only LLM in prod.

PR #69 added ``assert_no_openai_default`` so the API + worker refuse
to boot when the resolved reasoning model starts with ``openai/``.
These tests pin three things:

1. The class-level default in ``LLMSettings`` is non-OpenAI. A bare
   ``Settings()`` with no YAML / env overrides must never resolve to
   an OpenAI model — that's the safety net for scripts/tests that
   bypass the YAML loader.
2. The guard raises ``RuntimeError`` when the resolved reasoning
   model starts with ``openai/``.
3. The guard passes silently when the resolved reasoning model
   starts with ``gemini/``.
"""

from __future__ import annotations

import pytest

from helixis.settings import LLMSettings, Settings, assert_no_openai_default


def _build_settings(reasoning_model: str | None = None) -> Settings:
    """Construct a Settings instance with a specific reasoning model.

    Bypasses YAML loading so the test only exercises the guard. Empty
    ``models`` block falls back to ``default_model`` per the guard's
    resolver, which is the path the safety-net test relies on.
    """
    s = Settings(
        env="test",
        memory={"enabled": False},
        api={"host": "127.0.0.1", "port": 8000},
        llm={
            "default_model": "gemini/gemini-2.5-flash",
            "models": {"reasoning": {"model": reasoning_model}} if reasoning_model else {},
        },
    )
    return s


def test_llm_settings_default_model_is_not_openai() -> None:
    """The bare class-level default must not be an OpenAI model.
    Closes the audit's silent-OpenAI-fallback finding."""
    assert not LLMSettings().default_model.lower().startswith("openai/")


def test_assert_no_openai_default_raises_when_reasoning_is_openai() -> None:
    settings = _build_settings(reasoning_model="openai/gpt-4o")
    with pytest.raises(RuntimeError, match="forbids OpenAI"):
        assert_no_openai_default(settings)


def test_assert_no_openai_default_passes_when_reasoning_is_gemini() -> None:
    settings = _build_settings(reasoning_model="gemini/gemini-2.5-flash")
    # Must not raise.
    assert_no_openai_default(settings)


def test_assert_no_openai_default_falls_back_to_default_model() -> None:
    """When ``llm.models.reasoning`` is unset, the guard must fall
    back to ``llm.default_model`` — same resolution path the planner
    uses at ``action_planner.py:_reasoning_model_name``."""
    settings = _build_settings(reasoning_model=None)
    # default_model in _build_settings is gemini → must pass.
    assert_no_openai_default(settings)


def test_assert_no_openai_default_raises_when_default_model_is_openai() -> None:
    """Belt-and-braces — even when reasoning isn't pinned, an OpenAI
    default must still trip the guard."""
    settings = Settings(
        env="test",
        memory={"enabled": False},
        api={"host": "127.0.0.1", "port": 8000},
        llm={"default_model": "openai/gpt-4o", "models": {}},
    )
    with pytest.raises(RuntimeError, match="forbids OpenAI"):
        assert_no_openai_default(settings)
