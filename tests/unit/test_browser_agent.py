"""Construction + prompt-regression tests for the browser_agent.

Heavier integration tests (real Browserbase, real Playwright) live
under ``tests/integration/`` and require the ``[browser]`` extra;
those are skipped in CI. These tests verify the structural wiring:
the specialist constructs cleanly, carries the six tools, and the
system prompt enforces the hard-rule discipline the eval cases
depend on.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.agents import browser_agent as ba
from helixis.agents import browser_session as bs
from helixis.agents import browser_tools as bt
from helixis.settings import Settings


def _settings() -> Settings:
    return Settings(
        env="test",
        llm={"default_model": "gemini/gemini-2.5-flash", "google_api_key": "g"},
        memory={"enabled": False},
    )


def test_browser_agent_constructs_with_nine_tools() -> None:
    """Nine tools: navigate, read_page, extract_text, click, type_into,
    select_option, submit_form, screenshot, escalate_to_user.
    ``type_into`` / ``select_option`` are reversible fill primitives;
    ``submit_form`` is the irreversible submit, confirmation-gated via
    ``confirm_or_abort``. Logging in / authentication remains out of scope."""
    agent = ba.create_browser_agent(_settings())
    assert agent.name == "browser_agent"
    tool_names = {getattr(t, "name", "") for t in agent.tools}
    assert tool_names == {
        "navigate",
        "read_page",
        "extract_text",
        "click",
        "type_into",
        "select_option",
        "submit_form",
        "screenshot",
        "escalate_to_user",
    }


def test_browser_agent_prompt_carries_read_only_rules() -> None:
    """System prompt must enforce the seven hard rules in spec."""
    text = ba.BROWSER_AGENT_INSTRUCTIONS
    # Rule 1: fill-but-never-submit / no credentials / no authentication.
    # The agent may now type into fields, but must not submit, log in, or
    # enter credentials.
    assert "credentials" in text
    assert "do NOT SUBMIT" in text or "FORBIDDEN" in text
    assert "type_into" in text
    # Rule 2: login wall escalation — three-step discipline
    # (screenshot + escalate_to_user + text reply, all required).
    # The literal "STOP" wording was dropped when rule 2 was rewritten
    # to mandate the three-step shape (issue #155 follow-on); we now
    # assert on the rule's load-bearing anti-pattern phrase, which
    # fits on one line of the reflowed prompt and so survives
    # line-wrap drift unlike "All three steps are required" (which
    # wraps mid-phrase at the prompt's ~70-col reflow).
    assert "login wall" in text
    assert "Do not stop after the screenshot" in text
    # Rule 3: budgets (15/75/8)
    assert "15 step" in text or "15 steps" in text
    assert "75 second" in text
    assert "8 screenshot" in text
    # Rule 4: deny-list at tool layer
    assert "disallowed_domain" in text
    assert "user_facing_message" in text
    # Rule 5: .gov / .edu disclosure
    assert ".gov" in text and ".edu" in text
    assert "soft_warn" in text or "government_site" in text
    # Rule 6: grounding
    assert "grounded" in text.lower() or "ground" in text.lower()
    assert "training data" in text
    # Rule 7: identity rule
    assert "Buildium IDs" in text
    # Tool list — all seven named in the prompt
    for tool_name in (
        "navigate",
        "read_page",
        "extract_text",
        "click",
        "type_into",
        "screenshot",
        "escalate_to_user",
    ):
        assert tool_name in text


def test_browser_agent_prompt_allows_fill_and_gated_submit_but_forbids_login() -> None:
    """The agent may fill fields (type_into) AND submit via the
    confirmation-gated ``submit_form``, but the prompt must still forbid
    logging in and entering credentials — login is the safety line now."""
    text = ba.BROWSER_AGENT_INSTRUCTIONS
    # Fill + gated submit are allowed and named.
    assert "type_into" in text
    assert "submit_form" in text
    assert "ALLOWED" in text
    # Login / credentials remain forbidden.
    assert "FORBIDDEN" in text
    assert "credentials" in text
    assert "password" in text.lower()
    # Escalation discipline is still spelled out.
    assert "escalate_to_user" in text


def test_browser_agent_prompt_has_escalation_as_success() -> None:
    """Critical framing per the spec: escalation is success, not
    failure, when the task can't be done read-only. Without this
    framing the model treats escalation as giving up and tries
    to bypass instead."""
    text = ba.BROWSER_AGENT_INSTRUCTIONS
    assert "Escalation is success" in text or "escalation is success" in text.lower()


def test_browser_agent_prompt_mandates_text_after_terminal_tool_call() -> None:
    """Issue #155 fix: the prompt must explicitly forbid ending a turn
    on a tool call alone. Without this rule, the Agents SDK leaves
    ``RunResult.final_output`` empty when the LLM's last action is
    ``escalate_to_user``/``screenshot``/etc. with no follow-up text,
    and the chat surface goes silent on the user. Catches prompt drift
    that would re-open #155."""
    text = ba.BROWSER_AGENT_INSTRUCTIONS
    assert "NEVER end a turn on a tool call alone" in text
    # Coverage beyond escalate_to_user — the rule must apply to other
    # tools that can hit terminal states. Test the underlying terminal
    # signals are named (line-wrap-tolerant; the prompt is reflowed
    # at 70 cols so we don't substring-check multi-word phrases).
    assert "budget_exhausted" in text
    # disallowed_domain is already asserted by the read-only-rules
    # test (rule 4 carries the same string); reasserting here makes
    # the dependency explicit: if rule 4 drops the verbatim mention
    # the new rule 8 still requires it.
    assert "disallowed_domain" in text


