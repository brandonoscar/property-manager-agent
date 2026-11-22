"""PostHog singleton client for server-side product analytics.

Mirrors the pattern in observability/langfuse_setup.py:
- configure on startup; no-op if keys missing
- flush per request via middleware
- shutdown flushes on teardown
- every helper swallows exceptions so analytics can't take the app down

All capture helpers attach the ``company`` group when ``company_id`` is
passed, which is what drives per-company dashboards in PostHog.
"""

from __future__ import annotations

import logging
from typing import Any

from helixis.settings import Settings

logger = logging.getLogger(__name__)

_client: Any = None  # posthog.Posthog instance once configured
_active: bool = False


def configure_posthog(settings: Settings) -> bool:
    """Initialize the PostHog client.

    Returns True if analytics is active. Returns False (with a single log
    line) if disabled or the project API key is missing — the rest of
    the backend keeps working unchanged.
    """
    global _client, _active

    cfg = settings.posthog
    if not cfg.enabled:
        logger.info("PostHog disabled in settings — analytics off")
        _active = False
        return False

    if not cfg.project_api_key:
        logger.warning(
            "PostHog project_api_key not configured "
            "(HELIXIS_POSTHOG__PROJECT_API_KEY) — analytics off"
        )
        _active = False
        return False

    try:
        from posthog import Posthog

        _client = Posthog(
            project_api_key=cfg.project_api_key,
            host=cfg.host,
            debug=settings.env == "development",
        )
        _active = True
        logger.info("PostHog analytics enabled → %s", cfg.host)
        return True

    except Exception:  # noqa: BLE001 — never let analytics break the app
        logger.exception("Failed to configure PostHog — analytics off")
        _client = None
        _active = False
        return False


def shutdown_posthog() -> None:
    """Blocking flush + shutdown so in-flight events aren't lost."""
    global _client, _active
    if not _active or _client is None:
        return
    try:
        _client.shutdown()
        logger.info("PostHog shutdown complete")
    except Exception:
        logger.debug("PostHog shutdown failed (non-fatal)", exc_info=True)
    finally:
        _client = None
        _active = False


def flush_posthog() -> None:
    """Flush buffered events. Called by middleware after each request."""
    if _active and _client is not None:
        try:
            _client.flush()
        except Exception:
            logger.debug("PostHog flush failed (non-fatal)", exc_info=True)


def get_posthog() -> Any:
    """Return the PostHog client, or None if not configured."""
    return _client if _active else None


def is_active() -> bool:
    """Return whether PostHog was successfully configured."""
    return _active


# ── Convenience capture helpers ──────────────────────────────────────


def capture(
    distinct_id: str,
    event: str,
    properties: dict[str, Any] | None = None,
    *,
    company_id: str = "",
) -> None:
    """Capture an analytics event. No-op if PostHog is not configured.

    Args:
        distinct_id: Stable user identifier (matches Supabase Auth sub).
        event: Event name in "object verb" form (e.g. "agent run started").
        properties: Extra event properties.
        company_id: If provided, attaches the event to the PM company group.
    """
    if not _active or _client is None:
        return
    try:
        groups = {"company": company_id} if company_id else {}
        _client.capture(
            distinct_id=distinct_id,
            event=event,
            properties=properties or {},
            groups=groups,
        )
    except Exception:
        logger.debug("PostHog capture failed for %s (non-fatal)", event, exc_info=True)


def identify(
    distinct_id: str,
    properties: dict[str, Any] | None = None,
) -> None:
    """Identify a user with person properties. No-op if not configured."""
    if not _active or _client is None:
        return
    try:
        _client.identify(distinct_id, properties or {})
    except Exception:
        logger.debug("PostHog identify failed (non-fatal)", exc_info=True)


def group_identify(
    group_type: str,
    group_key: str,
    properties: dict[str, Any] | None = None,
) -> None:
    """Create or update a group (e.g. a PM company). No-op if not configured."""
    if not _active or _client is None:
        return
    try:
        _client.group_identify(group_type, group_key, properties or {})
    except Exception:
        logger.debug("PostHog group_identify failed (non-fatal)", exc_info=True)


# ── Onboarding funnel helpers ────────────────────────────────────────
# Server-side mirrors of the frontend funnel events. Used from API
# routes when the relevant action is initiated by the backend (e.g. the
# Supabase signup webhook fires `signup_completed`; the connector POST
# endpoint fires `api_key_added` after a successful key validation).


def capture_signup_started(distinct_id: str, *, method: str = "email") -> None:
    """Fired the moment a user starts the signup flow.

    The frontend version of this lives in ``frontend/src/services/analytics.ts``
    as ``captureSignupStarted``. The two paths are intentionally
    separate so the event can be fired from whichever side knows first
    (front-end click vs. backend webhook).
    """
    capture("anonymous" if not distinct_id else distinct_id, "signup_started", {"method": method})


def capture_signup_completed(
    distinct_id: str,
    *,
    method: str = "email",
    company_id: str = "",
) -> None:
    """Fired after the new user lands signed-in for the first time."""
    capture(
        distinct_id,
        "signup_completed",
        {"method": method, "company_id": company_id},
        company_id=company_id,
    )


def capture_api_key_added(
    distinct_id: str,
    *,
    connector: str,
    company_id: str = "",
) -> None:
    """Fired after a connector key is saved + validated."""
    capture(
        distinct_id,
        "api_key_added",
        {"connector": connector},
        company_id=company_id,
    )


def capture_webhook_configured(
    distinct_id: str,
    *,
    provider: str,
    company_id: str = "",
) -> None:
    """Fired after a new webhook is created and validated."""
    capture(
        distinct_id,
        "webhook_configured",
        {"provider": provider},
        company_id=company_id,
    )


def feature_enabled(
    flag_key: str,
    distinct_id: str,
    *,
    default: bool = False,
    company_id: str = "",
) -> bool:
    """Evaluate a feature flag. Returns ``default`` if PostHog is not
    configured or the evaluation fails.
    """
    if not _active or _client is None:
        return default
    try:
        groups = {"company": company_id} if company_id else {}
        result = _client.feature_enabled(
            flag_key,
            distinct_id,
            groups=groups,
        )
        return bool(result) if result is not None else default
    except Exception:
        logger.debug(
            "PostHog feature_enabled failed for %s (non-fatal)",
            flag_key,
            exc_info=True,
        )
        return default
