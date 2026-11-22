"""CI-only, read-only Browserbase connectivity probe.

Purpose: prove that the Browserbase credentials wired into GitHub Secrets can
open a real cloud-browser session end-to-end — WITHOUT touching local disk,
chat, or any real form. It is the minimal "the wire works" check that issue
#217 (live Browserbase end-to-end) needs before any real-filing run.

Strictly read-only and minimal:

    ensure_session()  ->  page.goto("https://example.com")  ->  page.title()
                      ->  close_session()

It REUSES the production session lifecycle (``ensure_session`` /
``close_session`` in :mod:`helixis.agents.browser_session`) rather than
hand-rolling a Browserbase client, so a green probe exercises the exact code
the browser_agent uses. There is no ``type_into``, no ``select_option``, no
``submit_form``, and no navigation beyond the single ``example.com`` load.

Secret discipline: the two Browserbase secrets are reported as set/unset plus
character length ONLY — their values are never printed, logged, or written to
the step summary. The credentials are read from the environment by Pydantic
settings exactly as in production (env names below); this script only inspects
their PRESENCE for the summary.

    HELIXIS_BROWSER__BROWSERBASE_API_KEY      -> settings.browser.browserbase_api_key
    HELIXIS_BROWSER__BROWSERBASE_PROJECT_ID   -> settings.browser.browserbase_project_id

Exit code is 0 only when a session was created, a non-empty page title was
captured, and the session closed cleanly; any failure exits non-zero.
"""

from __future__ import annotations

import asyncio
import os
import sys

# These are the GitHub Secret names AND the Pydantic env-var names (verified
# against settings.py: env_prefix="HELIXIS_", env_nested_delimiter="__",
# field browser.browserbase_{api_key,project_id}).
_API_KEY_ENV = "HELIXIS_BROWSER__BROWSERBASE_API_KEY"
_PROJECT_ID_ENV = "HELIXIS_BROWSER__BROWSERBASE_PROJECT_ID"

_EXAMPLE_URL = "https://example.com"
_NAV_TIMEOUT_MS = 20_000


def _secret_state(env_name: str) -> tuple[bool, int]:
    """Return (is_set, length) for an env var — never the value itself."""
    value = os.environ.get(env_name) or ""
    return bool(value), len(value)


def _write_summary(lines: list[str]) -> None:
    """Append a plain-language summary to $GITHUB_STEP_SUMMARY (if present)
    and always echo it to stdout. No secret values ever pass through here."""
    block = "\n".join(lines) + "\n"
    print(block, flush=True)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as fh:
                fh.write(block)
        except OSError as exc:  # best-effort — never fail the probe on summary I/O
            print(f"(warning: could not write step summary: {exc})", flush=True)


async def _run() -> int:
    # Import inside the function so a missing [browser] extra surfaces as a
    # clear probe failure with a summary, not an import crash at load time.
    from helixis.agents.browser_session import (
        close_session,
        current_session,
        ensure_session,
    )
    from helixis.settings import get_settings

    api_set, api_len = _secret_state(_API_KEY_ENV)
    proj_set, proj_len = _secret_state(_PROJECT_ID_ENV)

    summary: list[str] = [
        "## Browserbase connectivity probe (read-only)",
        "",
        "Opens one real Browserbase session, loads `https://example.com`, reads"
        " the page title, and closes. No form, no submit, no other navigation.",
        "",
        "| Check | Result |",
        "| --- | --- |",
        f"| `{_API_KEY_ENV}` set? | {'yes' if api_set else 'NO'} (len {api_len}) |",
        f"| `{_PROJECT_ID_ENV}` set? | {'yes' if proj_set else 'NO'} (len {proj_len}) |",
    ]

    if not (api_set and proj_set):
        summary += [
            "| Session created? | not attempted |",
            "| Title captured? | not attempted |",
            "| Closed cleanly? | not attempted |",
            "",
            "**RESULT: ❌ FAIL — one or both Browserbase secrets are missing."
            " Wire them as repo/environment secrets and re-run.**",
        ]
        _write_summary(summary)
        return 1

    settings = get_settings()

    session = await ensure_session(company_id="ci-probe", settings=settings)
    if session is None:
        # Both secrets are set (checked above), so a None here means the
        # [browser] libs are missing OR the Browserbase create / Playwright
        # connect failed. Values are NOT printed — only the diagnosis.
        summary += [
            "| Session created? | NO |",
            "| Title captured? | not attempted |",
            "| Closed cleanly? | not attempted |",
            "",
            "**RESULT: ❌ FAIL — `ensure_session` returned `None` despite both"
            " secrets being set. Likely the `[browser]` extra is not installed,"
            " or the Browserbase session create / Playwright CDP connect failed."
            " See the Actions log for the `browser_session` warning line.**",
        ]
        _write_summary(summary)
        return 1

    title = ""
    nav_error = ""
    try:
        await session.page.goto(
            _EXAMPLE_URL, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS
        )
        title = (await session.page.title() or "").strip()
    except Exception as exc:  # noqa: BLE001 — Playwright surface is broad; report type only
        nav_error = type(exc).__name__
    finally:
        # REUSE the production teardown. It is best-effort (swallows errors and
        # always resets the contextvar), so we treat "contextvar cleared" as a
        # clean close signal.
        await close_session()

    closed_cleanly = current_session() is None
    title_captured = bool(title)
    ok = title_captured and closed_cleanly and not nav_error

    summary += [
        "| Session created? | yes |",
        f"| Title captured? | {'yes' if title_captured else 'NO'}"
        + (f" (nav error: {nav_error})" if nav_error else "")
        + " |",
        f"| Closed cleanly? | {'yes' if closed_cleanly else 'NO'} |",
        "",
        (
            "**RESULT: ✅ PASS — Browserbase wire is live: session created,"
            " title captured, session closed.**"
            if ok
            else "**RESULT: ❌ FAIL — session opened but the probe did not fully"
            " succeed (see the row(s) marked NO above).**"
        ),
    ]
    _write_summary(summary)
    return 0 if ok else 1


def main() -> int:
    try:
        return asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 — last-resort guard so CI gets a summary, not a bare trace
        _write_summary(
            [
                "## Browserbase connectivity probe (read-only)",
                "",
                f"**RESULT: ❌ FAIL — unexpected error: {type(exc).__name__}.**",
            ]
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
