"""Tests for the PostHog analytics module.

Mirrors the structure of test_langfuse_setup.py — verifies configure /
shutdown lifecycle, capture helpers, and the no-op behavior when keys
are missing.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from helixis.analytics import posthog_client as _ph
from helixis.analytics.posthog_client import (
    capture,
    configure_posthog,
    feature_enabled,
    flush_posthog,
    group_identify,
    identify,
    is_active,
    shutdown_posthog,
)
from helixis.settings import PostHogSettings, Settings


@pytest.fixture(autouse=True)
def reset_module_state():
    """Module-level client is a singleton; isolate between tests."""
    _ph._client = None
    _ph._active = False
    yield
    _ph._client = None
    _ph._active = False


def _settings(**overrides) -> Settings:
    s = Settings(
        env="test",
        memory={"enabled": False},
        api={"host": "127.0.0.1", "port": 8000},
    )
    defaults = {
        "enabled": True,
        "project_api_key": "phc_test_key",
        "host": "https://us.i.posthog.com",
    }
    defaults.update(overrides)
    s.posthog = PostHogSettings(**defaults)
    return s


def _install_fake_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Put a MagicMock in the module's _client slot and mark active."""
    fake = MagicMock()
    monkeypatch.setattr(_ph, "_client", fake)
    monkeypatch.setattr(_ph, "_active", True)
    return fake


# ── configure_posthog ────────────────────────────────────────────────


def test_configure_with_valid_key_enables_analytics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid key + Posthog stub should flip is_active to True."""
    import sys
    import types

    fake_ph_module = types.SimpleNamespace(Posthog=MagicMock(return_value=MagicMock()))
    monkeypatch.setitem(sys.modules, "posthog", fake_ph_module)

    assert configure_posthog(_settings()) is True
    assert is_active() is True


def test_configure_disabled_returns_false() -> None:
    assert configure_posthog(_settings(enabled=False)) is False
    assert is_active() is False


def test_configure_missing_key_returns_false() -> None:
    assert configure_posthog(_settings(project_api_key="")) is False
    assert is_active() is False


def test_configure_swallows_init_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """If Posthog() raises during init, analytics goes off cleanly."""
    import sys
    import types

    def _boom(**_: object) -> None:
        raise RuntimeError("init failed")

    fake_ph_module = types.SimpleNamespace(Posthog=_boom)
    monkeypatch.setitem(sys.modules, "posthog", fake_ph_module)

    assert configure_posthog(_settings()) is False
    assert is_active() is False


# ── No-op behavior when inactive ─────────────────────────────────────


def test_all_helpers_noop_when_inactive() -> None:
    """No helper raises when PostHog isn't configured."""
    # None of these should touch a real network.
    capture("user-1", "test event", {"k": "v"}, company_id="co-1")
    identify("user-1", {"email": "x@y"})
    group_identify("company", "co-1", {"name": "Test Co"})
    flush_posthog()
    shutdown_posthog()


def test_feature_enabled_returns_default_when_inactive() -> None:
    assert feature_enabled("my-flag", "user-1") is False
    assert feature_enabled("my-flag", "user-1", default=True) is True


# ── Capture helpers forward to the underlying client ─────────────────


def test_capture_forwards_with_company_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_client(monkeypatch)

    capture(
        "user-1",
        "agent run started",
        {"session_id": "s1"},
        company_id="co-1",
    )

    fake.capture.assert_called_once_with(
        distinct_id="user-1",
        event="agent run started",
        properties={"session_id": "s1"},
        groups={"company": "co-1"},
    )


