"""Per-task Browserbase + Playwright session lifecycle.

Mirrors the contextvar pattern :mod:`helixis.memory.context` uses
for company / user / session_id binding. The browser session lives
on a contextvar so the six browser_tools can pick it up without
threading it through every kwarg the LLM sees.

Lifecycle:

1. The orchestrator's per-run scope sets up an empty session slot
   (no Browserbase call yet — sessions are expensive, ~1-2 s to
   start, so we defer until first ``navigate``).
2. ``ensure_session()`` is called from the navigate tool. If no
   session exists on the contextvar AND credentials are configured,
   it creates one via the Browserbase SDK and a Playwright
   ``connect_over_cdp`` to the returned URL.
3. Subsequent tool calls (read_page / click / screenshot) read the
   session from the contextvar — they never create one. If the
   session is missing (navigate wasn't called first OR creds are
   missing), they return ``browser_unavailable``.
4. ``close_session()`` is called at end-of-run by the orchestrator's
   cleanup path. Best-effort: a failed close is logged, not raised.

Budget tracking lives on the session (step_count, screenshot_count,
started_at wall clock). Tools increment the relevant counter on each
successful call. When the budget tool helper sees an exceeded
threshold, it returns ``budget_exhausted`` to the agent.

Lazy imports: ``playwright`` and ``browserbase`` are in the
``[browser]`` optional extra, NOT in base deps. This module imports
them inside ``_create_session`` so the rest of the codebase loads
cleanly on a base install (CI, scripts that don't use browser_agent).
When either is missing, ``ensure_session`` returns ``None`` and the
tool layer surfaces ``browser_unavailable``.
"""

from __future__ import annotations

import contextlib
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from helixis.settings import Settings

logger = logging.getLogger(__name__)


# ── session data ────────────────────────────────────────────────


@dataclass
class BrowserSession:
    """Per-task state shared across the six browser_tools.

    ``playwright`` is the async Playwright context manager object;
    ``browser`` is the connected Browser; ``page`` is the active
    Page. All three are typed ``Any`` so this module can be
    imported on a base install without ``playwright`` available.

    ``step_count`` and ``screenshot_count`` are incremented by the
    tools themselves (not by Playwright callbacks) so the budget
    check is deterministic and testable without a live browser.
    ``unmatched_read_count`` is incremented by ``read_page`` when a
    non-empty query fails to match any line in the accessibility
    tree; the ``screenshot`` tool reads it to decide whether to emit
    the answer-not-present escalation hint (issue #166).
    ``failed_click_count`` is incremented by ``click`` on the
    ``navigation_failed`` path (Playwright raised); the ``click``
    tool itself reads it to decide whether to emit the
    click-failure escalation hint (issue #167). Only the
    ``navigation_failed`` path increments — the ``browser_unavailable``
    early-return runs without a session, and ``invalid_input`` /
    ``budget_exhausted`` are tracked elsewhere.

    ``company_id`` is captured at session creation so the screenshot
    upload path can scope every blob to the right tenant directory
    without re-reading the memory context on each call.
    """

    company_id: str
    session_id: str
    """Browserbase session ID. Used as the storage path discriminator."""

    started_at: float
    """Wall-clock anchor for the per-task budget. ``time.monotonic()``."""

    playwright_cm: Any = None
    """Playwright async context manager (closed by ``close_session``)."""

    browser: Any = None
    """Playwright Browser handle (closed alongside ``playwright_cm``)."""

    page: Any = None
    """Playwright Page — the active tab. All tools operate on this."""

    step_count: int = 0
    screenshot_count: int = 0
    unmatched_read_count: int = 0
    failed_click_count: int = 0

    last_read_signature: tuple[str, str] | None = None
    """``(url, text)`` of the most recent successful ``read_page``. Paired
    with ``identical_read_streak`` to detect an agent grinding re-reads of a
    page that provably lacks the answer (case 063)."""
    identical_read_streak: int = 0
    """How many ``read_page`` calls in a row returned the SAME ``(url, text)``.
    At ``_READ_REPEAT_ESCALATE_THRESHOLD`` the read result carries a
    ``next_step`` steering the agent to ``escalate_to_user`` instead of
    re-reading an unchanged page."""
    soft_warn_tags: list[str] = field(default_factory=list)
    """Accumulated ``soft_warn:*`` tags from the deny-list. Tools
    read this when building their final result so the agent sees
    every government / educational source it touched."""

    filled_fields: list[tuple[str, str]] = field(default_factory=list)
    """Ephemeral record of fields populated this task, as
    ``(selector, masked_value)`` pairs, used to build the ``submit_form``
    confirm card so the approving human sees WHAT is being filed.

    Values are MASKED at capture time (``browser_tools._mask_value``):
    the raw input is used only for the Playwright fill/select and is
    never retained here, so this list is safe to surface in the confirm
    preview and carries no raw form data into telemetry. The buffer is
    purely in-memory and per-task — it lives only for the session and is
    dropped on ``close_session``. Latest write per selector wins (a
    correction overwrites in place rather than appending a duplicate)."""


