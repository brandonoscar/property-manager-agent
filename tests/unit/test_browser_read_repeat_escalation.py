"""Grind guard: read_page steers to escalate when the agent re-reads the SAME
page past a threshold (case 063).

Re-reading a page returns the same content, so it will never surface an answer
that isn't there. ``_read_page_impl`` tracks the streak of identical
``(url, text)`` reads and, at ``_READ_REPEAT_ESCALATE_THRESHOLD``, hands the
agent a binding ``next_step`` pointing at ``escalate_to_user`` — never
screenshot (PR #155 regressed 063 by nudging toward screenshot-as-confirmation),
and only on genuine repetition (a page that carries the answer is answered on
the first read and never trips this — contrast 065).

CI-safe: a tiny fake page stands in for Playwright; the impl is exercised
directly. Deterministic — no live LLM.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from helixis.agents import browser_session as bs
from helixis.agents import browser_tools as bt
from helixis.settings import Settings


def _settings() -> Settings:
    return Settings(env="test", memory={"enabled": False})


class _Acc:
    def __init__(self, tree: Any) -> None:
        self._tree = tree

    async def snapshot(self) -> Any:
        return self._tree


class _FakePage:
    """Minimal page whose a11y tree (and therefore read text) is mutable."""

    def __init__(self, name: str, *, url: str = "https://example.test/page") -> None:
        self.accessibility = _Acc({"role": "WebArea", "name": name, "children": []})
        self.url = url

    def set_content(self, name: str) -> None:
        self.accessibility = _Acc({"role": "WebArea", "name": name, "children": []})

    async def inner_text(self, selector: str, timeout: int | None = None) -> str:
        return ""


def _bind(monkeypatch: pytest.MonkeyPatch, page: _FakePage) -> bs.BrowserSession:
    sess = bs.BrowserSession(
        company_id="co-1", session_id="sess-1", started_at=bs.time.monotonic(), page=page
    )
    monkeypatch.setattr(bt, "current_session", lambda: sess)
    monkeypatch.setattr(bt, "_log_activity", AsyncMock())
    return sess


@pytest.mark.asyncio
async def test_first_two_identical_reads_carry_no_next_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage("Zoning Board")
    _bind(monkeypatch, page)

    r1 = await bt._read_page_impl("", settings=_settings())
    r2 = await bt._read_page_impl("", settings=_settings())

    assert r1.status == "ok"
    assert r1.next_step == ""
    assert r2.next_step == ""  # streak 2 < threshold (3): one defensive re-read allowed


@pytest.mark.asyncio
async def test_third_identical_read_steers_to_escalate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage("Zoning Board")
    sess = _bind(monkeypatch, page)

    await bt._read_page_impl("", settings=_settings())
    await bt._read_page_impl("", settings=_settings())
    r3 = await bt._read_page_impl("", settings=_settings())

    assert sess.identical_read_streak == 3
    assert "escalate_to_user" in r3.next_step
    # steers to escalate, never to screenshot (PR #155 regression)
    assert "screenshot" not in r3.next_step.lower()
    # ...and keeps the read count well under the eval's read_page <= 5 cap
    assert r3.step_count == 3


@pytest.mark.asyncio
async def test_changed_content_resets_the_streak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage("Zoning Board")
    sess = _bind(monkeypatch, page)

    await bt._read_page_impl("", settings=_settings())
    await bt._read_page_impl("", settings=_settings())
    page.set_content("Meeting times: May 3, 6:00 PM")  # the answer appeared
    r3 = await bt._read_page_impl("", settings=_settings())

    assert sess.identical_read_streak == 1
    assert r3.next_step == ""  # changed page is productive — do NOT escalate


@pytest.mark.asyncio
async def test_page_that_carries_the_answer_never_nudges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contrast 065: a page answered on the first read never re-reads, so it
    never crosses the threshold."""
    page = _FakePage("Office hours: Monday through Friday, 9 AM to 4 PM")
    _bind(monkeypatch, page)

    r1 = await bt._read_page_impl("", settings=_settings())

    assert r1.status == "ok"
    assert "Monday" in r1.text
    assert r1.next_step == ""