def test_capture_omits_group_when_no_company(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_client(monkeypatch)
    capture("user-1", "ping")
    fake.capture.assert_called_once_with(
        distinct_id="user-1",
        event="ping",
        properties={},
        groups={},
    )


def test_capture_swallows_client_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_client(monkeypatch)
    fake.capture.side_effect = RuntimeError("network down")
    # Must not raise — analytics can never break business logic.
    capture("user-1", "test event")


def test_identify_forwards(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_client(monkeypatch)
    identify("user-1", {"email": "test@example.com"})
    fake.identify.assert_called_once_with("user-1", {"email": "test@example.com"})


def test_group_identify_forwards(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_client(monkeypatch)
    group_identify("company", "co-1", {"name": "Acme PM"})
    fake.group_identify.assert_called_once_with("company", "co-1", {"name": "Acme PM"})


def test_feature_enabled_forwards_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_client(monkeypatch)
    fake.feature_enabled.return_value = True

    assert feature_enabled("beta-x", "user-1", company_id="co-1") is True
    fake.feature_enabled.assert_called_once_with(
        "beta-x",
        "user-1",
        groups={"company": "co-1"},
    )


def test_feature_enabled_returns_default_on_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When PostHog returns None (flag not found), fall back to default."""
    fake = _install_fake_client(monkeypatch)
    fake.feature_enabled.return_value = None
    assert feature_enabled("unknown-flag", "user-1", default=True) is True


def test_feature_enabled_returns_default_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_client(monkeypatch)
    fake.feature_enabled.side_effect = RuntimeError("timeout")
    assert feature_enabled("any-flag", "user-1", default=False) is False


def test_flush_forwards(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_client(monkeypatch)
    flush_posthog()
    fake.flush.assert_called_once()


def test_flush_swallows_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake_client(monkeypatch)
    fake.flush.side_effect = RuntimeError("boom")
    # Must not raise.
    flush_posthog()


def test_shutdown_calls_client_and_flips_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_client(monkeypatch)
    shutdown_posthog()
    fake.shutdown.assert_called_once()
    assert is_active() is False


def test_shutdown_clears_even_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The finally-block always clears state so subsequent configure works."""
    fake = _install_fake_client(monkeypatch)
    fake.shutdown.side_effect = RuntimeError("boom")
    shutdown_posthog()
    assert is_active() is False


# ── Onboarding funnel helpers ────────────────────────────────────────


def test_capture_signup_started(monkeypatch: pytest.MonkeyPatch) -> None:
    from helixis.analytics.posthog_client import capture_signup_started

    fake = _install_fake_client(monkeypatch)
    capture_signup_started("user-1", method="oauth_google")

    fake.capture.assert_called_once_with(
        distinct_id="user-1",
        event="signup_started",
        properties={"method": "oauth_google"},
        groups={},
    )


def test_capture_signup_completed_attaches_company_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from helixis.analytics.posthog_client import capture_signup_completed

    fake = _install_fake_client(monkeypatch)
    capture_signup_completed("user-1", method="email", company_id="co-1")

    fake.capture.assert_called_once_with(
        distinct_id="user-1",
        event="signup_completed",
        properties={"method": "email", "company_id": "co-1"},
        groups={"company": "co-1"},
    )


def test_capture_api_key_added(monkeypatch: pytest.MonkeyPatch) -> None:
    from helixis.analytics.posthog_client import capture_api_key_added

    fake = _install_fake_client(monkeypatch)
    capture_api_key_added("user-1", connector="buildium", company_id="co-1")

    fake.capture.assert_called_once_with(
        distinct_id="user-1",
        event="api_key_added",
        properties={"connector": "buildium"},
        groups={"company": "co-1"},
    )


def test_capture_webhook_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from helixis.analytics.posthog_client import capture_webhook_configured

    fake = _install_fake_client(monkeypatch)
    capture_webhook_configured("user-1", provider="buildium", company_id="co-1")

    fake.capture.assert_called_once_with(
        distinct_id="user-1",
        event="webhook_configured",
        properties={"provider": "buildium"},
        groups={"company": "co-1"},
    )


def test_funnel_helpers_noop_when_inactive() -> None:
    """All funnel helpers must early-return when PostHog isn't configured."""
    from helixis.analytics.posthog_client import (
        capture_api_key_added,
        capture_signup_completed,
        capture_signup_started,
        capture_webhook_configured,
    )

    # Module is reset by the autouse fixture; nothing should raise.
    capture_signup_started("user-1")
    capture_signup_completed("user-1", company_id="co-1")
    capture_api_key_added("user-1", connector="buildium")
    capture_webhook_configured("user-1", provider="buildium")
