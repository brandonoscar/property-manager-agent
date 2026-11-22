"""Browser_agent v1 — six read-only Browserbase + Playwright tools.

The orchestrator's browser specialist (:mod:`helixis.agents.browser_agent`)
calls these tools when a task requires navigating a real web page
that has no API and that no in-domain specialist covers.

Design rules (mirror :mod:`helixis.agents.web_tools` +
:mod:`helixis.agents.document_tools`):

* Six tools, each returns a typed Pydantic result, none raise into
  the agent loop. Every failure mode lands in ``warnings`` or in the
  ``status`` discriminator.
* Read-only. No form filling, no credential entry, no submit. v1
  explicitly omits ``type_into`` and ``submit_form`` — if a task
  needs them, the agent escalates instead.
* Deny-list (:mod:`helixis.agents.browser_denylist`) is enforced
  at the tool layer, not in the prompt. ``navigate`` runs the
  ``check_domain`` policy BEFORE creating a Browserbase session,
  so a hard-blocked URL never consumes session resources.
* Per-task budgets (steps, wall clock, screenshots) are enforced
  by :func:`helixis.agents.browser_session.check_budget`. When the
  budget trips, the tool returns ``budget_exhausted`` and the agent
  prompt teaches it to escalate.
* Activity log: every successful tool call writes one row to
  ``helixis_activity_log``, with descriptions passed through the shared
  identity/PII redactor (:mod:`helixis.security.pii`). Best-effort; never raises.
* Screenshots upload to Supabase Storage at
  ``browser_screenshots/{company_id}/{session_id}/{step}.png`` with
  a 24-hour signed URL. Uses the Supabase REST API via httpx — no
  new SDK dependency (PR #153 fork B2 decision).

Lazy Playwright imports live in :mod:`helixis.agents.browser_session`
so this module loads cleanly on a base install without ``[browser]``
extras. Tools that need Playwright check via ``current_session()``
and return ``browser_unavailable`` when it's missing.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from agents import function_tool
from pydantic import BaseModel, Field

from helixis.agents.browser_denylist import check_domain, user_facing_message
from helixis.agents.browser_session import (
    check_budget,
    current_session,
    ensure_session,
)
from helixis.agents.idempotency import SUCCESS_OUTCOMES, derive_key, pending_reply, run_guarded
from helixis.db import engine as db_engine
from helixis.db.repositories.activity import ActivityEntry, write_activity
from helixis.memory.context import current_memory_context
from helixis.security.pii import redact
from helixis.security.prompt_delimit import wrap_untrusted
from helixis.settings import Settings, get_settings

logger = logging.getLogger(__name__)


# Identity/PII redaction is shared across tools — see helixis.security.pii.


_NAVIGATION_TIMEOUT_SECONDS = 20.0
_NAVIGATION_RETRY_BACKOFF = 0.5
_ACCESSIBILITY_TEXT_MAX_CHARS = 8000
_QUERY_MAX_CHARS = 200
# After this many consecutive reads returning the SAME (url, text), read_page
# stops staying silent and steers the agent to escalate — the page is not going
# to reveal the answer on another read (case 063). 3 allows one defensive
# re-read before the nudge, and keeps the read_page count well under the eval's
# ``read_page <= 5`` safety cap. Unlike PR #155's regressed hint, this fires
# only on genuine repetition and points at escalate_to_user, never screenshot.
_READ_REPEAT_ESCALATE_THRESHOLD = 3
# Body-text fallback read timeout (#236): bounded so a hung inner_text("body")
# can't stall a read when the a11y snapshot is already unusable. See
# ``_read_page_lines``.
_BODY_TEXT_READ_TIMEOUT_MS = 5000


BrowserStatus = Literal[
    "ok",
    "browser_unavailable",
    "disallowed_domain",
    "navigation_failed",
    "budget_exhausted",
    "page_not_loaded",
    "extraction_failed",
    "screenshot_failed",
    "invalid_input",
    # submit_form outcomes (PR: gated submit). "submitted" = the gated
    # submit fired after approval; "not_submitted" = declined/timed-out at
    # the confirm gate, nothing was sent.
    "submitted",
    "not_submitted",
]


# ── data models ──────────────────────────────────────────────────


class BrowserNavigateResult(BaseModel):
    status: BrowserStatus
    url: str = ""
    final_url: str = ""
    deny_category: str | None = None
    deny_reason: str | None = None
    user_facing_message: str = ""
    step_count: int = 0
    warnings: list[str] = Field(default_factory=list)


class BrowserReadResult(BaseModel):
    status: BrowserStatus
    url: str = ""
    text: str = ""
    truncated: bool = False
    query: str = ""
    step_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    # Set only when the agent has re-read the SAME page enough times that the
    # answer is provably not there — a binding instruction to escalate rather
    # than grind (case 063). Empty on a first/changed read.
    next_step: str = ""


class BrowserExtractResult(BaseModel):
    status: BrowserStatus
    selector_or_role: str = ""
    text: str = ""
    step_count: int = 0
    warnings: list[str] = Field(default_factory=list)


class BrowserClickResult(BaseModel):
    status: BrowserStatus
    selector_or_role: str = ""
    step_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    next_step: str = ""


class BrowserTypeResult(BaseModel):
    status: BrowserStatus
    selector_or_role: str = ""
    # The number of characters typed — NOT the value itself. Form fields
    # routinely hold sensitive data (names, case numbers, account numbers),
    # so the typed text is never echoed back to the model or the logs.
    char_count: int = 0
    step_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    next_step: str = ""


class BrowserScreenshotResult(BaseModel):
    status: BrowserStatus
    signed_url: str = ""
    expires_at: datetime | None = None
    step_count: int = 0
    screenshot_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    next_step: str = ""


class BrowserEscalateResult(BaseModel):
    status: Literal["escalated"] = "escalated"
    reason: str = ""
    last_screenshot_url: str | None = None
    warnings: list[str] = Field(default_factory=list)


class BrowserSelectResult(BaseModel):
    status: BrowserStatus
    selector_or_role: str = ""
    # The option label/value is sensitive form data — like type_into, we
    # record only the field selector + the length of the chosen value, never
    # the value itself.
    value_count: int = 0
    step_count: int = 0
    warnings: list[str] = Field(default_factory=list)


class BrowserSubmitResult(BaseModel):
    status: BrowserStatus
    selector_or_role: str = ""
    step_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    # Cut 2 — read-back evidence so the agent can SHOW the user what it saw
    # instead of "trust me". ``final_url`` is the page the submit landed on;
    # ``confirmation_detail`` is the matched success/error signal snippet (a
    # short page phrase, never raw form-field PII).
    final_url: str = ""
    confirmation_detail: str = ""


# ── helpers ──────────────────────────────────────────────────────


def _sanitize_for_log(text: str, limit: int = 200) -> str:
    return redact(text)[:limit]


def _mask_value(value: str) -> str:
    """Mask a filled form value for the submit confirm preview.

    Reveals only the first and last character and replaces the interior
    with a fixed-width bullet run — the run is fixed-width on purpose so
    the value's *length* doesn't leak either. Values of two characters or
    fewer are fully masked. The approving human still gets a recognizable
    per-field hint (the field label plus first/last char), but the raw
    value never reaches the confirm card, the SSE payload, or the logs.

    This is the single tunable for confirm-card masking strength: a future
    product decision (issue: confirm-card content) can widen the reveal
    here without touching the gate wiring.
    """
    v = (value or "").strip()
    if len(v) <= 2:
        return "•" * len(v)
    return f"{v[0]}•••{v[-1]}"


def _record_filled_field(session: Any, selector_or_role: str, raw_value: str) -> None:
    """Record a populated field on the session as ``(selector, masked)``.

    Masks ``raw_value`` immediately via :func:`_mask_value` so the session
    never retains raw form data. Idempotent per selector — a correction to
    a field already filled overwrites in place (latest value wins) instead
    of appending a duplicate row to the confirm card.
    """
    masked = _mask_value(raw_value)
    for idx, (sel, _existing) in enumerate(session.filled_fields):
        if sel == selector_or_role:
            session.filled_fields[idx] = (selector_or_role, masked)
            return
    session.filled_fields.append((selector_or_role, masked))


def _budget_warning(check: Any) -> str:
    """Format a BudgetCheck into a short warning string for activity logs."""
    return f"budget_exhausted:{check.limit}={check.current}/{check.cap}"


def _flatten_accessibility_tree(node: Any, depth: int = 0) -> list[str]:
    """Flatten a Playwright ``page.accessibility.snapshot()`` dict into
    a list of ``"  role: name"`` strings, indented by depth. Empty
    names are dropped. Recursion-bounded by Playwright's own limits
    on tree depth (typically < 100)."""
    if not isinstance(node, dict):
        return []
    role = str(node.get("role") or "")
    name = str(node.get("name") or "").strip()
    out: list[str] = []
    if role and name:
        out.append("  " * depth + f"{role}: {name}")
    elif name:
        out.append("  " * depth + name)
    children = node.get("children") or []
    for child in children:
        out.extend(_flatten_accessibility_tree(child, depth + 1))
    return out


_AUTH_GATE_SIGNALS = (
    "sign in",
    "log in",
    "login",
    "sign-in",
    "log-in",
    "password",
    "email address",
    "continue with google",
    "continue with apple",
    "create account",
    "forgot password",
    "two-factor",
    "verification code",
)


def _looks_like_auth_gate(flattened_tree: str) -> bool:
    """Heuristic: does this accessibility tree look like a login/auth page?

    Returns True if at least two distinct auth-gate signal phrases appear.
    Two-signal threshold avoids false positives on pages that mention
    'log in' in a header link but aren't actually login walls.
    """
    if not flattened_tree:
        return False
    lowered = flattened_tree.lower()
    hits = sum(1 for signal in _AUTH_GATE_SIGNALS if signal in lowered)
    return hits >= 2


async def _read_page_lines(
    page: Any, *, body_timeout_ms: int = _BODY_TEXT_READ_TIMEOUT_MS
) -> tuple[list[str], str]:
    """Read the page's text as flattened lines, hardened against a flaky
    accessibility snapshot (the #236/#237 read-layer fragility).

    Primary read is ``accessibility.snapshot()`` flattened to ``role: name``
    lines. On real Browserbase DOM that call can raise (observed:
    ``AttributeError``) even when the page is fully rendered, so when the
    snapshot is *unusable* — it raised, or returned ``None`` — we fall back to
    the page's visible body text (``inner_text("body")``). This is the same
    fallback that hardened submit-verification in #237, generalized so every
    read path (``read_page``, the screenshot auth-gate detector, submit verify)
    shares one resilient primitive.

    Returns ``(lines, source)``. ``source`` is:
      ``"a11y"``       — lines came from the accessibility tree (may be empty
                         for a genuinely nameless page);
      ``"body"``       — the snapshot was unusable; lines came from body text;
      ``"empty"``      — no usable text, but the page was *readable* (snapshot
                         returned ``None`` / empty body, not an error);
      ``"unreadable"`` — BOTH reads errored (snapshot raised AND body read
                         threw) → genuinely unreadable.

    Never raises; callers decide how each ``source`` maps to their status.
    """
    a11y_ok = False
    tree: Any = None
    try:
        tree = await page.accessibility.snapshot()
        a11y_ok = True
    except Exception as exc:  # noqa: BLE001 — fall back to body text
        logger.info("a11y snapshot raised (%s); falling back to body text", type(exc).__name__)

    if a11y_ok and tree is not None:
        return _flatten_accessibility_tree(tree), "a11y"

    # Snapshot unusable (raised, or returned None) → visible body text.
    body = ""
    body_threw = False
    try:
        body = await page.inner_text("body", timeout=body_timeout_ms)
    except Exception as exc:  # noqa: BLE001
        logger.info("body inner_text fallback failed (%s)", type(exc).__name__)
        body_threw = True

    if body and body.strip():
        return [ln for ln in body.splitlines() if ln.strip()], "body"

    # No usable text. Genuinely unreadable only if BOTH reads errored;
    # a clean-but-blank page (snapshot None + empty body) is just "empty".
    return [], ("unreadable" if (not a11y_ok and body_threw) else "empty")


# ── Cut 2 submit-verification signals (tunable in ONE place) ─────────────────
#
# Post-submit page classification. Fail-safe: only a POSITIVE signal counts as
# "the filing landed"; a NEGATIVE signal, or no confirmable signal at all, is
# never read as success. These widen per court site without touching the gate
# wiring. Real-court tuning happens against a live page in issue #217.

# Error / rejection phrases on a post-submit page → the submit did NOT land.
_SUBMIT_FAILURE_SIGNALS = (
    "error",
    "required field",
    "is required",
    "could not be processed",
    "was not submitted",
    "was not saved",
    "please try again",
    "please correct",
    "invalid",
    "failed",
)

# URL path fragments that indicate the submit landed on a confirmation page.
_SUBMIT_SUCCESS_URL_FRAGMENTS = (
    "/confirmation",
    "/confirm",
    "/success",
    "/receipt",
    "/filed",
    "/complete",
)


def _classify_submit_outcome(
    *,
    flattened_tree: str,
    final_url: str,
    starting_url: str,
    expected_confirmation: str,
) -> tuple[bool | None, str]:
    """Classify a post-submit page. Returns (verdict, detail).

    verdict:
      * True  → POSITIVE confirmation (caller's expected phrase present, or the
        URL moved to a confirmation-shaped path AND no failure signal).
      * False → NEGATIVE: an explicit failure signal is present, OR the URL
        didn't move and nothing confirms success, OR no caller signal to
        confirm against (absence of a confirmable signal is NOT success).
      * None  → caller passed no signal but the page is otherwise clean — left
        to the caller's fallback policy (treated as not-confirmed by the
        verifier, surfaced honestly).

    ``detail`` is a short, PII-free snippet describing the matched signal.
    """
    lowered = (flattened_tree or "").lower()
    url_lower = (final_url or "").lower()

    # NEGATIVE first — an explicit error/rejection beats any weak positive.
    for sig in _SUBMIT_FAILURE_SIGNALS:
        if sig in lowered:
            return False, f"page shows '{sig}' — submission not confirmed"

    # POSITIVE: caller-supplied expected phrase present.
    exp = (expected_confirmation or "").strip().lower()
    if exp and exp in lowered:
        return True, f"page shows expected confirmation '{expected_confirmation.strip()}'"

    # POSITIVE: URL moved to a confirmation-shaped path.
    moved = bool(final_url) and final_url != starting_url
    if moved and any(frag in url_lower for frag in _SUBMIT_SUCCESS_URL_FRAGMENTS):
        return True, "navigated to a confirmation page"

    # No positive signal. If the caller gave an expected phrase and we never
    # saw it, that's a NEGATIVE (we looked and couldn't confirm). If the caller
    # gave NO signal, we have nothing to confirm against → None (not success).
    if exp:
        return False, "expected confirmation not found on the resulting page"
    return None, "no confirmation signal found on the resulting page"


def _filter_by_query(lines: list[str], query: str) -> list[str]:
    """When the agent passes a query, keep lines that contain it (case-
    insensitive) plus a small window of surrounding lines for context.
    Empty query → return all lines unfiltered."""
    q = (query or "").strip().lower()
    if not q:
        return lines
    hits: set[int] = set()
    for i, line in enumerate(lines):
        if q in line.lower():
            for j in range(max(0, i - 1), min(len(lines), i + 2)):
                hits.add(j)
    if not hits:
        return lines  # No matches — return everything so agent can see what's there
    return [lines[i] for i in sorted(hits)]


def _truncate(text: str, limit: int = _ACCESSIBILITY_TEXT_MAX_CHARS) -> tuple[str, bool]:
    """Truncate ``text`` to ``limit`` chars, returning (text, truncated)."""
    if len(text) <= limit:
        return text, False
    return text[: limit - 1] + "…", True


async def _log_activity(
    *,
    action_type: str,
    description: str,
    raw: dict[str, Any],
) -> None:
    """Best-effort activity-log write. Mirrors web_tools / document_tools."""
    try:
        pool = db_engine.get_pool()
    except RuntimeError:
        return
    ctx = current_memory_context()
    company_id = ctx.company_id if ctx else ""
    user_id = ctx.user_id if ctx else None
    if not company_id:
        return
    entry = ActivityEntry(
        account_id=None,
        company_id=company_id,
        source="chat",
        action_type=action_type,
        description=_sanitize_for_log(description),
        user_id=user_id or None,
        raw=raw,
    )
    await write_activity(pool, entry)


# ── Supabase Storage upload (httpx + REST) ──────────────────────


async def _upload_screenshot_to_storage(
    *,
    png_bytes: bytes,
    company_id: str,
    session_id: str,
    step: int,
    settings: Settings,
) -> tuple[str, datetime] | None:
    """Upload a PNG to Supabase Storage and return a 24-hour signed URL.

    Path shape: ``browser_screenshots/{company_id}/{session_id}/{step}.png``.
    Per-tenant scoping is enforced by the path; bucket-level RLS is a
    separate Supabase config concern (called out in the PR description
    as a follow-up).

    Two REST round trips:
      1. ``POST /storage/v1/object/{bucket}/{path}`` to upload
      2. ``POST /storage/v1/object/sign/{bucket}/{path}`` for the URL

    Returns ``None`` on any failure (missing Supabase URL, transport
    error, non-2xx response). Callers translate ``None`` to
    ``screenshot_failed`` for the agent.
    """
    supabase_url = (settings.supabase.url or "").rstrip("/")
    service_key = settings.supabase.service_key or ""
    if not supabase_url or not service_key:
        logger.warning("Supabase Storage upload skipped: URL or service key missing")
        return None

    bucket = "browser_screenshots"
    path = f"{company_id}/{session_id}/{step}.png"
    upload_url = f"{supabase_url}/storage/v1/object/{bucket}/{path}"
    sign_url = f"{supabase_url}/storage/v1/object/sign/{bucket}/{path}"

    headers = {
        "Authorization": f"Bearer {service_key}",
        "apikey": service_key,
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            put_resp = await client.post(
                upload_url,
                content=png_bytes,
                headers={**headers, "Content-Type": "image/png", "x-upsert": "true"},
            )
            if put_resp.status_code >= 400:
                logger.warning(
                    "Supabase Storage upload failed status=%d body=%s",
                    put_resp.status_code,
                    put_resp.text[:200],
                )
                return None

            sign_resp = await client.post(
                sign_url,
                json={"expiresIn": 86400},
                headers=headers,
            )
            if sign_resp.status_code >= 400:
                logger.warning(
                    "Supabase Storage sign failed status=%d body=%s",
                    sign_resp.status_code,
                    sign_resp.text[:200],
                )
                return None
            data = sign_resp.json()
    except (httpx.TimeoutException, httpx.TransportError, ValueError) as exc:
        logger.warning("Supabase Storage transport failure: %s", exc)
        return None

    signed_path = data.get("signedURL") or data.get("signedUrl") or ""
    if not signed_path:
        logger.warning("Supabase Storage sign response missing signedURL: %r", data)
        return None
    # The sign response returns a path like ``/object/sign/bucket/path?token=...``;
    # prepend the storage URL for a fully-qualified link the user can click.
    full_url = (
        signed_path if signed_path.startswith("http") else f"{supabase_url}/storage/v1{signed_path}"
    )
    expires_at = datetime.now(UTC) + timedelta(hours=24)
    return full_url, expires_at


# ── tool 1: navigate ─────────────────────────────────────────────


async def _navigate_impl(
    url: str,
    *,
    settings: Settings | None = None,
) -> BrowserNavigateResult:
    """Navigate the browser to ``url``. Creates a Browserbase session
    on first call. Enforces the deny-list BEFORE creating the session
    (a hard-blocked URL never consumes Browserbase resources)."""
    settings = settings or get_settings()

    if not isinstance(url, str) or not url.strip():
        return BrowserNavigateResult(
            status="invalid_input",
            url=str(url),
            warnings=["invalid_input: url must be a non-empty string"],
        )
    url = url.strip()

    # Deny-list check FIRST — before session creation.
    deny = check_domain(url)
    if deny.category in ("hard_blocked", "soft_blocked"):
        host = url
        try:
            from urllib.parse import urlparse

            host = urlparse(url).hostname or url
        except Exception:  # noqa: BLE001
            pass
        msg = user_facing_message(deny, str(host))
        await _log_activity(
            action_type="browser_blocked",
            description=f"navigate to {host} blocked: {deny.reason}",
            raw={
                "url": url,
                "deny_category": deny.category,
                "deny_reason": deny.reason,
            },
        )
        return BrowserNavigateResult(
            status="disallowed_domain",
            url=url,
            deny_category=deny.category,
            deny_reason=deny.reason,
            user_facing_message=msg,
            warnings=[f"disallowed_domain:{deny.reason or deny.category}"],
        )

    # Ensure session — returns None if creds missing or libs unavailable.
    ctx = current_memory_context()
    company_id = ctx.company_id if ctx else ""
    session = await ensure_session(company_id=company_id, settings=settings)
    if session is None:
        return BrowserNavigateResult(
            status="browser_unavailable",
            url=url,
            warnings=["browser_unavailable"],
        )

    # Soft-warn tags ride along with the result.
    if deny.category == "soft_warn" and deny.warning_tag:
        if deny.warning_tag not in session.soft_warn_tags:
            session.soft_warn_tags.append(deny.warning_tag)

    # Budget check + increment step.
    session.step_count += 1
    budget = check_budget(session, settings)
    if budget.exhausted:
        return BrowserNavigateResult(
            status="budget_exhausted",
            url=url,
            step_count=session.step_count,
            warnings=[_budget_warning(budget)],
        )

    # Navigation with one retry on transport failure.
    final_url = ""
    for attempt in (0, 1):
        try:
            await asyncio.wait_for(
                session.page.goto(url, wait_until="domcontentloaded"),
                timeout=_NAVIGATION_TIMEOUT_SECONDS,
            )
            final_url = str(session.page.url)
            break
        except TimeoutError as exc:
            logger.warning("navigate timeout attempt=%d url=%s: %s", attempt, url, exc)
            if attempt == 0:
                await asyncio.sleep(_NAVIGATION_RETRY_BACKOFF)
                continue
            return BrowserNavigateResult(
                status="navigation_failed",
                url=url,
                step_count=session.step_count,
                warnings=["navigation_failed:timeout"],
            )
        except Exception as exc:  # noqa: BLE001 — Playwright errors are broad
            logger.warning("navigate failed attempt=%d url=%s: %s", attempt, url, exc)
            if attempt == 0:
                await asyncio.sleep(_NAVIGATION_RETRY_BACKOFF)
                continue
            return BrowserNavigateResult(
                status="navigation_failed",
                url=url,
                step_count=session.step_count,
                warnings=[f"navigation_failed:{type(exc).__name__}"],
            )

    warnings: list[str] = []
    if deny.warning_tag:
        warnings.append(deny.warning_tag)

    await _log_activity(
        action_type="browser_navigate",
        description=f"navigated to {final_url or url}",
        raw={
            "url": url,
            "final_url": final_url,
            "step_count": session.step_count,
            "soft_warn_tags": list(session.soft_warn_tags),
        },
    )
    return BrowserNavigateResult(
        status="ok",
        url=url,
        final_url=final_url,
        deny_category=deny.category,
        step_count=session.step_count,
        warnings=warnings,
    )


@function_tool
async def navigate(url: str) -> str:
    """Navigate the browser to a URL. Read-only — does NOT submit
    forms, log in, or click anything. Returns the page's accessibility
    state for subsequent ``read_page`` / ``extract_text`` calls.

    Args:
        url: Full URL including scheme (https://...).

    Returns:
        JSON-encoded :class:`BrowserNavigateResult`. When ``status``
        is ``disallowed_domain``, ``user_facing_message`` carries the
        text to relay to the user verbatim.
    """
    result = await _navigate_impl(url)
    return result.model_dump_json()


# ── tool 2: read_page ────────────────────────────────────────────


async def _read_page_impl(
    query: str = "",
    *,
    settings: Settings | None = None,
) -> BrowserReadResult:
    """Read the current page's accessibility tree, optionally filtered
    by a query substring. Returns the flattened text the agent can
    reason over. No clicking, no navigation."""
    settings = settings or get_settings()
    session = current_session()
    if session is None:
        return BrowserReadResult(
            status="browser_unavailable",
            warnings=["browser_unavailable"],
        )

    query = (query or "")[:_QUERY_MAX_CHARS]

    session.step_count += 1
    budget = check_budget(session, settings)
    if budget.exhausted:
        return BrowserReadResult(
            status="budget_exhausted",
            query=query,
            step_count=session.step_count,
            warnings=[_budget_warning(budget)],
        )

    page_url = ""
    try:
        page_url = str(session.page.url)
    except Exception as exc:  # noqa: BLE001 — a URL read shouldn't sink the whole read
        logger.debug("read_page url read failed: %s", exc)

    # Hardened read: a11y snapshot with a body-text fallback (#236/#237).
    lines, source = await _read_page_lines(session.page)
    if source == "unreadable":
        # Both the a11y snapshot and the body read errored → truly unreadable.
        return BrowserReadResult(
            status="extraction_failed",
            query=query,
            url=page_url,
            step_count=session.step_count,
            warnings=["extraction_failed:page_unreadable"],
        )
    if source == "empty":
        return BrowserReadResult(
            status="page_not_loaded",
            query=query,
            url=page_url,
            step_count=session.step_count,
            warnings=["page_not_loaded:accessibility_snapshot_empty"],
        )

    filtered = _filter_by_query(lines, query)
    text, truncated = _truncate("\n".join(filtered))
    # Redact contact PII (emails / phones) from what the MODEL reads, not just
    # the logs (``_sanitize_for_log``). The browser agent must never surface a
    # person's raw contact details (prompt rule 8); stripping them at the source
    # means the model can't echo them even incidentally (case 067). Answers that
    # aren't contacts — a fee, hours, a license status — are untouched.
    text = redact(text)

    # Track "agent asked a question but the page didn't have the answer"
    # for the screenshot tool's escalation hint (issue #166).
    # ``_filter_by_query`` returns all lines under two conditions: empty
    # query OR non-empty query with zero matches. Disambiguate by
    # checking the query. The result value itself is NOT modified —
    # the agent only learns about the count when it later reaches for
    # ``screenshot`` (see ``_screenshot_impl``'s answer-not-present
    # branch). The Win 3.1 attempt on PR #155 made ``read_page`` itself
    # emit a hint and regressed case 063 from 15/20 to 11/20 by
    # nudging the agent toward screenshot-as-confirmation; this fix
    # keeps ``read_page`` silent and steers only at the terminal step.
    if bool(query) and len(filtered) == len(lines):
        session.unmatched_read_count += 1

    # Grind guard (case 063): re-reading the SAME page returns the SAME content
    # — it will never surface an answer that isn't there. Track the streak of
    # identical (url, text) reads and, once it crosses the threshold, hand the
    # agent a binding ``next_step`` to escalate instead of burning more reads.
    # Keyed on (url, text) so a query-refined or genuinely-changed read resets
    # the streak — a page that DOES carry the answer is answered on the first
    # read and never trips this (contrast 065). The steer points at
    # escalate_to_user, never screenshot (PR #155 regressed 063 by nudging
    # toward screenshot-as-confirmation).
    read_signature = (page_url, text)
    if session.last_read_signature == read_signature:
        session.identical_read_streak += 1
    else:
        session.last_read_signature = read_signature
        session.identical_read_streak = 1
    next_step = ""
    if session.identical_read_streak >= _READ_REPEAT_ESCALATE_THRESHOLD:
        next_step = (
            f"You have read this same page {session.identical_read_streak} times "
            "and its content has not changed — the information the user asked for "
            "is not present here. Do NOT read it again. Your next action MUST be "
            "escalate_to_user with a brief explanation that you could not find the "
            "requested information on the page(s) you reviewed."
        )

    await _log_activity(
        action_type="browser_read_page",
        description=f"read page {page_url} (query={query!r})",
        raw={
            "url": page_url,
            "query": query,
            "char_count": len(text),
            "truncated": truncated,
            "step_count": session.step_count,
            "unmatched_read_count": session.unmatched_read_count,
            "identical_read_streak": session.identical_read_streak,
        },
    )
    warnings = list(session.soft_warn_tags)
    if source == "body":
        # The a11y snapshot was unusable; this read came from visible body text
        # (role/name structure is absent). Surface it so the fallback is observable.
        warnings.append("read_via_body_text_fallback")
    return BrowserReadResult(
        status="ok",
        url=page_url,
        text=text,
        truncated=truncated,
        query=query,
        step_count=session.step_count,
        warnings=warnings,
        next_step=next_step,
    )


@function_tool
async def read_page(query: str = "") -> str:
    """Read the current page's accessibility tree. Returns text the
    agent can reason over — flattened role+name lines, optionally
    filtered by a query substring.

    Args:
        query: Optional substring to filter by (case-insensitive).
            Leave empty to get the whole page.

    Returns:
        JSON-encoded :class:`BrowserReadResult`. When ``text`` is
        empty or the page hasn't loaded, surface that to the user.
    """
    result = await _read_page_impl(query)
    # Fence the page text as untrusted before the agent reasons over it
    # (risk #4 — indirect prompt injection from web content). Internal callers
    # use ``_read_page_impl`` directly and are unaffected.
    if result.text:
        result = result.model_copy(
            update={"text": wrap_untrusted(result.text, label="web page content")}
        )
    return result.model_dump_json()


# ── tool 3: extract_text ─────────────────────────────────────────


async def _extract_text_impl(
    selector_or_role: str,
    *,
    settings: Settings | None = None,
) -> BrowserExtractResult:
    """Extract text from a specific element by CSS selector or by
    accessibility role:name. Used for "pull the table cell I saw" or
    "grab the price tag" follow-ups after ``read_page`` revealed the
    structure."""
    settings = settings or get_settings()
    session = current_session()
    if session is None:
        return BrowserExtractResult(
            status="browser_unavailable",
            selector_or_role=str(selector_or_role),
            warnings=["browser_unavailable"],
        )

    if not isinstance(selector_or_role, str) or not selector_or_role.strip():
        return BrowserExtractResult(
            status="invalid_input",
            selector_or_role=str(selector_or_role),
            warnings=["invalid_input: selector_or_role must be a non-empty string"],
        )
    selector_or_role = selector_or_role.strip()

    session.step_count += 1
    budget = check_budget(session, settings)
    if budget.exhausted:
        return BrowserExtractResult(
            status="budget_exhausted",
            selector_or_role=selector_or_role,
            step_count=session.step_count,
            warnings=[_budget_warning(budget)],
        )

    try:
        # CSS selector path. Playwright also supports role= queries
        # via ``page.get_by_role()``; we accept either by trying CSS
        # first and falling back to inner_text on the locator.
        text = await session.page.locator(selector_or_role).first.inner_text(
            timeout=5000,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "extract_text failed selector=%s: %s",
            selector_or_role,
            exc,
        )
        return BrowserExtractResult(
            status="extraction_failed",
            selector_or_role=selector_or_role,
            step_count=session.step_count,
            warnings=[f"extraction_failed:{type(exc).__name__}"],
        )

    text, _ = _truncate(text or "", limit=2000)
    # Same contact-PII redaction as read_page — the model must never receive a
    # raw email/phone it could echo (prompt rule 8).
    text = redact(text)
    await _log_activity(
        action_type="browser_extract_text",
        description=f"extracted from {selector_or_role}",
        raw={
            "selector_or_role": selector_or_role,
            "char_count": len(text),
            "step_count": session.step_count,
        },
    )
    return BrowserExtractResult(
        status="ok",
        selector_or_role=selector_or_role,
        text=text,
        step_count=session.step_count,
        warnings=list(session.soft_warn_tags),
    )


@function_tool
async def extract_text(selector_or_role: str) -> str:
    """Extract text from a specific element by CSS selector.

    Args:
        selector_or_role: CSS selector targeting the element.

    Returns:
        JSON-encoded :class:`BrowserExtractResult`.
    """
    result = await _extract_text_impl(selector_or_role)
    if result.text:
        result = result.model_copy(
            update={"text": wrap_untrusted(result.text, label="web page content")}
        )
    return result.model_dump_json()


# ── tool 4: click ────────────────────────────────────────────────


async def _click_impl(
    selector_or_role: str,
    *,
    settings: Settings | None = None,
) -> BrowserClickResult:
    """Click a navigational element. v1 read-only contract: the agent
    prompt forbids clicking anything with legal/financial weight
    (forms, submits, payments). The deny-list does not enforce this —
    it's the prompt's responsibility, with escalation as the safety
    net when the agent encounters such a control."""
    settings = settings or get_settings()
    session = current_session()
    if session is None:
        return BrowserClickResult(
            status="browser_unavailable",
            selector_or_role=str(selector_or_role),
            warnings=["browser_unavailable"],
        )

    if not isinstance(selector_or_role, str) or not selector_or_role.strip():
        return BrowserClickResult(
            status="invalid_input",
            selector_or_role=str(selector_or_role),
            warnings=["invalid_input: selector_or_role must be a non-empty string"],
        )
    selector_or_role = selector_or_role.strip()

    session.step_count += 1
    budget = check_budget(session, settings)
    if budget.exhausted:
        return BrowserClickResult(
            status="budget_exhausted",
            selector_or_role=selector_or_role,
            step_count=session.step_count,
            warnings=[_budget_warning(budget)],
        )

    try:
        await session.page.locator(selector_or_role).first.click(timeout=5000)
    except Exception as exc:  # noqa: BLE001
        logger.warning("click failed selector=%s: %s", selector_or_role, exc)
        # Issue #167: emit a click-failure escalation hint when the
        # agent has ALREADY seen >= 2 prior failures (the check is
        # pre-increment). Two prior failures is a pattern; the third
        # attempt is grinding. The browser_unavailable early-return
        # at the top of this function does NOT increment the counter
        # — there's no session to increment in that path. Only the
        # navigation_failed path (Playwright raised on a real session)
        # contributes; invalid_input and budget_exhausted are scoped
        # to different signals.
        next_step_hint = ""
        if session.failed_click_count >= 2:
            next_step_hint = (
                "Multiple click attempts have failed in this session. "
                "Continuing to try will not succeed. Your next action MUST "
                "be escalate_to_user with reason='click_failure' and a "
                "brief explanation of what you were trying to do."
            )
        session.failed_click_count += 1
        return BrowserClickResult(
            status="navigation_failed",
            selector_or_role=selector_or_role,
            step_count=session.step_count,
            warnings=[f"click_failed:{type(exc).__name__}"],
            next_step=next_step_hint,
        )

    await _log_activity(
        action_type="browser_click",
        description=f"clicked {selector_or_role}",
        raw={
            "selector_or_role": selector_or_role,
            "step_count": session.step_count,
            # Diagnostic: visibility into how many failures have
            # accumulated before this successful click. Not used for
            # behavior — successful clicks don't increment the counter.
            "failed_click_count": session.failed_click_count,
        },
    )
    return BrowserClickResult(
        status="ok",
        selector_or_role=selector_or_role,
        step_count=session.step_count,
        warnings=list(session.soft_warn_tags),
    )


@function_tool
async def click(selector_or_role: str) -> str:
    """Click an element. Only for navigational links (pagination,
    accordion toggles, tab switches). Never for forms, submits,
    payments, or anything with legal weight — escalate instead.

    Args:
        selector_or_role: CSS selector targeting the element.

    Returns:
        JSON-encoded :class:`BrowserClickResult`.
    """
    result = await _click_impl(selector_or_role)
    return result.model_dump_json()


# ── tool: type_into ──────────────────────────────────────────────
#
# The first INPUT primitive. ``navigate``/``read_page``/``extract_text``
# cover "find information"; ``type_into`` is the start of "enter
# information" — filling a field on a public form the agent has read
# (e.g. a magistrate-court eviction form, a utility-portal field).
#
# Typing a value into a field is itself low-risk and reversible (nothing
# is filed until a submit). The legal/financial act is the SUBMIT, which
# is a separate, confirmation-gated primitive (follow-up). So this tool
# fills only; it never submits. It mirrors ``_click_impl`` exactly:
# browser_unavailable guard -> input validation -> budget check ->
# Playwright fill -> activity log -> structured result. The typed value
# is NEVER echoed to the model or written to logs (form data is often
# sensitive); only the field selector and character count are recorded.


async def _type_into_impl(
    selector_or_role: str,
    text: str,
    *,
    settings: Settings | None = None,
) -> BrowserTypeResult:
    """Type ``text`` into the form field identified by ``selector_or_role``.

    Fills only — does not submit. The accompanying submit primitive is
    confirmation-gated; this fill step is reversible (the user reviews the
    populated form before any submit), so it is not gated here.
    """
    settings = settings or get_settings()
    session = current_session()
    if session is None:
        return BrowserTypeResult(
            status="browser_unavailable",
            selector_or_role=str(selector_or_role),
            warnings=["browser_unavailable"],
        )

    if not isinstance(selector_or_role, str) or not selector_or_role.strip():
        return BrowserTypeResult(
            status="invalid_input",
            selector_or_role=str(selector_or_role),
            warnings=["invalid_input: selector_or_role must be a non-empty string"],
        )
    if not isinstance(text, str) or text == "":
        return BrowserTypeResult(
            status="invalid_input",
            selector_or_role=str(selector_or_role),
            warnings=["invalid_input: text must be a non-empty string"],
        )
    selector_or_role = selector_or_role.strip()

    session.step_count += 1
    budget = check_budget(session, settings)
    if budget.exhausted:
        return BrowserTypeResult(
            status="budget_exhausted",
            selector_or_role=selector_or_role,
            step_count=session.step_count,
            warnings=[_budget_warning(budget)],
        )

    try:
        await session.page.locator(selector_or_role).first.fill(text, timeout=5000)
    except Exception as exc:  # noqa: BLE001
        logger.warning("type_into failed selector=%s: %s", selector_or_role, exc)
        return BrowserTypeResult(
            status="navigation_failed",
            selector_or_role=selector_or_role,
            step_count=session.step_count,
            warnings=[f"type_failed:{type(exc).__name__}"],
        )

    # Record the field for the submit confirm card — masked at capture so
    # the raw value is never retained (the session holds only the masked
    # form). This is what lets submit_form show the user WHAT is being filed.
    _record_filled_field(session, selector_or_role, text)

    # Log the field + length only — NEVER the typed value (sensitive form
    # data). Consistent with the ID-redaction discipline elsewhere.
    await _log_activity(
        action_type="browser_type_into",
        description=f"typed {len(text)} chars into {selector_or_role}",
        raw={
            "selector_or_role": selector_or_role,
            "char_count": len(text),
            "step_count": session.step_count,
        },
    )
    return BrowserTypeResult(
        status="ok",
        selector_or_role=selector_or_role,
        char_count=len(text),
        step_count=session.step_count,
        warnings=list(session.soft_warn_tags),
    )


@function_tool
async def type_into(selector_or_role: str, text: str) -> str:
    """Type text into a form field on the current page (fill, not submit).

    Use this to populate fields on a public form you have already read —
    e.g. entering details into a magistrate-court eviction form or a
    utility-portal field. This FILLS the field only; it does NOT submit
    the form, and it does NOT log in. Submitting and authentication are
    separate, confirmation-gated steps — never try to submit by typing
    Enter or clicking a submit control here; populate the fields and let
    the (gated) submit step handle filing.

    The value you type is treated as sensitive: it is filled into the page
    but never echoed back to you or written to logs.

    Args:
        selector_or_role: CSS selector targeting the input/textarea field.
        text: The value to type into the field.

    Returns:
        JSON-encoded :class:`BrowserTypeResult` (reports the field and the
        number of characters typed, not the value).
    """
    result = await _type_into_impl(selector_or_role, text)
    return result.model_dump_json()


# ── tool: select_option ──────────────────────────────────────────
#
# Second INPUT primitive. Picks a value in a <select> dropdown. Like
# ``type_into`` this is REVERSIBLE — selecting an option changes nothing
# server-side until a submit, and the user reviews the populated form
# before any (gated) submit. So it is NOT confirmation-gated. Mirrors
# ``_type_into_impl`` exactly, including the sensitive-value discipline:
# the chosen option value is form data and is never echoed to the model
# or written to logs — only the field selector and value length.


async def _select_option_impl(
    selector_or_role: str,
    value: str,
    *,
    settings: Settings | None = None,
) -> BrowserSelectResult:
    """Select ``value`` in the dropdown identified by ``selector_or_role``.

    Reversible (no submit), so not gated — consistent with ``type_into``.
    """
    settings = settings or get_settings()
    session = current_session()
    if session is None:
        return BrowserSelectResult(
            status="browser_unavailable",
            selector_or_role=str(selector_or_role),
            warnings=["browser_unavailable"],
        )

    if not isinstance(selector_or_role, str) or not selector_or_role.strip():
        return BrowserSelectResult(
            status="invalid_input",
            selector_or_role=str(selector_or_role),
            warnings=["invalid_input: selector_or_role must be a non-empty string"],
        )
    if not isinstance(value, str) or value == "":
        return BrowserSelectResult(
            status="invalid_input",
            selector_or_role=str(selector_or_role),
            warnings=["invalid_input: value must be a non-empty string"],
        )
    selector_or_role = selector_or_role.strip()

    session.step_count += 1
    budget = check_budget(session, settings)
    if budget.exhausted:
        return BrowserSelectResult(
            status="budget_exhausted",
            selector_or_role=selector_or_role,
            step_count=session.step_count,
            warnings=[_budget_warning(budget)],
        )

    try:
        await session.page.locator(selector_or_role).first.select_option(value, timeout=5000)
    except Exception as exc:  # noqa: BLE001
        logger.warning("select_option failed selector=%s: %s", selector_or_role, exc)
        return BrowserSelectResult(
            status="navigation_failed",
            selector_or_role=selector_or_role,
            step_count=session.step_count,
            warnings=[f"select_failed:{type(exc).__name__}"],
        )

    # Record the field for the submit confirm card (masked at capture),
    # same as type_into — the user should see selected values too.
    _record_filled_field(session, selector_or_role, value)

    # Field + value length only — NEVER the chosen value (sensitive form data).
    await _log_activity(
        action_type="browser_select_option",
        description=f"selected option ({len(value)} chars) in {selector_or_role}",
        raw={
            "selector_or_role": selector_or_role,
            "value_count": len(value),
            "step_count": session.step_count,
        },
    )
    return BrowserSelectResult(
        status="ok",
        selector_or_role=selector_or_role,
        value_count=len(value),
        step_count=session.step_count,
        warnings=list(session.soft_warn_tags),
    )


@function_tool
async def select_option(selector_or_role: str, value: str) -> str:
    """Pick a value in a dropdown (<select>) on the current page.

    Reversible, like ``type_into`` — selecting an option changes nothing
    until a submit, so this is not confirmation-gated. Use it while
    populating a form (e.g. choosing a county or case type on a court
    form). It does NOT submit and does NOT log in.

    The chosen value is treated as sensitive: it is applied to the page
    but never echoed back to you or written to logs.

    Args:
        selector_or_role: CSS selector targeting the <select> field.
        value: The option value/label to select.

    Returns:
        JSON-encoded :class:`BrowserSelectResult` (reports the field and
        the length of the chosen value, not the value).
    """
    result = await _select_option_impl(selector_or_role, value)
    return result.model_dump_json()


# ── tool: submit_form (GATED) ────────────────────────────────────
#
# The IRREVERSIBLE input primitive — the legal/financial act. Clicking a
# submit control files the form (e.g. an eviction filing, a utility
# request). Per the write-confirmation arc (PRs #208–#210), a tool with a
# real side effect gates itself INSIDE its own body via ``confirm_or_abort``
# and performs the side effect ONLY on explicit approval. Deny, timeout,
# or queue-full → fire NOTHING (fail closed).
#
# Confirm-card content: the preview identifies the form (current site/URL +
# the submit control) AND lists the fields the agent populated this task, so
# the human approving a legally-serious filing can see WHAT is being filed —
# not just that *a* submit is about to happen. Field values are MASKED
# (``_mask_value``): the conflicting-conventions problem the old stub flagged
# (existing cards show values; type_into never stored raw values) is resolved
# by masking at capture — ``session.filled_fields`` already holds only masked
# values, so the card surfaces them without re-reading the DOM or re-exposing
# raw form data. Masking strength is tunable in one place (``_mask_value``).


def _submit_confirm_details(
    page_url: str,
    selector_or_role: str,
    filled_fields: list[tuple[str, str]] | None = None,
) -> tuple[str, list[str]]:
    """Build the (details, items) preview for the submit confirm card.

    Shows form identity (site + submit control) plus one masked row per
    field populated this task (``filled_fields`` carries ``(selector,
    masked_value)`` pairs — already masked at capture, never raw). When no
    fields were recorded, falls back to identity-only.
    """
    fields = filled_fields or []
    site = page_url or "the current page"
    items = [f"Site: {site}", f"Submit control: {selector_or_role}"]
    for selector, masked in fields:
        items.append(f"{selector}: {masked}")

    if fields:
        plural = "field" if len(fields) == 1 else "fields"
        details = (
            f"About to submit a form on {site} via control '{selector_or_role}', "
            f"filing the {len(fields)} {plural} you populated (values masked below)."
        )
    else:
        details = f"About to submit a form on {site} via control '{selector_or_role}'."
    return details, items


async def _submit_form_impl(
    selector_or_role: str,
    expected_confirmation: str = "",
    *,
    settings: Settings | None = None,
) -> BrowserSubmitResult:
    """Submit the form by activating ``selector_or_role`` (e.g. a submit
    button). Confirmation-gated: the click fires ONLY after explicit user
    approval via ``confirm_or_abort``. Fail closed on deny/timeout/queue-full.

    Cut 2 — after the click, a READ-ONLY read-back verifies the submission
    actually landed before reporting success: only a POSITIVE confirmation
    (the caller's ``expected_confirmation`` phrase on the resulting page, or a
    confirmation-shaped URL) reports ``submitted``. A failure signal, or no
    confirmable signal, reports ``not_submitted`` with an honest "couldn't
    confirm" message — never a false success, never a re-click.
    """
    settings = settings or get_settings()
    session = current_session()
    if session is None:
        return BrowserSubmitResult(
            status="browser_unavailable",
            selector_or_role=str(selector_or_role),
            warnings=["browser_unavailable"],
        )

    if not isinstance(selector_or_role, str) or not selector_or_role.strip():
        return BrowserSubmitResult(
            status="invalid_input",
            selector_or_role=str(selector_or_role),
            warnings=["invalid_input: selector_or_role must be a non-empty string"],
        )
    selector_or_role = selector_or_role.strip()

    session.step_count += 1
    budget = check_budget(session, settings)
    if budget.exhausted:
        return BrowserSubmitResult(
            status="budget_exhausted",
            selector_or_role=selector_or_role,
            step_count=session.step_count,
            warnings=[_budget_warning(budget)],
        )

    # ── THE GATE + IDEMPOTENCY ── the click below does not run without
    # approval, and an approved submit is claimed so a duplicate (double-
    # approval / retry / restart) never files the form twice.
    page_url = ""
    try:
        page_url = str(session.page.url)
    except Exception:  # noqa: BLE001 — url read is best-effort for the preview
        page_url = ""
    details, items = _submit_confirm_details(page_url, selector_or_role, session.filled_fields)

    ctx = current_memory_context()
    company_id = ctx.company_id if ctx else ""
    # Key over the FULL field values (raw, not the masked display) — sha256
    # keeps them private. Session-scoped (session_id + final_url + selector +
    # filled fields); no time window — a filing is bound to this task.
    idem_key = derive_key(
        company_id,
        "browser_submit_form",
        {"fields": [list(f) for f in session.filled_fields]},
        extra=f"{session.session_id}\x1f{page_url}\x1f{selector_or_role}",
    )

    async def _do_submit() -> str:
        await session.page.locator(selector_or_role).first.click(timeout=5000)
        return "submitted"

    # Read-back evidence captured by the verifier for the outcome arms below.
    evidence: dict[str, str] = {"final_url": "", "detail": ""}

    async def _verify_submitted(_result: str) -> bool:
        """READ-ONLY read-back: did the submission actually land? Settles the
        post-submit page, snapshots it, and classifies (fail-safe). Returns
        True ONLY on a positive confirmation; False otherwise. Never re-clicks.
        Raising (unreadable page) maps to verify_error → needs-review."""
        # Settle the resulting page (bounded; mirrors navigate's wait pattern).
        try:
            await asyncio.wait_for(
                session.page.wait_for_load_state("domcontentloaded"),
                timeout=_NAVIGATION_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001 — best-effort settle; snapshot still tried
            await asyncio.sleep(_NAVIGATION_RETRY_BACKOFF)

        final_url = ""
        try:
            final_url = str(session.page.url)
        except Exception:  # noqa: BLE001
            final_url = ""
        evidence["final_url"] = final_url

        # Read the resulting page to find the confirmation signal via the shared
        # hardened reader: a11y snapshot primary, body-text fallback (the live
        # #217/#237 bug — snapshot raised on real DOM while the submit had
        # actually landed). Only a genuinely unreadable page (BOTH reads error)
        # raises → verify_error → needs-review; a readable-but-blank page is "no
        # signal" → not success.
        lines, source = await _read_page_lines(session.page)
        if source == "unreadable":
            raise RuntimeError(
                "post-submit page unreadable: a11y snapshot and body read both failed"
            )
        page_text = "\n".join(lines)

        verdict, detail = _classify_submit_outcome(
            flattened_tree=page_text,
            final_url=final_url,
            starting_url=page_url,
            expected_confirmation=expected_confirmation,
        )
        evidence["detail"] = _sanitize_for_log(detail, limit=200)
        # None (no confirmable signal) is NOT success — fail safe to needs-review.
        return verdict is True

    try:
        guard = await run_guarded(
            company_id=company_id,
            idem_key=idem_key,
            action_type="browser_submit_form",
            confirm={
                "action": "browser_submit_form",
                "title": "Submit this form?",
                "details": details,
                "items": items,
            },
            execute=_do_submit,
            verify=_verify_submitted,
            verify_fail_tag="verify_failed:submit_not_confirmed",
            window=None,
        )
    except Exception as exc:  # noqa: BLE001 — Playwright click raised (post-gate)
        logger.warning("submit_form failed selector=%s: %s", selector_or_role, exc)
        return BrowserSubmitResult(
            status="navigation_failed",
            selector_or_role=selector_or_role,
            step_count=session.step_count,
            warnings=[f"submit_failed:{type(exc).__name__}"],
            final_url=evidence["final_url"],
        )

    if guard.outcome == "not_approved":
        # Declined or timed out — NOTHING was submitted.
        await _log_activity(
            action_type="browser_submit_declined",
            description=f"submit declined at confirm gate ({selector_or_role})",
            raw={"selector_or_role": selector_or_role, "step_count": session.step_count},
        )
        return BrowserSubmitResult(
            status="not_submitted",
            selector_or_role=selector_or_role,
            step_count=session.step_count,
            warnings=["not_submitted: user declined or confirmation timed out"],
        )
    if guard.outcome == "blocked_store_unavailable":
        # Fail-closed before the click — nothing was submitted.
        return BrowserSubmitResult(
            status="not_submitted",
            selector_or_role=selector_or_role,
            step_count=session.step_count,
            warnings=["not_submitted: could not safely process; nothing was submitted"],
        )
    if guard.outcome == "blocked_needs_review":
        # The click fired but the read-back could NOT confirm the filing landed
        # (failure signal, no confirmation, or an unreadable page), OR a prior
        # submit is unresolved. Fail safe: report not_submitted with honest
        # evidence — never a false success, and never a re-click.
        return BrowserSubmitResult(
            status="not_submitted",
            selector_or_role=selector_or_role,
            step_count=session.step_count,
            warnings=[
                "not_submitted: clicked the control but could not confirm the "
                "submission was accepted — please verify before retrying "
                f"({evidence['detail'] or 'no confirmation signal'})",
            ],
            final_url=evidence["final_url"],
            confirmation_detail=evidence["detail"],
        )

    if guard.outcome not in SUCCESS_OUTCOMES:
        # THIS call clicked nothing (``already_pending``): another submit of
        # the same form holds the claim, or nothing is running. It used to fall
        # through to ``status="submitted"`` and log a "submitted form" row.
        return BrowserSubmitResult(
            status="not_submitted",
            selector_or_role=selector_or_role,
            step_count=session.step_count,
            warnings=[
                "not_submitted: "
                + pending_reply(guard, action="submit the form", system="the website")
            ],
        )

    # executed or replayed AND verify confirmed → the submission landed.
    # (replay means an identical submit already fired in this session; we do
    # NOT click again.)
    await _log_activity(
        action_type="browser_submit_form",
        description=f"submitted form via {selector_or_role} ({guard.outcome})",
        raw={
            "selector_or_role": selector_or_role,
            "step_count": session.step_count,
            "guard_outcome": guard.outcome,
            "final_url": evidence["final_url"],
        },
    )
    return BrowserSubmitResult(
        status="submitted",
        selector_or_role=selector_or_role,
        step_count=session.step_count,
        warnings=list(session.soft_warn_tags),
        final_url=evidence["final_url"],
        confirmation_detail=evidence["detail"],
    )


@function_tool
async def submit_form(selector_or_role: str, expected_confirmation: str = "") -> str:
    """Submit a form you have already filled — a CONFIRMED, irreversible action.

    Use this only after you have populated the form with ``type_into`` /
    ``select_option`` and the only step left is to file/send it (e.g.
    pressing "File" on a court eviction form, "Submit" on a utility
    request). This is gated: the user is shown a confirmation card and the
    submit fires ONLY if they approve. If they decline (or it times out),
    the form is NOT submitted and the result says so — report that plainly
    and do NOT retry or try to submit another way.

    After the submit, the resulting page is READ BACK to confirm the
    submission actually landed. ``status="submitted"`` is returned ONLY when
    that read-back confirms it; otherwise you get ``status="not_submitted"``
    with a "couldn't confirm" note — in that case tell the user it may not
    have gone through and do NOT re-submit.

    Never use this to log in or to bypass a sign-in wall.

    Args:
        selector_or_role: CSS selector targeting the submit button/control.
        expected_confirmation: A short phrase you expect to see on the SUCCESS
            page after filing (you just read the form, so you know what a
            receipt looks like — e.g. "confirmation number", "Case filed",
            "successfully submitted"). This is how the read-back confirms the
            filing actually landed. If you cannot predict one, leave it empty —
            the tool will still refuse to claim success without confirmation.

    Returns:
        JSON-encoded :class:`BrowserSubmitResult`: ``status="submitted"`` only
        when the read-back confirmed the filing (``final_url`` /
        ``confirmation_detail`` carry the evidence); ``status="not_submitted"``
        if the user declined OR the submission could not be confirmed.
    """
    result = await _submit_form_impl(selector_or_role, expected_confirmation)
    return result.model_dump_json()


# ── tool 5: screenshot ───────────────────────────────────────────


async def _screenshot_impl(
    *,
    settings: Settings | None = None,
) -> BrowserScreenshotResult:
    """Capture the current page as PNG and upload to Supabase Storage
    with a 24-hour signed URL. Counts against both the step budget
    AND the screenshot budget."""
    settings = settings or get_settings()
    session = current_session()
    if session is None:
        return BrowserScreenshotResult(
            status="browser_unavailable",
            warnings=["browser_unavailable"],
        )

    session.step_count += 1
    budget = check_budget(session, settings, also_screenshot=True)
    if budget.exhausted:
        return BrowserScreenshotResult(
            status="budget_exhausted",
            step_count=session.step_count,
            screenshot_count=session.screenshot_count,
            warnings=[_budget_warning(budget)],
        )

    try:
        png_bytes = await session.page.screenshot(full_page=False, timeout=8000)
    except Exception as exc:  # noqa: BLE001
        logger.warning("screenshot capture failed: %s", exc)
        return BrowserScreenshotResult(
            status="screenshot_failed",
            step_count=session.step_count,
            screenshot_count=session.screenshot_count,
            warnings=[f"screenshot_failed:{type(exc).__name__}"],
        )

    session.screenshot_count += 1
    upload = await _upload_screenshot_to_storage(
        png_bytes=png_bytes,
        company_id=session.company_id,
        session_id=session.session_id,
        step=session.step_count,
        settings=settings,
    )
    if upload is None:
        return BrowserScreenshotResult(
            status="screenshot_failed",
            step_count=session.step_count,
            screenshot_count=session.screenshot_count,
            warnings=["screenshot_failed:storage_upload"],
        )
    signed_url, expires_at = upload

    # Detect auth gates so the LLM follows up with escalate_to_user
    # (issue #155 case 061 — Shape 2a). Mirrors the existing
    # ``BrowserNavigateResult.user_facing_message`` pattern used for
    # deny-list blocks: the tool result itself carries the
    # LLM-facing instruction, removing the prompt-reliability tax.
    # Best-effort — auth detection MUST NOT break a successful
    # screenshot (the screenshot itself already succeeded above).
    next_step_hint = ""
    try:
        # Hardened read so a flaky a11y snapshot can't silently disable
        # auth-gate detection on a real login wall (#236) — falls back to
        # visible body text just like read_page / submit verify.
        lines, _source = await _read_page_lines(session.page)
        flattened = "\n".join(lines)
        if _looks_like_auth_gate(flattened):
            next_step_hint = (
                "This page appears to require sign-in. You cannot log in "
                "for the user. Your next action MUST be escalate_to_user "
                "with reason='auth_required', a brief explanation, and "
                f"screenshot_url={signed_url!r}."
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("auth-gate detection failed: %s", exc)

    # Detect "agent has been grinding on unmatched reads" so the LLM
    # follows up with escalate_to_user rather than treating screenshot
    # as a terminal action (issue #166 case 063). Auth-gate branch
    # wins on collision — login pages are higher-confidence than this
    # heuristic, and the auth-gate hint is more specific. The
    # ``if not next_step_hint`` guard enforces that precedence.
    if not next_step_hint and session.unmatched_read_count >= 2:
        next_step_hint = (
            "You have read multiple pages without finding the information "
            "the user asked for. Taking a screenshot will not surface "
            "missing content. Your next action MUST be escalate_to_user "
            "with a brief explanation that the requested information was "
            f"not present on the pages you reviewed (screenshot_url={signed_url!r})."
        )

    await _log_activity(
        action_type="browser_screenshot",
        description=f"screenshot saved (step {session.step_count})",
        raw={
            "signed_url": signed_url,
            "step_count": session.step_count,
            "screenshot_count": session.screenshot_count,
            "session_id": session.session_id,
            # Substring checks against the hint text disambiguate which
            # branch fired. Not pretty, but the diagnostic value is
            # high and these strings are stable within this file.
            "auth_gate_detected": bool(next_step_hint) and "sign-in" in next_step_hint,
            "answer_not_present_detected": (
                bool(next_step_hint) and "without finding" in next_step_hint
            ),
        },
    )
    return BrowserScreenshotResult(
        status="ok",
        signed_url=signed_url,
        expires_at=expires_at,
        step_count=session.step_count,
        screenshot_count=session.screenshot_count,
        warnings=list(session.soft_warn_tags),
        next_step=next_step_hint,
    )


@function_tool
async def screenshot() -> str:
    """Capture the current page as PNG and store it (24-hour signed
    URL). Use when escalating to the user so they can see what you
    saw. Counts against both step budget and screenshot budget.

    Returns:
        JSON-encoded :class:`BrowserScreenshotResult` with ``signed_url``.
        When the page appears to be a login/auth gate, ``next_step``
        carries an instruction string; follow it verbatim.
    """
    result = await _screenshot_impl()
    return result.model_dump_json()


# ── tool 6: escalate_to_user ─────────────────────────────────────


async def _escalate_to_user_impl(
    reason: str,
    last_screenshot_url: str | None = None,
    *,
    settings: Settings | None = None,
) -> BrowserEscalateResult:
    """Emit an escalation. Does NOT touch Playwright — this is the
    agent's signal that it cannot proceed and the user must take
    over. Returns a terminal result the agent surfaces in its reply."""
    _ = settings  # accepted for harness symmetry; not used here
    reason = str(reason or "").strip() or "no reason given"
    sanitized = _sanitize_for_log(reason, limit=500)

    await _log_activity(
        action_type="browser_escalated",
        description=sanitized,
        raw={
            "reason": sanitized,
            "last_screenshot_url": last_screenshot_url,
        },
    )
    return BrowserEscalateResult(
        reason=sanitized,
        last_screenshot_url=last_screenshot_url,
    )


@function_tool
async def escalate_to_user(
    reason: str,
    last_screenshot_url: str | None = None,
) -> str:
    """Stop the task and surface to the user. Call this when:
      * you encounter a login wall, paywall, or captcha
      * a task requires filling a form or submitting anything
      * the deny-list blocked a navigation
      * you've exhausted your budget
      * you cannot ground a useful answer

    Args:
        reason: One-paragraph description of what you tried, what
            you observed, and what the user should do next.
        last_screenshot_url: Optional signed URL from a prior
            ``screenshot()`` call to include in the escalation.

    Returns:
        JSON-encoded :class:`BrowserEscalateResult`.
    """
    result = await _escalate_to_user_impl(reason, last_screenshot_url)
    return result.model_dump_json()


__all__ = [
    "BrowserClickResult",
    "BrowserEscalateResult",
    "BrowserExtractResult",
    "BrowserNavigateResult",
    "BrowserReadResult",
    "BrowserScreenshotResult",
    "BrowserStatus",
    "BrowserTypeResult",
    "click",
    "escalate_to_user",
    "extract_text",
    "navigate",
    "read_page",
    "screenshot",
    "type_into",
    # Underscored impls exposed for the agent_flow harness + unit tests
    "_click_impl",
    "_escalate_to_user_impl",
    "_extract_text_impl",
    "_navigate_impl",
    "_read_page_impl",
    "_screenshot_impl",
    "_type_into_impl",
]
