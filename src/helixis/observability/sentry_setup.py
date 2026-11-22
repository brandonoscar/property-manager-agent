"""Sentry setup — captures unhandled exceptions from FastAPI routes,
background tasks, and the webhook worker.

Mirrors the pattern used by langfuse_setup / posthog_client:
- configure on startup; no-op if the DSN is missing
- shutdown flushes pending events on teardown
- never let Sentry break the app — every helper swallows errors
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import TYPE_CHECKING, Any, Literal

from helixis.settings import Settings

if TYPE_CHECKING:
    from sentry_sdk.types import Event, Hint

logger = logging.getLogger(__name__)

_active: bool = False

#: Field names that hold a credential somewhere in this codebase, beyond
#: ``sentry_sdk.scrubber.DEFAULT_DENYLIST`` (which already has ``password``,
#: ``secret``, ``api_key`` and ``token``). The SDK's scrubber matches a key
#: EXACTLY, not by substring, so ``client_secret`` and ``api_secret`` passed
#: it in plain text: measured 2026-10-05, sentry-sdk 2.71.0, a 200 on the
#: Buildium and Rentvine credential saves put the secret in the sampled
#: transaction. Request bodies and frame variables are no longer sent at all
#: (``configure_sentry``); this list masks the same names in the dicts that
#: still are (headers, span data, breadcrumbs, extras).
SENSITIVE_KEYS: tuple[str, ...] = (
    # Buildium, Propertyware
    "client_id",
    "client_secret",
    # Rentvine
    "api_secret",
    "webhook_secret",
    "signing_secret",
    # Rent Manager
    "username",
    "partner_token",
    # OAuth and provider tokens
    "access_token",
    "refresh_token",
    "id_token",
    "auth_token",
    # Their stored ciphertexts: a ciphertext and the data key are a secret
    "encrypted_client_id",
    "encrypted_client_secret",
    "encrypted_api_key",
    "encrypted_api_secret",
    "encrypted_username",
    "encrypted_password",
    "encrypted_partner_token",
    "encrypted_webhook_secret",
)


#: Keys whose value is a URL's query string, wherever the SDK puts one: the
#: request (``query_string``) and the HTTP spans and breadcrumbs.
_QUERY_KEYS = frozenset({"query_string", "http.query", "url.query"})

#: The SDK's own placeholder for a removed value.
_FILTERED = "[Filtered]"


def _filtered_query(query: str) -> str:
    """A query string with each value replaced and each name kept."""
    names = (part.split("=", 1)[0] for part in query.split("&") if part)
    return "&".join(f"{name}={_FILTERED}" for name in names)


def _filter_queries(node: Any) -> None:
    """Replace the values of every query string under ``node``, in place."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _QUERY_KEYS and isinstance(value, str):
                node[key] = _filtered_query(value)
            else:
                _filter_queries(value)
    elif isinstance(node, list):
        for item in node:
            _filter_queries(item)


def strip_query_values(event: Event, hint: Hint) -> Event:
    """The event with every query string's values filtered, names kept.

    ⚠ The scrubber reads the keys of a dict and never looks inside a query
    string, and the SDK records an outbound call's query unsanitized
    (``parse_url(..., sanitize=False)`` in its HTTP integrations): the Google
    geocoder's ``key=``, Mapbox's ``access_token=`` and a signed storage URL's
    ``token=`` all went to Sentry with every sampled trace. Inbound, the
    websocket's ``?token=`` is a person's session. The names stay, so a trace
    still shows which parameters a call sent.

    Used as ``before_send`` and ``before_send_transaction``. If it raises, the
    SDK drops the event rather than send it unfiltered.
    """
    _filter_queries(event)
    return event