# ── contextvar binding ──────────────────────────────────────────


_session_var: ContextVar[BrowserSession | None] = ContextVar(
    "browser_session",
    default=None,
)


def current_session() -> BrowserSession | None:
    """Return the session bound to the current contextvar, or None."""
    return _session_var.get()


# ── lifecycle helpers ───────────────────────────────────────────


async def _create_browserbase_session(
    *,
    api_key: str,
    project_id: str,
) -> tuple[str, str] | None:
    """Create a Browserbase session and return (session_id, connect_url).

    Returns ``None`` when the ``browserbase`` SDK isn't installed
    (CI without ``[browser]`` extras) or when the API call fails.
    Lazy-imports the SDK so a base-install pytest pass doesn't need
    the package available.
    """
    try:
        from browserbase import Browserbase  # type: ignore[import-not-found]
    except ImportError:
        logger.info(
            "browserbase SDK not installed; browser_agent runs in degraded "
            "mode (every tool returns browser_unavailable). Install with "
            'pip install -e ".[browser]" to enable.',
        )
        return None

    try:
        client = Browserbase(api_key=api_key)
        session = client.sessions.create(project_id=project_id)
    except Exception as exc:  # noqa: BLE001 — Browserbase API surface
        logger.warning("Browserbase session creation failed: %s", exc)
        return None

    session_id = getattr(session, "id", None)
    connect_url = getattr(session, "connect_url", None) or getattr(
        session,
        "connectUrl",
        None,
    )
    if not session_id or not connect_url:
        logger.warning(
            "Browserbase session created but missing id/connect_url: %r",
            session,
        )
        return None
    return str(session_id), str(connect_url)


async def _connect_playwright(connect_url: str) -> tuple[Any, Any, Any] | None:
    """Connect Playwright to a Browserbase session via CDP. Returns
    (playwright_cm, browser, page) tuple on success; ``None`` on
    missing-library or transport failure."""
    try:
        from playwright.async_api import (  # type: ignore[import-not-found]
            async_playwright,
        )
    except ImportError:
        logger.info(
            "playwright not installed; browser_agent runs in degraded mode.",
        )
        return None

    try:
        pw_cm = async_playwright()
        pw = await pw_cm.__aenter__()
        browser = await pw.chromium.connect_over_cdp(connect_url)
        # First context is the default Browserbase context with the
        # default page. Reuse it instead of creating a new one to
        # avoid burning a step on context creation.
        contexts = browser.contexts
        if contexts and contexts[0].pages:
            page = contexts[0].pages[0]
        elif contexts:
            page = await contexts[0].new_page()
        else:
            context = await browser.new_context()
            page = await context.new_page()
        return pw_cm, browser, page
    except Exception as exc:  # noqa: BLE001 — playwright surface is broad
        logger.warning("Playwright CDP connect failed: %s", exc)
        # Best-effort cleanup of the half-initialized state.
        with contextlib.suppress(Exception):
            await pw_cm.__aexit__(None, None, None)  # type: ignore[possibly-unbound]
        return None


