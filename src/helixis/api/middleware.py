"""FastAPI middleware — CORS, request ID, security headers, error handling."""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from helixis.analytics import flush_posthog
from helixis.common.errors import ConfirmationDeniedError, HelixisError, SessionNotFoundError
from helixis.observability.build_id import BUILD_HEADER, short_build_id
from helixis.settings import Settings

logger = logging.getLogger(__name__)

# The ADR 0003 client origins — the deployed frontends whose CORS access IS
# the cross-repo contract. Code-pinned so no configuration source can
# silently drop one: a stale env var overriding the YAML did exactly that
# to the onboarding wizard (#477 — preflight 400 from 2026-07-04 while the
# repo config was correct the whole time). Env/YAML values EXTEND this set;
# they can never subtract from it. Retiring a client means changing ADR
# 0003 first, then this constant.
CONTRACT_ORIGINS = (
    "https://your-app.vercel.app",
    "https://your-onboarding.vercel.app",
    # Occupella brand domains (2026-08-18): apex + www serve the main web
    # app, setup.* serves the onboarding wizard. The .vercel.app origins
    # stay allowed for the transition and for Vercel-internal traffic.
    "https://occupella.com",
    "https://www.occupella.com",
    "https://setup.occupella.com",
    # ⚠ ADDED 2026-09-15 AHEAD OF THE HOSTS IT DESCRIBES, DELIBERATELY. The
    # apex and setup.* are about to trade places — the marketing site (which
    # has the landing, pricing, features and legal pages) moves to
    # occupella.com, and the web app moves to app.occupella.com. Allowing an
    # origin nothing serves yet costs nothing; allowing it AFTER the DNS move
    # costs the whole app, because every API call from the new host fails
    # preflight with no error a user can read and no error in Sentry either —
    # the browser refuses the response before our code sees the request.
    #
    # So this lands and deploys FIRST, and the Vercel reassignment second.
    # The matching half is the Supabase Redirect URLs allowlist (Auth → URL
    # Configuration), which is a dashboard setting nothing here can reach:
    # `services/auth.ts` derives every redirect from `window.location.origin`
    # so it follows the new host automatically, but GoTrue silently falls back
    # to the Site URL for a host it does not recognise — the reset mail
    # arrives, the link "works", and the user lands signed in having never
    # been asked to set a password. Confirmed present 2026-09-15.
    "https://app.occupella.com",
)