def _resolve_release_sha() -> str:
    """Return a git SHA to tag Sentry events with, or ``"unknown"``.

    Resolution order:
      1. ``HELIXIS_RELEASE_SHA`` — explicit override for any environment.
      2. ``RENDER_GIT_COMMIT`` — Render auto-injects this on git-based services.
      3. ``git rev-parse HEAD`` — local dev fallback when neither env var is set.
      4. ``"unknown"`` — surfaced in the Sentry UI as ``release: unknown``,
         which is operationally more useful than omitting the tag entirely:
         it tells the triager "the SHA resolver failed in this environment"
         instead of leaving them wondering whether the deploy just wasn't
         tagged at all.

    Never raises. Sentry's release tag is "did this start with deploy X?"
    metadata — useful to have, never important enough to fail boot over.
    """
    for var in ("HELIXIS_RELEASE_SHA", "RENDER_GIT_COMMIT"):
        value = (os.environ.get(var) or "").strip()
        if value:
            return value
    try:
        # ``check=False`` + timeout so a broken git install / missing
        # .git directory / detached environment never blocks startup.
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            if sha:
                return sha
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return "unknown"


def configure_sentry(settings: Settings) -> bool:
    """Initialize Sentry. Returns True if active, False (with a single
    log line) when disabled or the DSN is missing.
    """
    global _active

    cfg = settings.sentry
    if not cfg.enabled:
        logger.info("Sentry disabled in settings — error tracking off")
        _active = False
        return False

    if not cfg.dsn:
        logger.warning("Sentry DSN not configured (HELIXIS_SENTRY__DSN) — error tracking off")
        _active = False
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.asyncio import AsyncioIntegration
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
        from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber

        release = _resolve_release_sha()
        sentry_sdk.init(
            dsn=cfg.dsn,
            environment=settings.env,
            release=release,
            traces_sample_rate=cfg.traces_sample_rate,
            # Explicit even though the current SDK default is 0 — a
            # future SDK could change the default and start profiling
            # silently. Profiling implies CPU sampling we don't want
            # billed against our quota.
            profiles_sample_rate=0.0,
            send_default_pii=cfg.send_default_pii,
            # ⚠ Never a request body. The routes that save credentials take
            # them in the body, and the SDK attached it to every sampled
            # transaction and error. Nothing triaged in Sentry needs a body.
            max_request_body_size="never",
            # ⚠ Never a frame's local variables. On a 500 they carried the raw
            # request body (FastAPI's ``body_bytes``) and each parsed model's
            # repr, so a failed credential save sent the secrets whatever its
            # field was called, ``SecretStr`` or not (measured 2026-10-05). A
            # denylist cannot name every variable a secret passes through. The
            # stack itself (file, function, line, source) still goes.
            include_local_variables=False,
            # Recursive, so a credential nested in a dict is masked too, not
            # only a top-level key.
            event_scrubber=EventScrubber(
                denylist=[*DEFAULT_DENYLIST, *SENSITIVE_KEYS],
                recursive=True,
                send_default_pii=cfg.send_default_pii,
            ),
            before_send=strip_query_values,
            before_send_transaction=strip_query_values,
            integrations=[
                FastApiIntegration(),
                StarletteIntegration(),
                AsyncioIntegration(),
            ],
        )
        _active = True
        logger.info(
            "Sentry error tracking enabled (env=%s, release=%s)",
            settings.env,
            release,
        )
        return True

    except Exception:  # noqa: BLE001 — never let error tracking break the app
        logger.exception("Failed to configure Sentry — error tracking off")
        _active = False
        return False


def capture_unhandled(exc: BaseException, *, op: str, request_id: str | None = None) -> None:
    """Report an exception the request layer is about to turn into a 500.

    The CORS-on-500 middleware (``api/middleware.py``) catches unhandled route
    exceptions and returns a JSONResponse so CORS headers get stamped on the
    way out (#63). But catching short-circuits Sentry's Starlette integration,
    which only captures at the outer ``ServerErrorMiddleware`` boundary — so
    without this the 500 would reach the user but never reach Sentry. This
    forwards it explicitly, tagged so it groups apart from swallowed
    degradations. Never raises; no-op when Sentry is off.
    """
    if not _active:
        return
    try:
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            scope.set_tag("unhandled", "true")
            scope.set_tag("operation", op)
            if request_id:
                scope.set_tag("request_id", request_id)
            sentry_sdk.capture_exception(exc)
    except Exception:  # noqa: BLE001 — observability must never break the caller
        logger.debug("Sentry capture_unhandled failed (non-fatal)", exc_info=True)


