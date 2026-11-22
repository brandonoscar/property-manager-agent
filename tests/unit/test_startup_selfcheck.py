"""The lifespan must emit a single ``helixis startup complete`` line with
a structured snapshot of which integrations are wired up. Operators grep
this on Render to triage why a feature is silent — much faster than
chasing per-subsystem warning lines.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import helixis.main as main_mod
from helixis.main import create_app
from helixis.settings import Settings


def _fake_memory(*, available: bool) -> MagicMock:
    mem = MagicMock()
    mem.available = available
    mem.initialize = AsyncMock()
    mem.search = AsyncMock(return_value=[])
    return mem


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": "test-jwt-secret"},
        api={"host": "127.0.0.1", "port": 8000},
        # Deliberately empty integration keys so the snapshot booleans
        # are False — the test asserts shape, not truthiness of each.
        memory={"enabled": False},
    )


def test_startup_emits_helixis_startup_complete(
    test_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="helixis.main")

    app = create_app(test_settings)
    with TestClient(app):
        # Triggering the context-manager runs the lifespan startup;
        # we inspect log records without actually hitting any route.
        pass

    matches = [r for r in caplog.records if r.message == "helixis startup complete"]
    assert len(matches) == 1, (
        f"Expected exactly one 'helixis startup complete' log record; got {len(matches)}"
    )
    record = matches[0]
    expected_keys = {
        "langfuse",
        "posthog",
        "memory",
        "composio",
        "supabase",
        "buildium",
        "db_pool",
    }
    actual_keys = set(record.__dict__.keys()) & expected_keys
    assert actual_keys == expected_keys, (
        f"Startup snapshot is missing keys: {expected_keys - actual_keys}"
    )
    # All booleans, every one False given the empty test settings.
    for key in expected_keys:
        assert isinstance(record.__dict__[key], bool), f"{key} is not a bool"


def test_startup_warms_memory_when_available(
    test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When memory is live, boot fires a throwaway search so mem0's one-time
    spaCy/BM25 load happens at startup, not on the first user's chat turn."""
    fake = _fake_memory(available=True)
    monkeypatch.setattr(main_mod, "MemoryManager", lambda _settings: fake)

    app = create_app(test_settings)
    with TestClient(app):
        pass

    fake.initialize.assert_awaited_once()
    fake.search.assert_awaited_once()
    args, kwargs = fake.search.call_args
    # Sentinel company so it matches no real rows; no searched_by → no audit.
    assert kwargs.get("company_id") == "__warmup__"
    assert "searched_by" not in kwargs or kwargs["searched_by"] == ""


def test_startup_skips_warmup_when_memory_unavailable(
    test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No memory (no creds) → no warmup search; boot must not call search."""
    fake = _fake_memory(available=False)
    monkeypatch.setattr(main_mod, "MemoryManager", lambda _settings: fake)

    app = create_app(test_settings)
    with TestClient(app):
        pass

    fake.search.assert_not_awaited()
