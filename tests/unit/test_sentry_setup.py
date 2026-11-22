"""Tests for Sentry error-tracking setup.

Same shape as test_langfuse_setup / test_posthog_analytics — verify the
configure / shutdown lifecycle and graceful no-op when the DSN is
missing.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from helixis.observability import sentry_setup as _sentry
from helixis.observability.sentry_setup import (
    configure_sentry,
    is_active,
    shutdown_sentry,
)
from helixis.settings import SentrySettings, Settings


@pytest.fixture(autouse=True)
def reset_module_state():
    _sentry._active = False
    yield
    _sentry._active = False


def _settings(**overrides) -> Settings:
    s = Settings(
        env="test",
        memory={"enabled": False},
        api={"host": "127.0.0.1", "port": 8000},
    )
    defaults = {
        "enabled": True,
        "dsn": "https://abc@o0.ingest.sentry.io/0",
        "traces_sample_rate": 0.2,
        "send_default_pii": False,
    }
    defaults.update(overrides)
    s.sentry = SentrySettings(**defaults)
    return s


def _install_fake_sentry(monkeypatch: pytest.MonkeyPatch, init: object = None) -> MagicMock:
    """Replace the sentry_sdk module + its integration submodules so
    init() can be called without any real network I/O.

    ``init`` replaces ``sentry_sdk.init`` (a fresh MagicMock by default).
    ``sentry_sdk.scrubber`` is faked too: ``configure_sentry`` imports it, and
    without it every caller here would stop at that import and read as
    "Sentry off" for the wrong reason.
    """
    fake_init = MagicMock() if init is None else init
    fake_sdk = types.SimpleNamespace(init=fake_init, get_client=MagicMock(name="get_client"))

    # Integration subpackages must exist as real module objects because
    # the setup imports FastApiIntegration/etc. by attribute.
    def _make_integration_module(name: str) -> types.ModuleType:
        mod = types.ModuleType(f"sentry_sdk.integrations.{name}")
        cls_name = {
            "asyncio": "AsyncioIntegration",
            "fastapi": "FastApiIntegration",
            "starlette": "StarletteIntegration",
        }[name]
        setattr(mod, cls_name, MagicMock())
        return mod

    fake_integrations = types.ModuleType("sentry_sdk.integrations")
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake_sdk)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations", fake_integrations)
    for name in ("asyncio", "fastapi", "starlette"):
        monkeypatch.setitem(
            sys.modules,
            f"sentry_sdk.integrations.{name}",
            _make_integration_module(name),
        )
    fake_scrubber = types.ModuleType("sentry_sdk.scrubber")
    fake_scrubber.DEFAULT_DENYLIST = ["password", "secret", "token"]  # type: ignore[attr-defined]
    fake_scrubber.EventScrubber = MagicMock(name="EventScrubber")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentry_sdk.scrubber", fake_scrubber)
    return fake_init  # type: ignore[return-value]


# ── configure_sentry ────────────────────────────────────────────────


def test_configure_with_dsn_enables_tracking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_init = _install_fake_sentry(monkeypatch)

    assert configure_sentry(_settings()) is True
    assert is_active() is True
    fake_init.assert_called_once()
    # DSN + env passed through.
    kwargs = fake_init.call_args.kwargs
    assert kwargs["dsn"] == "https://abc@o0.ingest.sentry.io/0"
    assert kwargs["environment"] == "test"
    assert kwargs["traces_sample_rate"] == 0.2


def test_configure_never_sends_bodies_frame_variables_or_query_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ The three settings that kept customer credentials out of Sentry.

    Measured 2026-10-05 (sentry-sdk 2.71.0): with the SDK's defaults, a
    Buildium or Rentvine credentials save sent the secret in the sampled
    transaction's request body, and on a 500 in the frame variables (the raw
    ``body_bytes`` and the parsed model's repr). ``test_sentry_sends_no_
    secrets`` proves the behaviour end to end; this pins the wiring.
    """
    fake_init = _install_fake_sentry(monkeypatch)
    assert configure_sentry(_settings()) is True

    kwargs = fake_init.call_args.kwargs
    assert kwargs["max_request_body_size"] == "never"
    assert kwargs["include_local_variables"] is False
    assert kwargs["before_send"] is _sentry.strip_query_values
    assert kwargs["before_send_transaction"] is _sentry.strip_query_values

    fake_scrubber = sys.modules["sentry_sdk.scrubber"]
    scrubber_kwargs = fake_scrubber.EventScrubber.call_args.kwargs
    assert kwargs["event_scrubber"] is fake_scrubber.EventScrubber.return_value
    assert scrubber_kwargs["recursive"] is True
    # The SDK's own list is kept, and every credential name is added to it.
    denylist = scrubber_kwargs["denylist"]
    assert set(fake_scrubber.DEFAULT_DENYLIST) <= set(denylist)
    assert set(_sentry.SENSITIVE_KEYS) <= set(denylist)
    # The SDK's PII rule follows the setting, as it did before a scrubber was
    # passed in.
    assert scrubber_kwargs["send_default_pii"] is False