async def ensure_session(
    *,
    company_id: str,
    settings: Settings,
) -> BrowserSession | None:
    """Lazily create + bind a browser session for the current task.

    Returns the bound session on success. Returns ``None`` (after
    logging) when:
      * credentials aren't configured (api_key OR project_id blank)
      * the Browserbase SDK / Playwright aren't installed
      * the Browserbase session creation or Playwright connect fails

    Caller (the navigate tool) translates ``None`` into a
    ``browser_unavailable`` result for the agent.

    Idempotent — if a session is already bound, returns it.
    """
    existing = current_session()
    if existing is not None:
        return existing

    cfg = settings.browser
    if not cfg.browserbase_api_key or not cfg.browserbase_project_id:
        logger.info(
            "Browserbase credentials not configured "
            "(browserbase_api_key=%s, browserbase_project_id=%s); "
            "browser_agent in degraded mode.",
            "set" if cfg.browserbase_api_key else "unset",
            "set" if cfg.browserbase_project_id else "unset",
        )
        return None

    pair = await _create_browserbase_session(
        api_key=cfg.browserbase_api_key,
        project_id=cfg.browserbase_project_id,
    )
    if pair is None:
        return None
    session_id, connect_url = pair

    pw_triple = await _connect_playwright(connect_url)
    if pw_triple is None:
        # Browserbase session was created but Playwright couldn't
        # connect — leave it; Browserbase auto-terminates idle sessions.
        return None
    playwright_cm, browser, page = pw_triple

    session = BrowserSession(
        company_id=company_id,
        session_id=session_id,
        started_at=time.monotonic(),
        playwright_cm=playwright_cm,
        browser=browser,
        page=page,
    )
    _session_var.set(session)
    logger.info(
        "Browserbase session created for company=%s session_id=%s",
        company_id,
        session_id,
    )
    return session


async def close_session() -> None:
    """Close any session bound to the current contextvar. Best-effort
    — exceptions are logged, not raised. Called by the orchestrator
    at end-of-run."""
    session = current_session()
    if session is None:
        return
    try:
        if session.browser is not None:
            await session.browser.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Browser close failed for session=%s: %s", session.session_id, exc)
    try:
        if session.playwright_cm is not None:
            await session.playwright_cm.__aexit__(None, None, None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Playwright cleanup failed for session=%s: %s", session.session_id, exc)
    _session_var.set(None)
    logger.info("Browserbase session closed: %s", session.session_id)


# ── budget enforcement ─────────────────────────────────────────


@dataclass(frozen=True)
class BudgetCheck:
    """Result of asking the session "are you within budget?". When
    ``exhausted`` is True, the tool returns ``budget_exhausted`` to
    the agent and forces an escalation; the agent prompt teaches it
    to wrap up gracefully when this fires."""

    exhausted: bool
    limit: str | None = None
    """Which budget tripped: ``max_steps`` | ``max_wall_clock`` |
    ``max_screenshots``. ``None`` when ``exhausted`` is False."""

    current: int | float | None = None
    cap: int | float | None = None


def check_budget(
    session: BrowserSession,
    settings: Settings,
    *,
    also_screenshot: bool = False,
) -> BudgetCheck:
    """Check whether the session has any budget left.

    Called by each tool BEFORE doing work. The step counter is
    pre-incremented (the call about to happen counts), so a session
    with ``step_count == max_steps_per_task`` is over budget.

    ``also_screenshot`` flags the screenshot path — when True, we
    also check the screenshot counter against its own cap.
    """
    cfg = settings.browser
    elapsed = time.monotonic() - session.started_at
    if session.step_count >= cfg.max_steps_per_task:
        return BudgetCheck(
            exhausted=True,
            limit="max_steps",
            current=session.step_count,
            cap=cfg.max_steps_per_task,
        )
    if elapsed >= cfg.max_wall_clock_seconds:
        return BudgetCheck(
            exhausted=True,
            limit="max_wall_clock",
            current=round(elapsed, 1),
            cap=cfg.max_wall_clock_seconds,
        )
    if also_screenshot and session.screenshot_count >= cfg.max_screenshots_per_task:
        return BudgetCheck(
            exhausted=True,
            limit="max_screenshots",
            current=session.screenshot_count,
            cap=cfg.max_screenshots_per_task,
        )
    return BudgetCheck(exhausted=False)


__all__ = [
    "BrowserSession",
    "BudgetCheck",
    "check_budget",
    "close_session",
    "current_session",
    "ensure_session",
]