def test_looks_like_auth_gate_detection_heuristic() -> None:
    """Issue #155 case 061 fix (Shape 2a): the screenshot tool detects
    auth gates and writes a binding instruction into ``next_step``.
    Detection is gated by a two-signal threshold so a header link
    mentioning "log in" doesn't trip a false positive on a normal
    page. Pure string function, no mocks needed."""
    # Two or more signals → True. A typical sign-in form's accessibility
    # tree contains both a password textbox and a sign-in button, so
    # this is the load-bearing positive case.
    login_page = (
        "heading: County Permit Portal\n"
        "  textbox: Username\n"
        "  textbox: Password\n"
        "  button: Sign in\n"
        "  link: Forgot password?"
    )
    assert bt._looks_like_auth_gate(login_page) is True

    # Single signal → False. A normal page with a "Log in" link in the
    # header should NOT trip detection — that's the false-positive
    # the two-signal threshold exists to prevent.
    normal_page_with_login_link = (
        "heading: Welcome\n  link: About\n  link: Services\n  link: Log in\n  link: Contact"
    )
    assert bt._looks_like_auth_gate(normal_page_with_login_link) is False

    # Empty string → False (no signals, no detection).
    assert bt._looks_like_auth_gate("") is False

    # Unrelated content → False.
    zoning_page = (
        "heading: Zoning Board\n"
        "  link: May 2026 agenda\n"
        "  link: Public hearing schedule\n"
        "  link: Contact"
    )
    assert bt._looks_like_auth_gate(zoning_page) is False


# ── screenshot answer-not-present hint (issue #166) ──────────────


def _screenshot_settings() -> Settings:
    """Minimal Settings for ``_screenshot_impl`` tests. Mirrors the
    shape ``tests/unit/test_browser_tools.py`` uses; browser cap
    values are explicit so check_budget never trips during the
    one-step paths the hint tests exercise."""
    return Settings(
        env="test",
        llm={"default_model": "gemini/gemini-2.5-flash", "google_api_key": "g"},
        memory={"enabled": False},
        browser={
            "browserbase_api_key": "k",
            "browserbase_project_id": "p",
            "max_steps_per_task": 15,
            "max_wall_clock_seconds": 75,
            "max_screenshots_per_task": 8,
        },
    )


def _fake_session_for_screenshot(
    *,
    unmatched_read_count: int = 0,
    tree: Any = None,
) -> bs.BrowserSession:
    """Build a BrowserSession whose ``page.screenshot()`` returns bytes
    and ``page.accessibility.snapshot()`` returns the given tree dict.
    ``started_at`` is anchored to ``time.monotonic()`` so the
    wall-clock budget arm doesn't trip on the test's quick path
    (the gotcha that surfaced during PR #155's Win 3.1 read_page
    tests, now reverted but the lesson stands).

    The default tree models a bare landing page — no auth signals —
    so callers testing the answer-not-present branch don't
    accidentally trip the auth-gate branch."""
    if tree is None:
        tree = {
            "role": "heading",
            "name": "Zoning Board",
            "children": [
                {"role": "link", "name": "Past meetings", "children": []},
                {"role": "link", "name": "Resources", "children": []},
                {"role": "link", "name": "Contact", "children": []},
            ],
        }

    async def _snapshot() -> Any:
        return tree

    async def _capture_png(**_kwargs: Any) -> bytes:
        return b"\x89PNG\r\n\x1a\n"  # PNG magic; content irrelevant to the test

    page = MagicMock()
    page.url = "https://example-county.test/zoning"
    page.accessibility.snapshot = _snapshot
    page.screenshot = _capture_png

    return bs.BrowserSession(
        company_id="co-test",
        session_id="sess-test",
        started_at=time.monotonic(),
        page=page,
        unmatched_read_count=unmatched_read_count,
    )


