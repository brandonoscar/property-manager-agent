"""Orchestrates all smoke checks in parallel with per-check timeout.

Total runtime is roughly ``max(individual check) + small overhead``
because every check runs concurrently via :func:`asyncio.gather`.
Each check is wrapped in :func:`asyncio.wait_for`; a check that hangs is
failed with detail ``"timeout"`` so the smoke run never blocks the daily
cron.

The unit of work here is a REAL third-party round-trip, not a localhost
ping, so the budget must cover a genuine call. A blanket 5s budget
(pre-fix) false-failed the heavy checks every morning — a live Gemini
completion alone runs 5-12s (up to ~20s cold, see ``llm/router.py``), and
a cold asyncpg pool + TLS handshake to Supabase can exceed 5s — while
Composio answered in 2.5-3.5s. So each check gets a realistic default and
the live-LLM check a larger override.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
from collections.abc import Awaitable
from datetime import UTC, datetime
from pathlib import Path

from helixis.settings import get_settings
from helixis.smoke import checks as _checks
from helixis.smoke.checks import CheckResult
from helixis.smoke.output import format_json, format_pretty

logger = logging.getLogger(__name__)

# Realistic budgets for healthy real-service round-trips. A check that
# exceeds its budget is a genuine hang/outage and fails with "timeout".
_DEFAULT_CHECK_TIMEOUT_SECONDS = 12.0
_CHECK_TIMEOUT_OVERRIDES: dict[str, float] = {
    # A real Gemini completion; cold calls reach ~20s (llm/router.py).
    "llm_router": 25.0,
    # Probes the non-default model slugs concurrently — one cold Gemini
    # round-trip in wall-time, so it needs the same headroom as llm_router.
    "model_slots": 25.0,
    # mem0 init (embedder + pgvector connect) + a live query-embed round-trip.
    "memory": 25.0,
    # A live judge completion (JSON response_format) on the reasoning model.
    "judge": 25.0,
}

_DEFAULT_COMPANY_ID = "demo-company"


def _timeout_for(name: str) -> float:
    """The per-check budget, with an override for the live-LLM check."""
    return _CHECK_TIMEOUT_OVERRIDES.get(name, _DEFAULT_CHECK_TIMEOUT_SECONDS)


async def _run_with_timeout(
    name: str, coro: Awaitable[CheckResult], *, timeout: float | None = None
) -> CheckResult:
    """Wrap a check coroutine in its budget. Timeout becomes a fail."""
    budget = timeout if timeout is not None else _timeout_for(name)
    started = time.perf_counter()
    try:
        return await asyncio.wait_for(coro, timeout=budget)
    except TimeoutError:
        elapsed = int((time.perf_counter() - started) * 1000)
        return CheckResult(name, "fail", elapsed, "timeout")
    except Exception as exc:  # noqa: BLE001
        # A check should never crash the whole run — every check already
        # catches its own errors, but a bug could still escape. Turn any
        # escapee into a FAIL for that check so the run completes and always
        # emits parseable JSON. (A ``SystemExit`` from a check — gotcha 7 —
        # can't be caught here: CPython re-raises it out of the event loop,
        # past this awaiter; the top-level ``main()`` guard is its backstop.)
        elapsed = int((time.perf_counter() - started) * 1000)
        return CheckResult(name, "fail", elapsed, f"{type(exc).__name__}: {exc}")


async def run_smoke(*, json_output: bool = False, out_path: str | None = None) -> bool:
    """Run all checks in parallel, print output, return overall pass.

    Returns ``True`` when every check is ``pass`` or ``skipped``.
    Returns ``False`` when any check is ``fail``.
    """
    settings = get_settings()
    user_id = os.environ.get("HELIXIS_SMOKE_COMPANY_ID", _DEFAULT_COMPANY_ID)

    started = time.perf_counter()

    tasks: list[Awaitable[CheckResult]] = [
        _run_with_timeout("buildium_api", _checks.check_buildium(settings)),
        _run_with_timeout(
            "composio_gmail",
            _checks.check_composio_toolkit(settings, "composio_gmail", "GMAIL", user_id),
        ),
        _run_with_timeout(
            "composio_calendar",
            _checks.check_composio_toolkit(
                settings, "composio_calendar", "GOOGLECALENDAR", user_id
            ),
        ),
        _run_with_timeout(
            "composio_drive",
            _checks.check_composio_toolkit(settings, "composio_drive", "GOOGLEDRIVE", user_id),
            # ⚠ GOOGLEDOCS / GOOGLESHEETS deliberately have NO probe yet.
            # check_composio_toolkit FAILS (not skips) with no matching
            # connection, so adding them before the toolkits are actually
            # connected would red the daily smoke every morning for an
            # unconfigured feature — the red-forever cron of gotcha 23, which
            # teaches people to ignore the one signal that matters. Add both
            # lines (and bump the check-count assertions in test_smoke.py plus
            # the "all 17 checks green" line in CLAUDE.md §4) once the grants
            # exist.
        ),
        _run_with_timeout("supabase_postgres", _checks.check_postgres(settings)),
        _run_with_timeout("storage_buckets", _checks.check_storage_buckets(settings)),
        _run_with_timeout("geocoder", _checks.check_geocoder(settings)),
        _run_with_timeout("web_search", _checks.check_web_search(settings)),
        _run_with_timeout("webhook_queue", _checks.check_webhook_queue(settings)),
        _run_with_timeout("llm_router", _checks.check_llm_router(settings)),
        _run_with_timeout("model_slots", _checks.check_model_slots(settings)),
        _run_with_timeout("memory", _checks.check_memory(settings)),
        _run_with_timeout("judge", _checks.check_judge(settings)),
        _run_with_timeout("langfuse", _checks.check_langfuse(settings)),
        _run_with_timeout("twilio_sms", _checks.check_twilio(settings)),
        _run_with_timeout("stripe", _checks.check_stripe(settings)),
        _run_with_timeout("billing_rows", _checks.check_billing_rows(settings)),
    ]

    # Run the checks with stdout redirected to STDERR. Several dependencies
    # print banners straight to stdout on import/init (mem0's "Mem0 Platform
    # is optimized…" promo, etc.), and CI runs the smoke as
    # ``python -m helixis.smoke --json > smoke_output.json`` — any stray stdout
    # line pollutes that file so ``jq`` rejects it and the workflow reports the
    # opaque "smoke output missing or malformed" (the OTHER half of the blind
    # smoke, alongside the crash guard in #728). Keeping stdout clean means the
    # ONLY thing on it is the final JSON we print below; the noise still shows
    # in the CI log via stderr.
    with contextlib.redirect_stdout(sys.stderr):
        results: list[CheckResult] = list(await asyncio.gather(*tasks))
    duration = time.perf_counter() - started
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # A skipped check does not fail the run — a genuinely dark feature
    # (Twilio before A2P approval, Langfuse when unconfigured) has nothing to
    # probe, and failing on it is the red-forever cron of gotcha 23.
    #
    # ⚠ This comment said "Langfuse is the only one that opts into skip" and
    # that was never true: SEVEN checks can skip across nine branches. A
    # comment cannot fail (gotcha 48), so it stood in for the audit nobody
    # did — and underneath it ``memory`` was skipping whenever the Voyage key
    # was absent, i.e. going green in exactly the dead-memory state it exists
    # to alarm on. That branch now FAILS; the remaining skips are pinned with
    # their reasons in ``test_smoke_skips.py`` so a new one cannot be added
    # silently.
    #
    # Skips are still invisible in the exit code by design, so they are named
    # in BOTH renderings instead — a reader must be able to see what did not
    # run without diffing the green count against the total.
    passed = not any(r.failed for r in results)

    if json_output:
        rendered = format_json(timestamp, passed, duration, results)
    else:
        rendered = format_pretty(timestamp, passed, duration, results)
    print(rendered)
    # The redirect above keeps CHECK-time prints off stdout, but import-time
    # banners (litellm/composio/mem0 print on import, before run_smoke is even
    # called) can still land ahead of the JSON in a `> file` redirect — the
    # 2026-08-18 recurrence of "smoke output missing or malformed". CI now
    # parses this directly-written file instead of stdout, so no stray print
    # anywhere in the process can corrupt the machine-readable result.
    if out_path:
        try:
            Path(out_path).write_text(rendered + "\n", encoding="utf-8")
        except Exception:  # noqa: BLE001 — stdout copy above still stands
            logger.warning("could not write smoke output to %s", out_path, exc_info=True)

    return passed
