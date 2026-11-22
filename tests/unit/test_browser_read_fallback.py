"""Browser read-layer hardening (#236) — a11y snapshot with body-text fallback.

The live #217 bug was that ``accessibility.snapshot()`` raises on real
Browserbase DOM even when the page rendered fine. #237 hardened submit-verify;
this generalizes the fallback into ``_read_page_lines`` and routes ``read_page``
and the screenshot auth-gate detector through it too.

CI-safe: a tiny fake page stands in for Playwright. The helper is unit-tested
directly; the two new wirings are exercised through their impls.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from helixis.agents import browser_session as bs
from helixis.agents import browser_tools as bt
from helixis.settings import Settings


def _settings() -> Settings:
    return Settings(env="test", memory={"enabled": False})


class _Acc:
    def __init__(self, tree: Any, *, raises: bool = False) -> None:
        self._tree = tree
        self._raises = raises

    async def snapshot(self) -> Any:
        if self._raises:
            raise AttributeError("accessibility.snapshot unavailable on this page")
        return self._tree


class _FakePage:
    """Minimal page: configurable a11y snapshot + body inner_text."""

    def __init__(
        self,
        *,
        tree: Any = None,
        a11y_raises: bool = False,
        body_text: str = "",
        body_raises: bool = False,
        url: str = "https://example.test/page",
    ) -> None:
        self.accessibility = _Acc(tree, raises=a11y_raises)
        self._body_text = body_text
        self._body_raises = body_raises
        self.url = url

    async def inner_text(self, selector: str, timeout: int | None = None) -> str:
        if self._body_raises:
            raise RuntimeError("body read failed")
        return self._body_text

    async def screenshot(self, **_kwargs: Any) -> bytes:
        return b"\x89PNG\r\n\x1a\n"


def _tree(name: str) -> dict:
    return {"role": "WebArea", "name": name, "children": []}


# ── _read_page_lines: the shared hardened primitive ──────────────────────────


@pytest.mark.asyncio
async def test_lines_from_a11y_when_snapshot_ok() -> None:
    page = _FakePage(tree=_tree("Dashboard"))
    lines, source = await bt._read_page_lines(page)
    assert source == "a11y"
    assert any("Dashboard" in ln for ln in lines)


@pytest.mark.asyncio
async def test_lines_fall_back_to_body_when_a11y_raises() -> None:
    page = _FakePage(a11y_raises=True, body_text="Welcome\nYour orders")
    lines, source = await bt._read_page_lines(page)
    assert source == "body"
    assert lines == ["Welcome", "Your orders"]


@pytest.mark.asyncio
async def test_lines_fall_back_to_body_when_snapshot_none() -> None:
    page = _FakePage(tree=None, body_text="Hello world")
    lines, source = await bt._read_page_lines(page)
    assert source == "body"
    assert lines == ["Hello world"]


@pytest.mark.asyncio
async def test_lines_unreadable_when_both_fail() -> None:
    """a11y raises AND body read throws → genuinely unreadable."""
    page = _FakePage(a11y_raises=True, body_raises=True)
    lines, source = await bt._read_page_lines(page)
    assert source == "unreadable"
    assert lines == []


@pytest.mark.asyncio
async def test_lines_empty_when_readable_but_blank() -> None:
    """Snapshot None + empty body = readable-but-blank, NOT an error."""
    page = _FakePage(tree=None, body_text="")
    lines, source = await bt._read_page_lines(page)
    assert source == "empty"
    assert lines == []


@pytest.mark.asyncio
async def test_lines_a11y_preferred_body_not_consulted() -> None:
    """When the snapshot works, the body fallback is never reached (so a
    nameless-but-present tree stays 'a11y', not 'body')."""
    page = _FakePage(tree=_tree("Home"), body_raises=True)
    lines, source = await bt._read_page_lines(page)
    assert source == "a11y"  # body_raises would have thrown if consulted
    assert any("Home" in ln for ln in lines)


# ── read_page wiring ─────────────────────────────────────────────────────────


def _bind(monkeypatch: pytest.MonkeyPatch, page: _FakePage) -> bs.BrowserSession:
    sess = bs.BrowserSession(
        company_id="co-1", session_id="sess-1", started_at=bs.time.monotonic(), page=page
    )
    monkeypatch.setattr(bt, "current_session", lambda: sess)
    monkeypatch.setattr(bt, "_log_activity", AsyncMock())
    return sess


@pytest.mark.asyncio
async def test_read_page_ok_via_body_fallback_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _FakePage(a11y_raises=True, body_text="Account balance: 42")
    _bind(monkeypatch, page)
    result = await bt._read_page_impl("balance", settings=_settings())
    assert result.status == "ok"
    assert "42" in result.text
    assert "read_via_body_text_fallback" in result.warnings


@pytest.mark.asyncio
async def test_read_page_extraction_failed_when_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _FakePage(a11y_raises=True, body_raises=True)
    _bind(monkeypatch, page)
    result = await bt._read_page_impl("x", settings=_settings())
    assert result.status == "extraction_failed"


@pytest.mark.asyncio
async def test_read_page_page_not_loaded_when_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _FakePage(tree=None, body_text="")
    _bind(monkeypatch, page)
    result = await bt._read_page_impl("x", settings=_settings())
    assert result.status == "page_not_loaded"


# ── screenshot auth-gate detection survives a flaky snapshot ──────────────────


def _screenshot_settings() -> Settings:
    return Settings(
        env="test",
        memory={"enabled": False},
        browser={
            "browserbase_api_key": "k",
            "browserbase_project_id": "p",
        },
    )


@pytest.mark.asyncio
async def test_screenshot_auth_gate_detected_via_body_fallback() -> None:
    """The a11y snapshot raises, but the login wall is visible in body text —
    auth-gate detection must still fire so the agent escalates (#236)."""
    page = _FakePage(
        a11y_raises=True,
        body_text="Sign in\nEmail address\nPassword\nForgot password",
    )
    sess = bs.BrowserSession(
        company_id="co-1", session_id="sess-1", started_at=bs.time.monotonic(), page=page
    )
    upload = AsyncMock(
        return_value=(
            "https://example-supabase.test/sign/x.png?token=t",
            datetime.now(UTC) + timedelta(hours=24),
        )
    )
    with (
        patch.object(bt, "current_session", return_value=sess),
        patch.object(bt, "_upload_screenshot_to_storage", new=upload),
        patch.object(bt, "_log_activity", new=AsyncMock()),
    ):
        result = await bt._screenshot_impl(settings=_screenshot_settings())

    assert result.status == "ok"
    assert "escalate_to_user" in result.next_step
    assert "auth_required" in result.next_step
