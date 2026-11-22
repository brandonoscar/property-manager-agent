"""Regression tests for the layered-settings env/YAML precedence.

Guards the bug fixed alongside the Browserbase live-path work: an empty
placeholder in ``config/default.yaml`` (e.g. ``browserbase_api_key: ""``) was
injected as a constructor kwarg and, because init kwargs outrank env vars in
pydantic-settings, silently shadowed the matching ``HELIXIS_*`` env var. The
fix (``_prune_empty``) drops empty leaves before injection so env vars win,
while real (non-empty) YAML values — including ``False`` / ``0`` — survive.
"""

from __future__ import annotations

import pytest

from helixis.settings import _DROP, Settings, _drop_env_shadowed, _prune_empty


def test_env_overrides_empty_yaml_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    """An env var must populate a key whose only YAML value is ``""``."""
    monkeypatch.setenv("HELIXIS_BROWSER__BROWSERBASE_API_KEY", "dummyXYZ123")
    monkeypatch.setenv("HELIXIS_BROWSER__BROWSERBASE_PROJECT_ID", "proj-dummy-456")
    monkeypatch.setenv("HELIXIS_WEB__TAVILY_API_KEY", "tvly-dummy")

    settings = Settings()

    assert settings.browser.browserbase_api_key == "dummyXYZ123"
    assert settings.browser.browserbase_project_id == "proj-dummy-456"
    assert settings.web.tavily_api_key == "tvly-dummy"


def test_env_overrides_langfuse_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """HELIXIS_LANGFUSE__HOST must override the default. Regression: a
    hardcoded ``host`` in default.yaml was injected as a kwarg that
    outranked the env var, pinning prod tracing to ``localhost:3000`` no
    matter what was set in Render."""
    monkeypatch.setenv("HELIXIS_LANGFUSE__HOST", "https://us.cloud.langfuse.com")

    settings = Settings()

    assert settings.langfuse.host == "https://us.cloud.langfuse.com"


def test_langfuse_host_defaults_to_localhost_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env var, dev still falls back to the localhost default."""
    monkeypatch.delenv("HELIXIS_LANGFUSE__HOST", raising=False)

    settings = Settings()

    assert settings.langfuse.host == "http://localhost:3000"


def test_blank_env_keeps_graceful_empty_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env var set the creds stay empty so the browser/web tools
    degrade to '*_unavailable' rather than crashing."""
    monkeypatch.delenv("HELIXIS_BROWSER__BROWSERBASE_API_KEY", raising=False)
    monkeypatch.delenv("HELIXIS_WEB__TAVILY_API_KEY", raising=False)

    settings = Settings()

    assert settings.browser.browserbase_api_key == ""
    assert settings.web.tavily_api_key == ""


def test_non_empty_yaml_values_survive_prune() -> None:
    """The prune must not disturb real config — including falsy-but-meaningful
    values like ``False`` and numeric ``0``/``15``."""
    settings = Settings()

    assert settings.llm.default_model == "anthropic/claude-sonnet-5"
    assert settings.browser.max_steps_per_task == 15
    assert settings.browser.headless is False  # falsy, must NOT be pruned
    assert settings.sentry.send_default_pii is False
    assert settings.dev.show_runs_page is True
    assert "http://localhost:5173" in settings.api.cors_origins


def test_production_yaml_loads_when_env_is_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: ``config/production.yaml`` was never loaded in production.

    The env-specific YAML file was selected from ``kwargs.get('env')``, but
    ``Settings()`` is constructed with no kwargs in the deployed process — env
    comes from ``HELIXIS_ENV`` — so it always fell back to 'development' →
    ``development.yaml`` (absent) → production.yaml silently skipped. The CORS
    regression (onboarding origin rejected despite being in production.yaml +
    render.yaml) was the visible symptom.
    """
    monkeypatch.setenv("HELIXIS_ENV", "production")
    # Isolate the YAML-loading behaviour from any ambient render-style env CORS
    # vars in the test runner.
    monkeypatch.delenv("HELIXIS_API__CORS_ORIGINS", raising=False)
    monkeypatch.delenv("HELIXIS_API__CORS_ORIGIN_REGEX", raising=False)

    settings = Settings()

    assert settings.env == "production"
    # Values production.yaml flips AWAY from the default.yaml defaults — proof
    # the env file actually merged in.
    assert settings.browser.headless is True  # default.yaml: False
    assert settings.dev.show_runs_page is False  # default.yaml: True
    # The CORS regression specifically: the onboarding origin + cross-project
    # preview regex live only in production.yaml.
    assert "https://your-onboarding.vercel.app" in settings.api.cors_origins
    assert settings.api.cors_origin_regex  # non-empty only in production.yaml


def test_missing_env_file_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An env name with no matching YAML (e.g. 'test', the CI value) cleanly
    uses default.yaml only — no crash, defaults intact."""
    monkeypatch.setenv("HELIXIS_ENV", "test")  # config/test.yaml does not exist

    settings = Settings()

    assert settings.browser.headless is False  # default.yaml value survives
    assert settings.dev.show_runs_page is True
    assert settings.api.cors_origin_regex == ""  # production-only, so unset here


def test_env_overrides_non_empty_yaml_leaf(monkeypatch: pytest.MonkeyPatch) -> None:
    """The headline fix: an env var must beat a NON-empty default.yaml leaf.

    Before the fix this failed — default.yaml's ``api.port: 8000`` was injected
    as a kwarg that outranked ``HELIXIS_API__PORT``. The deep-merge still leaves
    sibling leaves (``host``) coming from YAML, proving it's per-leaf, not a
    wholesale replace of the ``api`` block.
    """
    monkeypatch.setenv("HELIXIS_API__PORT", "9999")

    settings = Settings()

    assert settings.api.port == 9999  # env wins over default.yaml's 8000
    assert settings.api.host == "0.0.0.0"  # sibling YAML leaf still applies


def test_drop_env_shadowed_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct coverage: a leaf is dropped iff its HELIXIS_<PATH> var is set."""
    env_keys = {"HELIXIS_API__PORT", "HELIXIS_DATA_KEY"}
    tree = {
        "api": {"host": "0.0.0.0", "port": 8000},
        "data_key": "yaml-key",
        "llm": {"default_model": "gemini/gemini-3.5-flash"},
    }
    result = _drop_env_shadowed(tree, env_keys)
    # Shadowed leaves dropped (env will fill them); the rest survive.
    assert result == {
        "api": {"host": "0.0.0.0"},
        "llm": {"default_model": "gemini/gemini-3.5-flash"},
    }
    # A subtree whose every leaf is shadowed collapses to _DROP.
    assert _drop_env_shadowed({"port": 1}, {"HELIXIS_PORT"}) is _DROP


def test_prune_empty_unit() -> None:
    """Direct coverage of the leaf-pruning rule."""
    assert _prune_empty("") is _DROP
    assert _prune_empty(None) is _DROP
    assert _prune_empty({"a": "", "b": None}) is _DROP  # all-empty dict collapses
    # Falsy-but-meaningful values survive.
    assert _prune_empty(False) is False
    assert _prune_empty(0) == 0
    assert _prune_empty([]) == []
    # Mixed dict keeps only the non-empty leaves.
    assert _prune_empty({"keep": "x", "drop": "", "nested": {"k": None}}) == {"keep": "x"}
