"""Product analytics — PostHog event capture.

``configure_posthog(settings)`` initializes a module-level PostHog client
at startup. All capture helpers below are no-ops when the client is not
configured, so analytics can never break the app.
"""

from helixis.analytics.posthog_client import (
    capture,
    configure_posthog,
    feature_enabled,
    flush_posthog,
    get_posthog,
    group_identify,
    identify,
    is_active,
    shutdown_posthog,
)

__all__ = [
    "capture",
    "configure_posthog",
    "feature_enabled",
    "flush_posthog",
    "get_posthog",
    "group_identify",
    "identify",
    "is_active",
    "shutdown_posthog",
]
