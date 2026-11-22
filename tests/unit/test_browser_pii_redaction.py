"""read_page / extract_text redact contact PII from what the MODEL reads (067).

read_page only redacted PII for logs (``_sanitize_for_log``), not the text
handed to the model, so the browser agent could echo a page's incidental
contact details into its reply (~1-in-5, flaking the browser sweep). Now the
page-reading tools apply ``pii.redact()`` to the returned text, so contact PII
is stripped at the source — deterministic, not dependent on the model obeying
prompt rule 8. Non-contact answers (a fee, hours) are preserved.

CI-safe: a tiny fake page stands in for Playwright; the impls are exercised
directly. Deterministic — no live LLM.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from helixis.agents import browser_session as bs
from helixis.agents import browser_tools as bt
from helixis.settings import Settings

_PII_PAGE = (
    "Permits Office\n"
    "Roof inspection permit fee: $145, payable online.\n"
    "Questions? Email the clerk at clerk@example-county.test or call (212) 555-0177."
)


def _settings() -> Settings:
    return Settings(env="test", memory={"enabled": False})


class _Acc:
    def __init__(self, *, raises: bool = True) -> None:
        self._raises = raises

    async def snapshot(self) -> Any:
        raise AttributeError("force body-text fallback")


class _Locator:
    def __init__(self, text: str) -> None:
        self._text = text
        self.first = self

    async def inner_text(self, timeout: int | None = None) -> str:
        return self._text


class _FakePage:
    def __init__(self, body_text: str, *, url: str = "https://example-county.test/permits") -> None:
        self.accessibility = _Acc()
        self._body_text = body_text
        self.url = url

    async def inner_text(self, selector: str, timeout: int | None = None) -> str:
        return self._body_text

    def locator(self, selector_or_role: str) -> _Locator:
        return _Locator(self._body_text)


def _bind(monkeypatch: pytest.MonkeyPatch, page: _FakePage) -> bs.BrowserSession:
    sess = bs.BrowserSession(
        company_id="co-1", session_id="sess-1", started_at=bs.time.monotonic(), page=page
    )
    monkeypatch.setattr(bt, "current_session", lambda: sess)
    monkeypatch.setattr(bt, "_log_activity", AsyncMock())
    return sess


_FORBIDDEN = [
    "clerk@example-county.test",
    "@example-county.test",
    "555-0177",
    "(212) 555-0177",
]


@pytest.mark.asyncio
async def test_read_page_redacts_contact_pii_keeps_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(monkeypatch, _FakePage(_PII_PAGE))
    result = await bt._read_page_impl("", settings=_settings())
    assert result.status == "ok"
    # the non-contact answer survives
    assert "145" in result.text
    # every forbidden contact string is gone
    for forbidden in _FORBIDDEN:
        assert forbidden not in result.text, f"leaked {forbidden!r}"


@pytest.mark.asyncio
async def test_extract_text_redacts_contact_pii(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, _FakePage(_PII_PAGE))
    result = await bt._extract_text_impl("body", settings=_settings())
    assert result.status == "ok"
    for forbidden in _FORBIDDEN:
        assert forbidden not in result.text, f"leaked {forbidden!r}"


@pytest.mark.asyncio
async def test_read_page_without_pii_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redaction is a no-op on pages with no contact PII (065-class)."""
    page = _FakePage("Office hours: Monday through Friday, 9:00 AM to 4:00 PM.")
    _bind(monkeypatch, page)
    result = await bt._read_page_impl("", settings=_settings())
    assert "Monday" in result.text and "Friday" in result.text
    assert "[redacted]" not in result.text