def shutdown_sentry() -> None:
    """Flush buffered events on teardown so nothing in-flight is lost."""
    if not _active:
        return
    try:
        import sentry_sdk

        sentry_sdk.get_client().flush(timeout=2.0)
    except Exception:
        logger.debug("Sentry flush failed (non-fatal)", exc_info=True)


def is_active() -> bool:
    return _active


def capture_swallowed(exc: BaseException, *, op: str) -> None:
    """Report an exception that a fail-open path is about to SWALLOW.

    The memory layer deliberately degrades instead of raising (memory is an
    enhancement, never load-bearing — gotcha 7), which is correct for a chat
    turn but means degradation is invisible between daily smoke runs — up to
    ~24h of silent ``[]`` recall, which is exactly wrong during a
    design-partner week (P2, task #34). This forwards the swallowed exception
    to Sentry tagged with the operation, WITHOUT changing the degrade
    behavior. Never raises; no-op when Sentry is off.
    """
    if not _active:
        return
    try:
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            scope.set_tag("swallowed", "true")
            scope.set_tag("operation", op)
            sentry_sdk.capture_exception(exc)
    except Exception:  # noqa: BLE001 — observability must never break the caller
        logger.debug("Sentry capture_swallowed failed (non-fatal)", exc_info=True)


# The severities Sentry's own ``capture_message`` accepts.
#
# ⚠ Declared rather than typed ``str``. `level: str` type-checks fine here and
# fails INSIDE this module at the `capture_message` call, which is what broke
# CI on #999 — the error names a line the author of the mistake never touched.
# Matching Sentry's Literal moves the failure to the CALLER, where a wrong
# severity is actually written.
#
# ⚠ A comment, not a docstring: a bare string BEFORE an assignment is a no-op
# expression, not documentation attached to anything.
NoticeLevel = Literal["fatal", "critical", "error", "warning", "info", "debug"]


def capture_notice(
    message: str,
    *,
    op: str,
    level: NoticeLevel = "info",
    tags: dict[str, str] | None = None,
) -> None:
    """Report something that HAPPENED and that a human should see — no exception.

    The two captures above both start from an exception, which means the only
    things that reach Sentry are failures. A customer filing a bug report is
    not a failure and was reaching nobody: the row landed in `helixis_feedback`
    and no code anywhere reads that table, so the only way to see a complaint
    was a SQL console somebody had to think to open.

    ⚠ **The `message` is a POINTER, never the person's own words.** That is the
    same line `feedback.py`'s PostHog capture already draws and `codeexec/audit`
    draws for source code: Sentry is third-party telemetry, and a customer's
    prose about their own portfolio does not belong in it. Callers pass what
    identifies the record; an operator reads the record.

    ⚠ It reaches Sentry. Whether it NOTIFIES anybody is a Sentry alert-rule
    setting, which lives in the console and not in this repo — so this closes
    "the report goes nowhere" and does not by itself close "somebody is paged".

    Never raises; no-op when Sentry is off.
    """
    if not _active:
        return
    try:
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            scope.set_tag("notice", "true")
            scope.set_tag("operation", op)
            for key, value in (tags or {}).items():
                scope.set_tag(key, value)
            sentry_sdk.capture_message(message, level=level)
    except Exception:  # noqa: BLE001 — observability must never break the caller
        logger.debug("Sentry capture_notice failed (non-fatal)", exc_info=True)


__all__ = [
    "SENSITIVE_KEYS",
    "capture_notice",
    "capture_swallowed",
    "capture_unhandled",
    "configure_sentry",
    "is_active",
    "shutdown_sentry",
    "strip_query_values",
]
