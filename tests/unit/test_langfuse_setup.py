"""Tests for the Langfuse observability setup.

Focus:
- Graceful no-op when keys are missing (backend must still boot).
- Metadata injector merges trace context into litellm kwargs.
- Concurrent async tasks see their own contexts (no cross-tenant leakage).
- Idempotent configuration.
"""

from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest

from helixis.observability import (
    TraceContext,
    configure_langfuse,
    current_trace_context,
    trace_context,
)
from helixis.observability import langfuse_setup as _setup_mod
from helixis.settings import LangfuseSettings, Settings


def _fake_litellm() -> types.SimpleNamespace:
    """Build a stand-in litellm module with just the surface we wrap."""
    captured: list[dict] = []

    async def acompletion(**kwargs: Any) -> dict:
        captured.append({"kind": "async", "kwargs": kwargs})
        return {"id": "ok"}

    def completion(**kwargs: Any) -> dict:
        captured.append({"kind": "sync", "kwargs": kwargs})
        return {"id": "ok"}

    module = types.SimpleNamespace(
        acompletion=acompletion,
        completion=completion,
        success_callback=[],
        failure_callback=[],
    )
    module._captured = captured  # type: ignore[attr-defined]
    return module


def _settings(**overrides: Any) -> Settings:
    s = Settings(
        env="test",
        memory={"enabled": False},
        api={"host": "127.0.0.1", "port": 8000},
    )
    defaults = {
        "enabled": True,
        "public_key": "pk",
        "secret_key": "sk",
        "host": "http://localhost:3000",
    }
    defaults.update(overrides)
    s.langfuse = LangfuseSettings(**defaults)
    return s


def _reset(monkeypatch: pytest.MonkeyPatch, fake: types.SimpleNamespace) -> None:
    """Swap in a fresh fake litellm and reset the _active flag."""
    monkeypatch.setitem(__import__("sys").modules, "litellm", fake)
    monkeypatch.setattr(_setup_mod, "_active", False)


def test_no_keys_is_graceful_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_litellm()
    _reset(monkeypatch, fake)

    s = _settings(public_key="", secret_key="")
    assert configure_langfuse(s) is False
    assert "langfuse" not in fake.success_callback
    assert "langfuse" not in fake.failure_callback


def test_disabled_flag_skips_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_litellm()
    _reset(monkeypatch, fake)

    s = _settings(enabled=False)
    assert configure_langfuse(s) is False
    assert fake.success_callback == []


def test_configure_registers_callbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_litellm()
    _reset(monkeypatch, fake)

    s = _settings()
    assert configure_langfuse(s) is True
    assert "langfuse" in fake.success_callback
    assert "langfuse" in fake.failure_callback


