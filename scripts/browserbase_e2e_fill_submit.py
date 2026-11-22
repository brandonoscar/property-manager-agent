"""Live Browserbase end-to-end harness for the fill→confirm→submit path (#217).

CI-only, GATED (workflow_dispatch + the ``live-e2e`` environment), and billed —
it opens a REAL Browserbase cloud browser and drives the REAL browser tools
against a SAFE public echo form (never a court site). It is the "the agent can
actually fill and file a real form" proof that the §4 sweep (mocked tools) and
the unit tests (fake page) cannot give.

What it exercises, against Selenium's official test form
(``https://www.selenium.dev/selenium/web/web-form.html``) whose submit lands on
a real HTML page showing "Received!" — a confirmable success signal, safe to
submit repeatedly, no real-world side effect:

  Scenario A — APPROVE:
    ensure_session → navigate → read_page (best-effort, non-gating) →
    type_into (a field, by CSS selector) → submit_form (approved out-of-band) →
    verify the result reports submitted and the read-back confirmation landed.
  Scenario B — DECLINE:
    submit_form with the confirm gate DECLINED → verify nothing was submitted
    (status not_submitted) — the fail-closed guarantee on a real page.

The confirm gate (``confirm_or_abort``) normally waits for a UI approval; in CI
there is no UI, so a background task drains the pending confirmation and
resolves it (approve in A, decline in B) — exercising the REAL emission/resolve
channel, not a stub.

Secret discipline mirrors the connectivity probe: the two Browserbase secrets
are reported set/unset + length ONLY, never their values; no submitted form
value is echoed to the summary.

Exit 0 only if BOTH scenarios pass; any failure exits non-zero. The plain-
language pass/fail table goes to $GITHUB_STEP_SUMMARY so the outcome is readable
without the Actions log.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys

_API_KEY_ENV = "HELIXIS_BROWSER__BROWSERBASE_API_KEY"
_PROJECT_ID_ENV = "HELIXIS_BROWSER__BROWSERBASE_PROJECT_ID"

# Safe automation test form (Selenium's official test page): submitting it lands
# on a real HTML confirmation page showing "Received!" — a confirmable success
# signal (unlike an echo endpoint that returns raw JSON). Stable + purpose-built
# for automation, safe to submit repeatedly, no real-world side effect. NOT a
# court site.
_FORM_URL = "https://www.selenium.dev/selenium/web/web-form.html"
# The form's text field + a value to type. Filled by CSS selector, so this does
# NOT depend on read_page succeeding.
_FIELD_SELECTOR = "input[name='my-text']"
_FIELD_VALUE = "Helixis E2E Probe"
_SUBMIT_SELECTOR = "button"  # the form's lone submit button
# After submit, the result page shows the heading "Received!" (the canonical
# assertion in Selenium's own docs). Use it as the caller-supplied confirmation.
_EXPECTED_CONFIRMATION = "Received!"

_RESOLVE_POLL_SECONDS = 0.1
_RESOLVE_TIMEOUT_SECONDS = 20.0


def _secret_state(env_name: str) -> tuple[bool, int]:
    value = os.environ.get(env_name) or ""
    return bool(value), len(value)


def _write_summary(lines: list[str]) -> None:
    block = "\n".join(lines) + "\n"
    print(block, flush=True)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as fh:
                fh.write(block)
        except OSError as exc:  # best-effort
            print(f"(warning: could not write step summary: {exc})", flush=True)


async def _resolve_one_confirmation(*, approved: bool) -> bool:
    """Drain the pending confirm emission and resolve it (approve/decline).

    Exercises the REAL module-level emission/resolve channel that the UI would
    drive. Returns True if a confirmation was found and resolved."""
    from helixis.agents import tools as agent_tools

    waited = 0.0
    while waited < _RESOLVE_TIMEOUT_SECONDS:
        drained = await agent_tools.drain_pending_confirmations()
        if drained:
            agent_tools.resolve_confirmation(drained[0]["confirm_id"], approved)
            return True
        await asyncio.sleep(_RESOLVE_POLL_SECONDS)
        waited += _RESOLVE_POLL_SECONDS
    return False


async def _scenario_approve() -> tuple[bool, list[str]]:
    """Fill → submit (approved) → expect submitted + confirmation. Returns
    (ok, detail_lines)."""
    from helixis.agents.browser_tools import (
        _navigate_impl,
        _read_page_impl,
        _submit_form_impl,
        _type_into_impl,
    )

    lines: list[str] = []

    nav = await _navigate_impl(_FORM_URL)
    if nav.status != "ok":
        return False, [f"navigate → {nav.status} (expected ok)"]
    lines.append(f"navigate → {nav.status}")

    # read_page is BEST-EFFORT and does NOT gate the flow. type_into targets a
    # CSS selector directly and needs no prior read_page; a flaky/failed
    # accessibility snapshot here must not block the fill→submit it precedes.
    read = await _read_page_impl("")
    lines.append(f"read_page → {read.status} (informational; non-gating)")

    typed = await _type_into_impl(_FIELD_SELECTOR, _FIELD_VALUE)
    if typed.status != "ok":
        return False, lines + [f"type_into → {typed.status} (expected ok)"]
    lines.append(f"type_into → {typed.status} (chars={typed.char_count})")

    # Drive submit + approve the gate concurrently.
    submit_task = asyncio.create_task(_submit_form_impl(_SUBMIT_SELECTOR, _EXPECTED_CONFIRMATION))
    resolved = await _resolve_one_confirmation(approved=True)
    if not resolved:
        submit_task.cancel()
        with contextlib.suppress(Exception):
            await submit_task
        return False, lines + ["submit_form → confirm gate never emitted (FAIL)"]
    result = await asyncio.wait_for(submit_task, timeout=_RESOLVE_TIMEOUT_SECONDS + 25.0)
    lines.append(f"submit_form (approved) → {result.status}")
    lines.append(f"  final_url captured: {'yes' if result.final_url else 'no'}")
    lines.append(f"  confirmation_detail: {result.confirmation_detail or '(none)'}")

    # ── DEBUG: show what the page actually looked like post-submit, via TWO
    # independent reads, so we can tell a verifier false-negative (the a11y
    # read the verifier uses is thin/flaky on this real page) from a genuine
    # submit failure (the page never reached the 'Received!' confirmation).
    lines += await _debug_dump_post_submit(expected=_EXPECTED_CONFIRMATION)

    ok = result.status == "submitted"
    if not ok:
        lines.append("  EXPECTED status=submitted with read-back confirmation")
    return ok, lines


async def _debug_dump_post_submit(*, expected: str) -> list[str]:
    """READ-ONLY: capture the actual post-submit page two ways so we can see
    what the verifier saw. Never mutates the page. All best-effort."""
    from helixis.agents.browser_session import current_session
    from helixis.agents.browser_tools import _flatten_accessibility_tree

    out: list[str] = ["  --- debug: what the page actually shows ---"]
    session = current_session()
    if session is None or session.page is None:
        return out + ["  (no live session/page to inspect)"]

    page = session.page
    # 1. Real final URL.
    try:
        out.append(f"  url now: {page.url}")
    except Exception as exc:  # noqa: BLE001
        out.append(f"  url read failed: {type(exc).__name__}")

    # 2. Accessibility-tree read — the SAME signal the verifier relies on.
    a11y_has = "unknown"
    try:
        tree = await page.accessibility.snapshot()
        flat = "\n".join(_flatten_accessibility_tree(tree)) if tree else ""
        a11y_has = "yes" if expected.lower() in flat.lower() else "no"
        out.append(f"  a11y tree length: {len(flat)} chars; contains '{expected}': {a11y_has}")
    except Exception as exc:  # noqa: BLE001
        out.append(f"  a11y snapshot raised: {type(exc).__name__} (verifier read is failing)")

    # 3. A SECOND opinion — plain visible body text. If THIS contains the
    # confirmation but the a11y read didn't, it's a verifier false-negative.
    try:
        body = await page.inner_text("body", timeout=5000)
        body_has = "yes" if expected.lower() in body.lower() else "no"
        out.append(f"  body text length: {len(body)} chars; contains '{expected}': {body_has}")
    except Exception as exc:  # noqa: BLE001
        out.append(f"  body inner_text raised: {type(exc).__name__}")
    return out


async def _scenario_decline() -> tuple[bool, list[str]]:
    """submit (declined) → expect not_submitted (fail-closed on a real page)."""
    from helixis.agents.browser_tools import _navigate_impl, _submit_form_impl

    lines: list[str] = []
    # Re-navigate to a clean form so the decline scenario is independent.
    nav = await _navigate_impl(_FORM_URL)
    if nav.status != "ok":
        return False, [f"navigate → {nav.status} (expected ok)"]

    submit_task = asyncio.create_task(_submit_form_impl(_SUBMIT_SELECTOR, _EXPECTED_CONFIRMATION))
    resolved = await _resolve_one_confirmation(approved=False)
    if not resolved:
        submit_task.cancel()
        with contextlib.suppress(Exception):
            await submit_task
        return False, lines + ["submit_form → confirm gate never emitted (FAIL)"]
    result = await asyncio.wait_for(submit_task, timeout=_RESOLVE_TIMEOUT_SECONDS + 5.0)
    lines.append(f"submit_form (declined) → {result.status}")
    ok = result.status == "not_submitted"
    if not ok:
        lines.append("  EXPECTED status=not_submitted (fail-closed on decline)")
    return ok, lines


async def _run() -> int:
    from helixis.agents.browser_session import close_session, current_session, ensure_session
    from helixis.memory.context import memory_context
    from helixis.settings import get_settings

    api_set, api_len = _secret_state(_API_KEY_ENV)
    proj_set, proj_len = _secret_state(_PROJECT_ID_ENV)

    summary: list[str] = [
        "## Browserbase live fill→submit e2e (#217)",
        "",
        f"Real cloud browser against a SAFE echo form (`{_FORM_URL}`) — not a court site.",
        "",
        "| Check | Result |",
        "| --- | --- |",
        f"| `{_API_KEY_ENV}` set? | {'yes' if api_set else 'NO'} (len {api_len}) |",
        f"| `{_PROJECT_ID_ENV}` set? | {'yes' if proj_set else 'NO'} (len {proj_len}) |",
    ]

    if not (api_set and proj_set):
        summary += ["", "**RESULT: ❌ FAIL — Browserbase secrets missing; wire them and re-run.**"]
        _write_summary(summary)
        return 1

    settings = get_settings()
    overall_ok = True

    # Both scenarios share one memory context + session lifecycle.
    with memory_context(company_id="ci-e2e", user_id="ci", session_id="e2e-217"):
        session = await ensure_session(company_id="ci-e2e", settings=settings)
        if session is None:
            summary += [
                "| Session created? | NO |",
                "",
                "**RESULT: ❌ FAIL — ensure_session returned None (creds set). Likely the"
                " [browser] extra isn't installed or Browserbase connect failed.**",
            ]
            _write_summary(summary)
            return 1
        summary.append("| Session created? | yes |")

        try:
            approve_ok, approve_lines = await _scenario_approve()
            decline_ok, decline_lines = await _scenario_decline()
        finally:
            await close_session()

    overall_ok = approve_ok and decline_ok
    closed_cleanly = current_session() is None

    summary += [
        f"| Scenario A — APPROVE → submitted? | {'✅ yes' if approve_ok else '❌ NO'} |",
        f"| Scenario B — DECLINE → not_submitted? | {'✅ yes' if decline_ok else '❌ NO'} |",
        f"| Session closed cleanly? | {'yes' if closed_cleanly else 'NO'} |",
        "",
        "<details><summary>Scenario A steps</summary>",
        "",
        "```",
        *approve_lines,
        "```",
        "</details>",
        "",
        "<details><summary>Scenario B steps</summary>",
        "",
        "```",
        *decline_lines,
        "```",
        "</details>",
        "",
        (
            "**RESULT: ✅ PASS — the agent filled and submitted a real form, the confirm"
            " gate fired, the read-back confirmed the submission, and a decline filed"
            " nothing.**"
            if (overall_ok and closed_cleanly)
            else "**RESULT: ❌ FAIL — see the rows marked NO above.**"
        ),
    ]
    _write_summary(summary)
    return 0 if (overall_ok and closed_cleanly) else 1


def main() -> int:
    try:
        return asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 — last-resort guard so CI gets a summary
        _write_summary(
            [
                "## Browserbase live fill→submit e2e (#217)",
                "",
                f"**RESULT: ❌ FAIL — unexpected error: {type(exc).__name__}.**",
            ]
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