def setup_middleware(app: FastAPI, settings: Settings) -> None:
    """Register all middleware on the FastAPI app.

    ⚠ ORDER MATTERS. Starlette wraps middleware LIFO — the LAST one added is
    the OUTERMOST. An unhandled exception in a route propagates UP as an
    exception, not a response, and ``CORSMiddleware`` only stamps its
    ``Access-Control-Allow-Origin`` header onto an actual RESPONSE. So if CORS
    sits *inside* the failure point, an unhandled 500 reaches the browser with
    NO CORS header, the browser blocks it, and the frontend sees only "failed
    to fetch" — the real error (a stack trace, a UniqueViolation) is invisible
    to the user and often to us too. That masking class bit ``/auth/bootstrap``
    and ``/buildium/test`` live within one week (#63).

    Fix = two cooperating pieces, added in this order:
      1. ``catch_unhandled_exceptions`` (added FIRST → innermost) turns any
         unhandled route exception into a JSONResponse (and forwards it to
         Sentry, which our catch would otherwise short-circuit);
      2. ``CORSMiddleware`` (added LAST → outermost) then stamps its headers
         onto that 500 on the way out, so the browser accepts it and the
         frontend can render the real error instead of "failed to fetch".
    """

    # (1) Catch-all — added FIRST so it's the innermost user middleware,
    # wrapping the router. FastAPI's ExceptionMiddleware (inside this) has
    # already converted HTTPException / HelixisError to responses, so anything
    # that reaches this ``except`` is a genuine unhandled 500.
    @app.middleware("http")
    async def catch_unhandled_exceptions(request: Request, call_next) -> Response:
        try:
            response: Response = await call_next(request)
            return response
        except Exception as exc:  # noqa: BLE001 — convert to a CORS-stampable 500
            request_id = getattr(request.state, "request_id", None)
            logger.exception(
                "unhandled exception on %s %s (request_id=%s)",
                request.method,
                request.url.path,
                request_id,
            )
            # Sentry's Starlette integration captures at the OUTER
            # ServerErrorMiddleware boundary, which this catch short-circuits —
            # so forward it explicitly or the 500 would never reach Sentry.
            from helixis.observability.sentry_setup import capture_unhandled

            capture_unhandled(exc, op=f"{request.method} {request.url.path}", request_id=request_id)
            return JSONResponse(
                status_code=500,
                content={
                    "error": "internal_server_error",
                    "message": "An unexpected error occurred. Please try again.",
                    "request_id": request_id,
                },
            )

    # Request ID + security headers + rate-limit header propagation.
    # Single middleware so there's one ASGI pass for all three concerns.
    # Added AFTER the catch-all so it wraps it: request_id is set here on the
    # way in (available to the catch-all), and security headers land on the
    # catch-all's 500 response on the way out.
    @app.middleware("http")
    async def security_and_request_id(request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.request_id = request_id

        response: Response = await call_next(request)

        response.headers["X-Request-ID"] = request_id

        # Which build answered. The web app bakes its own commit in and can
        # now tell whether it is talking to the same one — so a 404 from an
        # endpoint that moved under a stale tab stops reading as "that record
        # was deleted". Cached; see build_id's note on why that matters here
        # and did not for Sentry.
        response.headers[BUILD_HEADER] = short_build_id()

        # Security headers on every response.
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        # XSS-Protection is intentionally disabled: modern browsers ignore it
        # and the legacy implementation could itself be exploited (OWASP).
        response.headers["X-XSS-Protection"] = "0"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"

        # HSTS only in production — requires HTTPS end-to-end and
        # "preload" is a one-way commitment we don't want in dev.
        if settings.env == "production":
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains; preload"
            )

        # Propagate rate-limit headers stashed by the route dependency.
        rate_limit_headers = getattr(request.state, "rate_limit_headers", None)
        if rate_limit_headers:
            for key, value in rate_limit_headers.items():
                response.headers[key] = value

        return response

    # PostHog flush — runs after every request so buffered events ship
    # promptly without blocking request latency on network I/O
    # (posthog-python's flush uses a background thread internally).
    if settings.posthog.enabled and settings.posthog.flush_per_request:

        @app.middleware("http")
        async def posthog_flush(request: Request, call_next) -> Response:
            response: Response = await call_next(request)
            flush_posthog()
            return response

    # CORS — added LAST so it's the OUTERMOST middleware and stamps its
    # headers onto EVERY response, including the catch-all's 500 (see the
    # ordering note at the top of this function). ``cors_origin_regex`` covers
    # Vercel preview deployments (URLs change per commit, e.g.
    # occupella-abc123-…vercel.app), while ``cors_origins`` pins the
    # explicit hosts — always including the ADR 0003 contract origins,
    # whatever env/YAML resolved to.
    configured = list(settings.api.cors_origins)
    missing = [o for o in CONTRACT_ORIGINS if o not in configured]
    if missing:
        # Loud on purpose: the configured list drifting from the contract
        # is exactly the failure #477 hid for days.
        logger.warning(
            "CORS config was missing ADR-0003 contract origin(s) %s — adding them (configured: %s)",
            missing,
            configured,
        )
    cors_kwargs: dict = {
        "allow_origins": configured + missing,
        "allow_credentials": True,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
        # ⚠ `allow_headers` is about the REQUEST. A response header is not
        # readable by browser JS cross-origin unless it is named HERE — it
        # arrives on the wire, DevTools shows it, and `res.headers.get(...)`
        # returns null with no error to explain why. The wildcard is no help
        # either: `Access-Control-Expose-Headers: *` is ignored whenever the
        # request carries credentials, which every authenticated call here
        # does. So both of these are named explicitly.
        #
        # `X-Request-ID` was stamped on every response from the day this
        # middleware shipped and has been unreadable by the client the whole
        # time — the id a support conversation turns on.
        "expose_headers": [BUILD_HEADER, "X-Request-ID"],
    }
    if settings.api.cors_origin_regex:
        cors_kwargs["allow_origin_regex"] = settings.api.cors_origin_regex
    app.add_middleware(CORSMiddleware, **cors_kwargs)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """FastAPI's 422, without the values that were sent.

        ⚠ The default handler puts each rejected field's ``input`` in the
        body, and for a body-level error that is the whole body: measured
        2026-10-05, a Buildium credentials save missing ``client_id`` answered
        with the ``client_secret`` in plain text, and Rentvine's with the
        ``api_secret``. ``SecretStr`` does not help: the input is echoed
        before the model is built. The web app reads only ``loc`` from a 422
        (``frontend/src/services/api.ts`` ``validationFields``), so ``loc``,
        ``msg`` and ``type`` stay and everything else (``input``, ``ctx``,
        ``url``) goes.
        """
        detail = [
            {key: error[key] for key in ("loc", "msg", "type") if key in error}
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": detail})

    # Error handler for Helixis exceptions
    @app.exception_handler(HelixisError)
    async def helixis_error_handler(request: Request, exc: HelixisError) -> JSONResponse:
        status_code = 500
        if isinstance(exc, SessionNotFoundError):
            status_code = 404
        elif isinstance(exc, ConfirmationDeniedError):
            status_code = 403

        return JSONResponse(
            status_code=status_code,
            content={"error": type(exc).__name__, "message": str(exc)},
        )