def test_mask_on_by_default_redacts_content(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_litellm()
    _reset(monkeypatch, fake)

    # Safe-by-default (Q25): with no override, generation content is masked so
    # raw tenant PII never ships to Langfuse Cloud.
    assert LangfuseSettings().mask_generation_content is True
    assert configure_langfuse(_settings()) is True
    assert fake.turn_off_message_logging is True


def test_mask_can_be_disabled_for_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_litellm()
    _reset(monkeypatch, fake)

    # Explicit opt-out (trusted dev env, synthetic data) keeps content raw.
    assert configure_langfuse(_settings(mask_generation_content=False)) is True
    assert getattr(fake, "turn_off_message_logging", False) is False


def test_mask_flag_on_redacts_generation_content(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_litellm()
    _reset(monkeypatch, fake)

    s = _settings(mask_generation_content=True)
    assert configure_langfuse(s) is True
    # LiteLLM redacts prompt/completion text for every logger incl. Langfuse.
    assert fake.turn_off_message_logging is True
    # Tracing itself is still on (structure/latency/tokens still flow).
    assert "langfuse" in fake.success_callback


def test_wrapper_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_litellm()
    _reset(monkeypatch, fake)

    s = _settings()
    configure_langfuse(s)
    wrapped_once = fake.acompletion
    configure_langfuse(s)
    assert fake.acompletion is wrapped_once, "wrapper must not double-wrap"


def test_metadata_injected_on_async_call(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_litellm()
    _reset(monkeypatch, fake)

    configure_langfuse(_settings())

    async def _run() -> None:
        with trace_context(company_id="acme", session_id="sess-1"):
            await fake.acompletion(model="gemini/2.5-flash", messages=[])

    asyncio.run(_run())

    [call] = fake._captured
    md = call["kwargs"]["metadata"]
    assert md["trace_user_id"] == "acme"
    assert md["session_id"] == "sess-1"
    assert md["trace_name"] == "helixis-agent"
    assert "helixis" in md["tags"]


def test_metadata_injected_on_sync_call(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_litellm()
    _reset(monkeypatch, fake)

    configure_langfuse(_settings())

    with trace_context(company_id="acme", session_id="sess-2"):
        fake.completion(model="gpt-4", messages=[])

    [call] = fake._captured
    md = call["kwargs"]["metadata"]
    assert md["session_id"] == "sess-2"


def test_no_trace_context_no_metadata_added(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_litellm()
    _reset(monkeypatch, fake)

    configure_langfuse(_settings())

    async def _run() -> None:
        await fake.acompletion(model="gemini/2.5-flash", messages=[])

    asyncio.run(_run())

    [call] = fake._captured
    assert "metadata" not in call["kwargs"]


def test_caller_metadata_merges_with_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_litellm()
    _reset(monkeypatch, fake)

    configure_langfuse(_settings())

    async def _run() -> None:
        with trace_context(company_id="acme", session_id="s1", tags=("browser",)):
            await fake.acompletion(
                model="gemini/2.5-flash",
                messages=[],
                metadata={"custom_field": "x", "tags": ["extra"]},
            )

    asyncio.run(_run())
    md = fake._captured[0]["kwargs"]["metadata"]
    # Caller keys preserved
    assert md["custom_field"] == "x"
    # Tags merged: defaults + context + caller, deduped
    assert "helixis" in md["tags"]
    assert "browser" in md["tags"]
    assert "extra" in md["tags"]
    # Context identity preserved
    assert md["trace_user_id"] == "acme"


def test_concurrent_tasks_see_separate_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_litellm()
    _reset(monkeypatch, fake)
    configure_langfuse(_settings())

    async def tenant_call(company: str, session: str) -> str:
        with trace_context(company_id=company, session_id=session):
            await asyncio.sleep(0)  # yield — let the scheduler interleave
            await fake.acompletion(model="m", messages=[])
            return session

    async def _run() -> list[str]:
        return await asyncio.gather(
            tenant_call("acme", "s-acme"),
            tenant_call("globex", "s-globex"),
            tenant_call("initech", "s-initech"),
        )

    asyncio.run(_run())

    user_ids = {c["kwargs"]["metadata"]["trace_user_id"] for c in fake._captured}
    session_ids = {c["kwargs"]["metadata"]["session_id"] for c in fake._captured}
    assert user_ids == {"acme", "globex", "initech"}
    assert session_ids == {"s-acme", "s-globex", "s-initech"}


def test_trace_context_restored_after_exit() -> None:
    assert current_trace_context() is None
    with trace_context(company_id="acme", session_id="s"):
        inner = current_trace_context()
        assert isinstance(inner, TraceContext)
        assert inner.company_id == "acme"
    assert current_trace_context() is None


def test_to_metadata_shape() -> None:
    ctx = TraceContext(company_id="acme", session_id="s1", trace_name="custom", tags=("a", "b"))
    md = ctx.to_metadata()
    assert md == {
        "trace_user_id": "acme",
        "session_id": "s1",
        "trace_name": "custom",
        "tags": ["a", "b"],
    }
