"""Debug endpoints for verifying observability wiring.

Token-gated test surfaces. Not on the ``/dev`` router because the
``/dev`` flags belong to staff inspection tooling; ``/debug/*`` is
for "throw a known event so we can confirm the pipeline carried it"
checks. The two have different audiences and different access
control (session auth vs. shared secret), so they live in
different modules.

Current contents:
  * ``GET /debug/sentry?token=<TOKEN>`` — raise a deterministic
    test exception so we can confirm Sentry is configured and
    actually receiving events end-to-end. Token must match the
    ``SENTRY_TEST_TOKEN`` env var. When the env var is unset OR the
    token mismatches, the route returns 404 so the endpoint's
    existence isn't leaked to scanners.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug", tags=["debug"])


class SentryTestError(RuntimeError):
    """Raised by ``GET /debug/sentry`` to verify Sentry ingestion.

    Distinct exception type so the Sentry issue dashboard has a stable
    grouping for these test events and they don't pollute real-bug
    triage.
    """


@router.get("/sentry")
async def trigger_sentry_test(
    token: str = Query(default=""),
) -> None:
    """Raise :class:`SentryTestError` when ``token`` matches the
    server-side ``SENTRY_TEST_TOKEN`` env var. Otherwise 404.

    The unhandled exception propagates to FastAPI's exception
    machinery, which Sentry's ``FastApiIntegration`` captures and
    forwards. The caller sees a 500 — that's the signal that the
    chain worked.
    """
    expected = (os.environ.get("SENTRY_TEST_TOKEN") or "").strip()
    if not expected or token != expected:
        # 404, not 401 — don't reveal that the route exists to an
        # attacker who probes without the right token.
        raise HTTPException(status_code=404)
    logger.warning("Sentry test exception triggered via /debug/sentry")
    raise SentryTestError("Sentry test exception (intentional)")
