"""Unit tests for browser_tools.

Two layers exercised:

1. **Deny-list integration at the tool boundary** — navigate() must
   refuse hard- and soft-blocked URLs without ever touching
   Browserbase. The deny-list module itself is tested separately
   in ``test_browser_denylist.py``; here we test that the tool
   wraps it correctly.

2. **browser_unavailable degradation** — without Browserbase
   credentials, every tool returns the ``browser_unavailable``
   status cleanly, never raises.

3. **Activity-log identity-rule sanitizer** — the escalate tool's
   ``reason`` field is sanitized before persistence even when the
   LLM puts a Buildium-shaped ID in it.

Full Browserbase + Playwright integration tests live separately in
``tests/integration/agents/test_browser_agent.py`` (gated behind
the ``[browser]`` optional extra; skipped in CI).
"""

from __future__ import annotations

import contextlib
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.agents import browser_session as bs
from helixis.agents import browser_tools as bt
from helixis.settings import Settings


def _settings(api_key: str = "", project_id: str = "") -> Settings:
    return Settings(
        env="test",
        llm={"default_model": "gemini/gemini-2.5-flash", "google_api_key": "g"},
        memory={"enabled": False},
        browser={
            "browserbase_api_key": api_key,
            "browserbase_project_id": project_id,
            "max_steps_per_task": 15,
            "max_wall_clock_seconds": 75,
            "max_screenshots_per_task": 8,
        },
    )


# ── deny-list integration on navigate() ─────────────────────────


@pytest.mark.asyncio
async def test_navigate_hard_blocks_twitter() -> None:
    result = await bt._navigate_impl(
        "https://twitter.com/search",
        settings=_settings(),
    )
    assert result.status == "disallowed_domain"
    assert result.deny_category == "hard_blocked"
    assert result.deny_reason == "social_media"
    assert (
        "social_media" in result.user_facing_message.lower()
        or "type of site" in result.user_facing_message.lower()
    )


@pytest.mark.asyncio
async def test_navigate_soft_blocks_competitor() -> None:
    result = await bt._navigate_impl(
        "https://appfolio.com/dashboard",
        settings=_settings(),
    )
    assert result.status == "disallowed_domain"
    assert result.deny_category == "soft_blocked"
    assert result.deny_reason == "competitor_pm_saas"
    assert "competitor" in result.user_facing_message.lower()


@pytest.mark.asyncio
async def test_navigate_blocks_malformed_url() -> None:
    result = await bt._navigate_impl("not-a-url", settings=_settings())
    assert result.status == "disallowed_domain"
    assert result.deny_reason == "malformed_url"


@pytest.mark.asyncio
async def test_navigate_empty_string_returns_invalid_input() -> None:
    """Empty URL is caught at validation before deny-list runs."""
    result = await bt._navigate_impl("", settings=_settings())
    assert result.status == "invalid_input"
    assert any("non-empty" in w for w in result.warnings)


# ── browser_unavailable when creds missing ──────────────────────


@pytest.mark.asyncio
async def test_navigate_returns_browser_unavailable_without_creds() -> None:
    """``check_domain`` allows the URL, but session creation should
    fail because creds aren't configured. Tool surfaces the failure
    as ``browser_unavailable`` without raising."""
    result = await bt._navigate_impl(
        "https://example.com/",
        settings=_settings(),
    )
    assert result.status == "browser_unavailable"
    assert "browser_unavailable" in result.warnings


@pytest.mark.asyncio
async def test_read_page_returns_browser_unavailable_without_session() -> None:
    """``read_page`` doesn't create a session — only ``navigate``
    does. Called before navigate (no session bound) → unavailable."""
    result = await bt._read_page_impl("query", settings=_settings())
    assert result.status == "browser_unavailable"


@pytest.mark.asyncio
async def test_extract_text_returns_browser_unavailable_without_session() -> None:
    result = await bt._extract_text_impl(".price", settings=_settings())
    assert result.status == "browser_unavailable"


@pytest.mark.asyncio
async def test_click_returns_browser_unavailable_without_session() -> None:
    result = await bt._click_impl("button.next", settings=_settings())
    assert result.status == "browser_unavailable"


@pytest.mark.asyncio
async def test_screenshot_returns_browser_unavailable_without_session() -> None:
    result = await bt._screenshot_impl(settings=_settings())
    assert result.status == "browser_unavailable"


# ── escalate_to_user — never touches Playwright ────────────────


@pytest.mark.asyncio
async def test_escalate_to_user_returns_escalated_status() -> None:
    result = await bt._escalate_to_user_impl(
        "encountered a login wall",
        "https://supabase.test/x.png",
        settings=_settings(),
    )
    assert result.status == "escalated"
    assert "login wall" in result.reason
    assert result.last_screenshot_url == "https://supabase.test/x.png"