def test_sensitive_keys_cover_every_credential_column() -> None:
    """Every ``encrypted_*`` column a credentials table holds, and the plain
    name it decrypts to, is in the list.

    Read from the migrations, so a new connector's credentials table cannot
    land with a field the scrubber does not know.
    """
    import re
    from pathlib import Path

    migrations = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
    columns: set[str] = set()
    for sql in migrations.glob("*.sql"):
        columns |= set(re.findall(r"\b(encrypted_[a-z_]+)\b", sql.read_text()))
    assert columns, "no encrypted_* column found: the migrations moved?"
    missing = sorted(
        name
        for column in columns
        for name in (column, column.removeprefix("encrypted_"))
        if name not in _sentry.SENSITIVE_KEYS
        and name.lower() not in {"password", "secret", "api_key", "token"}
    )
    assert not missing, f"credential names Sentry would send in plain text: {missing}"


def test_configure_disabled_flag() -> None:
    assert configure_sentry(_settings(enabled=False)) is False
    assert is_active() is False


def test_configure_missing_dsn() -> None:
    assert configure_sentry(_settings(dsn="")) is False
    assert is_active() is False


def test_configure_swallows_init_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """If sentry_sdk.init raises, we degrade cleanly — no re-throw."""

    boom = MagicMock(side_effect=RuntimeError("bad dsn"))
    _install_fake_sentry(monkeypatch, init=boom)

    assert configure_sentry(_settings()) is False
    assert is_active() is False
    # ⚠ init was reached. Before the scrubber import was faked, this test
    # stopped at that import and passed without ever calling init.
    boom.assert_called_once()


# ── strip_query_values ──────────────────────────────────────────────


def test_strip_query_values_keeps_names_and_drops_values() -> None:
    """Inbound (``request.query_string``), outbound spans and breadcrumbs."""
    event = {
        "request": {"url": "https://api.test/ws", "query_string": "token=eyJ.jwt.sig"},
        "spans": [
            {
                "op": "http.client",
                "data": {"url": "https://maps.test/geo", "http.query": "address=1+Main&key=AIzaK"},
            },
            {"op": "db", "data": {"db.system": "postgresql"}},
        ],
        "breadcrumbs": {
            "values": [
                {"type": "http", "data": {"http.query": "access_token=pk.secret&limit=1"}},
                {"type": "default", "message": "no data"},
            ]
        },
    }

    out = _sentry.strip_query_values(event, {})

    assert out is event
    assert event["request"]["query_string"] == "token=[Filtered]"
    assert event["spans"][0]["data"]["http.query"] == "address=[Filtered]&key=[Filtered]"
    assert event["spans"][0]["data"]["url"] == "https://maps.test/geo"
    assert event["spans"][1]["data"] == {"db.system": "postgresql"}
    crumbs = event["breadcrumbs"]["values"]
    assert crumbs[0]["data"]["http.query"] == "access_token=[Filtered]&limit=[Filtered]"
    assert "eyJ" not in str(event) and "AIzaK" not in str(event) and "pk.secret" not in str(event)


def test_strip_query_values_leaves_an_empty_or_absent_query_alone() -> None:
    event: dict = {"request": {"query_string": ""}, "spans": [{"data": {"http.query": ""}}]}
    _sentry.strip_query_values(event, {})
    assert event == {"request": {"query_string": ""}, "spans": [{"data": {"http.query": ""}}]}
    assert _sentry.strip_query_values({"message": "x"}, {}) == {"message": "x"}


def test_strip_query_values_keeps_a_name_without_a_value() -> None:
    event = {"request": {"query_string": "flag&code=abc"}}
    _sentry.strip_query_values(event, {})
    assert event["request"]["query_string"] == "flag=[Filtered]&code=[Filtered]"


# ── shutdown_sentry ─────────────────────────────────────────────────


def test_shutdown_is_noop_when_inactive() -> None:
    # Should not raise.
    shutdown_sentry()