def _patch_screenshot_io() -> tuple[Any, Any]:
    """Build the (upload, log_activity) patches the screenshot tests
    share: upload returns a fake (signed_url, expires_at) tuple;
    log_activity is a no-op so we don't need a real DB pool."""
    fake_upload = AsyncMock(
        return_value=(
            "https://example-supabase.test/storage/sign/x.png?token=t",
            datetime.now(UTC) + timedelta(hours=24),
        )
    )
    fake_log = AsyncMock()
    return fake_upload, fake_log


@pytest.mark.asyncio
async def test_screenshot_no_hint_when_unmatched_read_count_zero() -> None:
    """Issue #166 fix — negative case at the floor.

    ``unmatched_read_count = 0`` means the agent hasn't asked any
    questions yet (or every question matched). Screenshot should
    return cleanly with empty ``next_step`` — no escalation hint
    fires until the pattern of misses appears."""
    session = _fake_session_for_screenshot(unmatched_read_count=0)
    upload, log = _patch_screenshot_io()
    with (
        patch.object(bt, "current_session", return_value=session),
        patch.object(bt, "_upload_screenshot_to_storage", new=upload),
        patch.object(bt, "_log_activity", new=log),
    ):
        result = await bt._screenshot_impl(settings=_screenshot_settings())

    assert result.status == "ok"
    assert result.next_step == ""


@pytest.mark.asyncio
async def test_screenshot_no_hint_when_unmatched_read_count_one() -> None:
    """Issue #166 fix — negative case below the threshold.

    One unmatched read is exploration; the hint must NOT fire until
    the second unmatched read. Threshold rationale: avoid nagging
    the agent into early escalation after a single search-miss on
    the wrong subpage."""
    session = _fake_session_for_screenshot(unmatched_read_count=1)
    upload, log = _patch_screenshot_io()
    with (
        patch.object(bt, "current_session", return_value=session),
        patch.object(bt, "_upload_screenshot_to_storage", new=upload),
        patch.object(bt, "_log_activity", new=log),
    ):
        result = await bt._screenshot_impl(settings=_screenshot_settings())

    assert result.status == "ok"
    assert result.next_step == ""


@pytest.mark.asyncio
async def test_screenshot_emits_answer_not_present_hint_at_threshold() -> None:
    """Issue #166 fix — load-bearing positive case.

    ``unmatched_read_count = 2`` AND no auth signals on the page →
    ``next_step`` carries the answer-not-present escalation hint.
    This is the core regression-prevention assertion for case 063."""
    session = _fake_session_for_screenshot(unmatched_read_count=2)
    upload, log = _patch_screenshot_io()
    with (
        patch.object(bt, "current_session", return_value=session),
        patch.object(bt, "_upload_screenshot_to_storage", new=upload),
        patch.object(bt, "_log_activity", new=log),
    ):
        result = await bt._screenshot_impl(settings=_screenshot_settings())

    assert result.status == "ok"
    assert result.next_step != ""
    assert "without finding" in result.next_step
    assert "escalate_to_user" in result.next_step
    # The new hint must NOT use the auth-gate vocabulary — different
    # branch, different framing. If "sign-in" leaks into the
    # answer-not-present hint, the branches got tangled.
    assert "sign-in" not in result.next_step


@pytest.mark.asyncio
async def test_screenshot_auth_gate_wins_precedence_over_answer_not_present() -> None:
    """Issue #166 fix — precedence guard.

    When BOTH conditions fire (login page in the accessibility tree
    AND ``unmatched_read_count >= 2``), the auth-gate hint must win.
    Login pages are higher-confidence than the grinding heuristic,
    and the correct response to an auth wall is always
    escalate-with-auth-context regardless of read history.

    Regression catch: if a refactor flips the branch order or drops
    the ``if not next_step_hint`` guard, this test fails because the
    answer-not-present text will overwrite the auth-gate text."""
    login_tree = {
        "role": "heading",
        "name": "County Permit Portal",
        "children": [
            {"role": "textbox", "name": "Username", "children": []},
            {"role": "textbox", "name": "Password", "children": []},
            {"role": "button", "name": "Sign in", "children": []},
            {"role": "link", "name": "Forgot password?", "children": []},
        ],
    }
    session = _fake_session_for_screenshot(unmatched_read_count=2, tree=login_tree)
    upload, log = _patch_screenshot_io()
    with (
        patch.object(bt, "current_session", return_value=session),
        patch.object(bt, "_upload_screenshot_to_storage", new=upload),
        patch.object(bt, "_log_activity", new=log),
    ):
        result = await bt._screenshot_impl(settings=_screenshot_settings())

    assert result.status == "ok"
    assert result.next_step != ""
    assert "sign-in" in result.next_step
    assert "auth_required" in result.next_step
    # The answer-not-present vocabulary must NOT leak — the precedence
    # guard ensures the auth-gate branch is exclusive when it fires.
    assert "without finding" not in result.next_step