@pytest.mark.asyncio
async def test_escalate_to_user_works_without_creds() -> None:
    """Escalation doesn't require Browserbase — it's an LLM-side
    signal, not a browser action."""
    result = await bt._escalate_to_user_impl(
        "could not find data",
        settings=_settings(),
    )
    assert result.status == "escalated"


# ── identity-rule sanitizer on activity log writes ─────────────


@pytest.mark.asyncio
async def test_escalate_sanitizes_buildium_id_in_log_description() -> None:
    """If the LLM includes a Buildium-ID-shaped string in the
    escalation reason, the activity-log description must redact it
    even though the agent's reply itself is the LLM's responsibility."""
    from helixis.memory.context import memory_context

    fake_pool = MagicMock()
    write_called: dict[str, Any] = {}

    async def _fake_write(pool: Any, entry: Any) -> None:
        write_called["entry"] = entry

    with (
        patch.object(bt.db_engine, "get_pool", return_value=fake_pool),
        patch.object(bt, "write_activity", new=_fake_write),
        memory_context(company_id="co-1", user_id="u-1", session_id="s-1"),
    ):
        await bt._escalate_to_user_impl(
            "could not find Task #12345 on the page; escalating",
            settings=_settings(),
        )

    entry = write_called["entry"]
    assert entry.action_type == "browser_escalated"
    assert "Task #12345" not in entry.description
    assert "[id-redacted]" in entry.description


@pytest.mark.asyncio
async def test_blocked_navigate_logs_activity_with_deny_reason() -> None:
    """Hard-blocked navigation writes an activity row with
    ``action_type=browser_blocked`` and the deny reason in raw,
    so the Activity page surfaces every deny-list event."""
    from helixis.memory.context import memory_context

    fake_pool = MagicMock()
    write_called: dict[str, Any] = {}

    async def _fake_write(pool: Any, entry: Any) -> None:
        write_called["entry"] = entry

    with (
        patch.object(bt.db_engine, "get_pool", return_value=fake_pool),
        patch.object(bt, "write_activity", new=_fake_write),
        memory_context(company_id="co-A", user_id="u-1", session_id="s-1"),
    ):
        await bt._navigate_impl(
            "https://twitter.com/search?q=test",
            settings=_settings(),
        )

    entry = write_called["entry"]
    assert entry.action_type == "browser_blocked"
    assert entry.company_id == "co-A"
    assert entry.raw["deny_category"] == "hard_blocked"
    assert entry.raw["deny_reason"] == "social_media"


# ── Supabase Storage upload — failure handling ─────────────────


@pytest.mark.asyncio
async def test_upload_screenshot_returns_none_when_supabase_unconfigured() -> None:
    """No ``supabase.url`` set → upload skipped, returns None.
    Caller translates None to ``screenshot_failed``."""
    s = _settings()
    # Default Settings has empty supabase.url; verify upload helper bails
    result = await bt._upload_screenshot_to_storage(
        png_bytes=b"\x89PNG\r\n\x1a\n",
        company_id="co",
        session_id="s1",
        step=1,
        settings=s,
    )
    assert result is None


@pytest.mark.asyncio
async def test_screenshot_path_is_company_scoped() -> None:
    """Multi-tenant property: the upload path includes
    ``{company_id}/`` so company A's screenshots can't collide with
    company B's. This is code-side scoping; bucket-level RLS in
    Supabase is a separate config concern (called out in the PR
    description as a follow-up)."""
    # The path-building logic lives inside _upload_screenshot_to_storage.
    # We verify it indirectly by patching httpx and inspecting the URL.
    s = Settings(
        env="test",
        llm={"default_model": "gemini/g", "google_api_key": "g"},
        memory={"enabled": False},
        supabase={"url": "https://supabase.test", "service_key": "svc"},
        browser={"browserbase_api_key": "x", "browserbase_project_id": "y"},
    )

    captured_urls: list[str] = []

    class _FakeResp:
        status_code = 200
        text = ""

        def json(self) -> dict[str, Any]:
            return {"signedURL": "/object/sign/browser_screenshots/co-A/s1/1.png?token=t"}

    async def _fake_post(self: Any, url: str, **kw: Any) -> _FakeResp:
        captured_urls.append(url)
        return _FakeResp()

    import httpx as _httpx

    with patch.object(_httpx.AsyncClient, "post", _fake_post):
        result = await bt._upload_screenshot_to_storage(
            png_bytes=b"\x89PNG\r\n\x1a\n",
            company_id="co-A",
            session_id="s1",
            step=1,
            settings=s,
        )

    assert result is not None
    signed_url, _expires = result
    # The upload URL must include the company-scoped path
    assert any("browser_screenshots/co-A/s1/1.png" in u for u in captured_urls)
    # The signed URL must be returned fully-qualified
    assert signed_url.startswith("https://supabase.test/storage/v1")