def test_shutdown_flushes_when_active(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_sentry(monkeypatch)
    configure_sentry(_settings())

    # Now hand back a client whose flush we can assert.
    import sentry_sdk as fake

    client = MagicMock()
    fake.get_client.return_value = client

    shutdown_sentry()
    client.flush.assert_called_once()


def test_shutdown_swallows_flush_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_sentry(monkeypatch)
    configure_sentry(_settings())

    import sentry_sdk as fake

    client = MagicMock()
    client.flush.side_effect = RuntimeError("network gone")
    fake.get_client.return_value = client

    # Must not raise.
    shutdown_sentry()


# ── capture_swallowed (P2, task #34) ────────────────────────────────


def test_capture_swallowed_noop_when_inactive() -> None:
    """Sentry off → no import attempt, no error, nothing captured."""
    from helixis.observability.sentry_setup import capture_swallowed

    _sentry._active = False
    capture_swallowed(RuntimeError("boom"), op="memory.search")  # must not raise


def test_capture_swallowed_forwards_with_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sentry on → the swallowed exception reaches capture_exception, tagged
    with the operation so bursts group by memory op in the issue UI."""
    from helixis.observability.sentry_setup import capture_swallowed

    scope = MagicMock()

    class _Scope:
        def __enter__(self):  # noqa: ANN204
            return scope

        def __exit__(self, *args):  # noqa: ANN002, ANN204
            return False

    fake_sdk = types.SimpleNamespace(
        new_scope=lambda: _Scope(),
        capture_exception=MagicMock(),
    )
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake_sdk)
    _sentry._active = True

    err = RuntimeError("embedder down")
    capture_swallowed(err, op="memory.search")

    fake_sdk.capture_exception.assert_called_once_with(err)
    scope.set_tag.assert_any_call("swallowed", "true")
    scope.set_tag.assert_any_call("operation", "memory.search")


def test_capture_swallowed_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken SDK must not turn observability into a new failure mode."""
    from helixis.observability.sentry_setup import capture_swallowed

    fake_sdk = types.SimpleNamespace(
        new_scope=MagicMock(side_effect=RuntimeError("sdk broken")),
        capture_exception=MagicMock(),
    )
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake_sdk)
    _sentry._active = True

    capture_swallowed(RuntimeError("boom"), op="memory.add")  # must not raise


# ── capture_notice (feedback arrival, 2026-09-08) ───────────────────


def test_capture_notice_noop_when_inactive() -> None:
    """Sentry off → no import attempt, no error, nothing captured."""
    from helixis.observability.sentry_setup import capture_notice

    _sentry._active = False
    capture_notice("a report arrived", op="feedback.submitted")  # must not raise


def test_capture_notice_sends_a_message_at_the_requested_level() -> None:
    """⚠ A MESSAGE, not an exception. The other two captures both start from an
    exception, so before this the only things that reached Sentry were
    failures — and a customer filing a bug report is not a failure. It landed
    in `helixis_feedback`, which no code anywhere reads.
    """
    from helixis.observability.sentry_setup import capture_notice

    scope = MagicMock()

    class _Scope:
        def __enter__(self):  # noqa: ANN204
            return scope

        def __exit__(self, *args):  # noqa: ANN002, ANN204
            return False

    fake_sdk = types.SimpleNamespace(
        new_scope=lambda: _Scope(),
        capture_message=MagicMock(),
        capture_exception=MagicMock(),
    )
    with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
        _sentry._active = True
        capture_notice(
            "Feedback (bug) from a customer — read helixis_feedback abc",
            op="feedback.submitted",
            level="warning",
            tags={"feedback_category": "bug"},
        )

    fake_sdk.capture_message.assert_called_once()
    assert fake_sdk.capture_message.call_args.kwargs["level"] == "warning"
    # ⚠ Never capture_exception. A notice grouped as an exception would sit in
    # the same stream as a 500 and erode the one signal that must stay loud.
    fake_sdk.capture_exception.assert_not_called()
    scope.set_tag.assert_any_call("notice", "true")
    scope.set_tag.assert_any_call("operation", "feedback.submitted")
    scope.set_tag.assert_any_call("feedback_category", "bug")


def test_capture_notice_never_raises() -> None:
    """A broken SDK must not turn a bug report into a failed submission."""
    from helixis.observability.sentry_setup import capture_notice

    fake_sdk = types.SimpleNamespace(
        new_scope=MagicMock(side_effect=RuntimeError("sdk broken")),
        capture_message=MagicMock(),
    )
    with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
        _sentry._active = True
        capture_notice("something happened", op="feedback.submitted")  # must not raise
    _sentry._active = False