# ── click failure escalation hint (issue #167) ───────────────────


def _click_settings() -> Settings:
    """Minimal Settings for ``_click_impl`` tests. Same shape as
    ``_screenshot_settings()`` above."""
    return Settings(
        env="test",
        llm={"default_model": "gemini/gemini-2.5-flash", "google_api_key": "g"},
        memory={"enabled": False},
        browser={
            "browserbase_api_key": "k",
            "browserbase_project_id": "p",
            "max_steps_per_task": 15,
            "max_wall_clock_seconds": 75,
            "max_screenshots_per_task": 8,
        },
    )


def _fake_session_for_click(
    *,
    failed_click_count: int = 0,
    raise_on_click: bool = True,
) -> bs.BrowserSession:
    """Build a BrowserSession whose ``page.locator(...).first.click()``
    either raises (simulating Playwright's failure → ``navigation_failed``
    return path) or succeeds. ``failed_click_count`` seeds the prior
    count so we can drive the threshold-check assertions directly.

    ``started_at`` anchored to ``time.monotonic()`` so the wall-clock
    budget arm doesn't trip on the test's quick path."""

    class _RaisingLocator:
        @property
        def first(self) -> Any:
            return self

        async def click(self, **_kwargs: Any) -> None:
            if raise_on_click:
                raise RuntimeError("synthetic click failure")
            # Success path: silently return.

    page = MagicMock()
    page.locator = MagicMock(return_value=_RaisingLocator())

    return bs.BrowserSession(
        company_id="co-test",
        session_id="sess-test",
        started_at=time.monotonic(),
        page=page,
        failed_click_count=failed_click_count,
    )


@pytest.mark.asyncio
async def test_click_first_failure_no_hint_counter_goes_to_one() -> None:
    """Issue #167 — first failed click in a session.

    ``failed_click_count`` enters at 0 (no prior failures). Check is
    pre-increment, so 0 >= 2 is false → hint stays empty. After the
    return, the counter has advanced to 1 (recorded for the next
    failure)."""
    session = _fake_session_for_click(failed_click_count=0)
    with (
        patch.object(bt, "current_session", return_value=session),
        patch.object(bt, "_log_activity", new=AsyncMock()),
    ):
        result = await bt._click_impl("a.next-page", settings=_click_settings())

    assert result.status == "navigation_failed"
    assert result.next_step == ""
    assert session.failed_click_count == 1


@pytest.mark.asyncio
async def test_click_second_failure_no_hint_counter_goes_to_two() -> None:
    """Issue #167 — second failed click, still below threshold.

    ``failed_click_count`` enters at 1 (one prior failure). Check
    pre-increment: 1 >= 2 is false → hint empty. After return,
    counter is at 2 — the next failure WILL fire the hint."""
    session = _fake_session_for_click(failed_click_count=1)
    with (
        patch.object(bt, "current_session", return_value=session),
        patch.object(bt, "_log_activity", new=AsyncMock()),
    ):
        result = await bt._click_impl("a.next-page", settings=_click_settings())

    assert result.status == "navigation_failed"
    assert result.next_step == ""
    assert session.failed_click_count == 2


@pytest.mark.asyncio
async def test_click_third_failure_emits_hint_at_threshold() -> None:
    """Issue #167 — load-bearing positive case.

    ``failed_click_count`` enters at 2 (two prior failures). Check
    pre-increment: 2 >= 2 is true → hint fires. The agent has seen
    "two failures is a pattern; the third is grinding"; the tool
    layer pushes toward escalate."""
    session = _fake_session_for_click(failed_click_count=2)
    with (
        patch.object(bt, "current_session", return_value=session),
        patch.object(bt, "_log_activity", new=AsyncMock()),
    ):
        result = await bt._click_impl("a.next-page", settings=_click_settings())

    assert result.status == "navigation_failed"
    assert result.next_step != ""
    assert "click_failure" in result.next_step
    assert "escalate_to_user" in result.next_step
    assert session.failed_click_count == 3


@pytest.mark.asyncio
async def test_click_success_does_not_increment_failed_click_count() -> None:
    """Issue #167 — only failures count.

    A successful click must not advance ``failed_click_count`` — the
    counter is a failure-specific signal, not a general click counter.
    Seeded with a non-zero starting count to make the no-op visible."""
    session = _fake_session_for_click(failed_click_count=5, raise_on_click=False)
    with (
        patch.object(bt, "current_session", return_value=session),
        patch.object(bt, "_log_activity", new=AsyncMock()),
    ):
        result = await bt._click_impl("a.next-page", settings=_click_settings())

    assert result.status == "ok"
    assert result.next_step == ""
    assert session.failed_click_count == 5