# ── type_into — the first INPUT primitive ───────────────────────────
#
# type_into fills a form field (find -> fill, the start of end-to-end
# browser work). It mirrors click's structure: browser_unavailable guard,
# input validation, budget check, Playwright fill, structured result.
# Critical safety property under test: the typed VALUE is never echoed
# back to the model or written to the activity log — only the field
# selector and a character count.


@contextlib.contextmanager
def _bound_session(page: Any, *, started_at: float | None = None):
    """Bind a BrowserSession with a mock ``page`` for the duration of a
    tool call, then restore. Mirrors what ensure_session does in prod
    without any Browserbase/Playwright."""
    session = bs.BrowserSession(
        company_id="co-1",
        session_id="sess-1",
        started_at=started_at if started_at is not None else time.monotonic(),
        page=page,
    )
    token = bs._session_var.set(session)  # noqa: SLF001
    try:
        yield session
    finally:
        bs._session_var.reset(token)  # noqa: SLF001


def _page_with_fill(fill_mock: AsyncMock) -> Any:
    """A mock Playwright page whose ``locator(...).first.fill`` is fill_mock."""
    first = MagicMock()
    first.fill = fill_mock
    locator = MagicMock()
    locator.first = first
    page = MagicMock()
    page.locator = MagicMock(return_value=locator)
    return page


@pytest.mark.asyncio
async def test_type_into_returns_browser_unavailable_without_session() -> None:
    result = await bt._type_into_impl("input#name", "Jane Tenant", settings=_settings())
    assert result.status == "browser_unavailable"
    assert "browser_unavailable" in result.warnings


@pytest.mark.asyncio
async def test_type_into_fills_field_and_reports_char_count() -> None:
    fill = AsyncMock()
    page = _page_with_fill(fill)
    secret_value = "Jane Q. Tenant"

    with patch.object(bt, "_log_activity", new=AsyncMock()) as log, _bound_session(page):
        result = await bt._type_into_impl("input#name", secret_value, settings=_settings())

    assert result.status == "ok"
    assert result.char_count == len(secret_value)
    assert result.step_count == 1
    # The field was actually filled with the real value...
    page.locator.assert_called_once_with("input#name")
    fill.assert_awaited_once()
    assert fill.await_args.args[0] == secret_value
    # ...but the value must NOT appear in the model-facing result.
    assert secret_value not in result.model_dump_json()
    # ...nor in the activity log (selector + char_count only, no value).
    log.assert_awaited_once()
    log_raw = log.await_args.kwargs["raw"]
    assert log_raw["char_count"] == len(secret_value)
    assert secret_value not in str(log_raw)
    assert secret_value not in log.await_args.kwargs["description"]


@pytest.mark.asyncio
async def test_type_into_empty_text_is_invalid_input() -> None:
    page = _page_with_fill(AsyncMock())
    with _bound_session(page):
        result = await bt._type_into_impl("input#name", "", settings=_settings())
    assert result.status == "invalid_input"


@pytest.mark.asyncio
async def test_type_into_empty_selector_is_invalid_input() -> None:
    page = _page_with_fill(AsyncMock())
    with _bound_session(page):
        result = await bt._type_into_impl("   ", "value", settings=_settings())
    assert result.status == "invalid_input"


@pytest.mark.asyncio
async def test_type_into_playwright_error_returns_navigation_failed() -> None:
    fill = AsyncMock(side_effect=RuntimeError("element not found"))
    page = _page_with_fill(fill)
    with patch.object(bt, "_log_activity", new=AsyncMock()) as log, _bound_session(page):
        result = await bt._type_into_impl("input#missing", "x", settings=_settings())
    assert result.status == "navigation_failed"
    assert any("type_failed" in w for w in result.warnings)
    # A failed fill must NOT write a success activity-log row.
    log.assert_not_awaited()


@pytest.mark.asyncio
async def test_type_into_respects_step_budget() -> None:
    fill = AsyncMock()
    page = _page_with_fill(fill)
    s = _settings()
    s.browser.max_steps_per_task = 1
    # Pre-consume the single step so this call trips the budget.
    with _bound_session(page) as session:
        session.step_count = 1
        result = await bt._type_into_impl("input#name", "value", settings=s)
    assert result.status == "budget_exhausted"
    # The field must NOT have been filled once over budget.
    fill.assert_not_awaited()
