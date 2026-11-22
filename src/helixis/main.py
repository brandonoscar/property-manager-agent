"""FastAPI application factory — wires all subsystems together."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, WebSocket

from helixis.analytics import configure_posthog, shutdown_posthog
from helixis.api.middleware import setup_middleware
from helixis.api.rate_limit import apply_settings as apply_rate_limits
from helixis.api.routes import (
    a2p,
    activity,
    admin,
    agent,
    billing,
    buildium,
    chat,
    company,
    compliance,
    connectors,
    crm,
    debug,
    dev,
    documents,
    doorloop,
    feedback,
    health,
    maps,
    mfa,
    mirror,
    operations,
    properties,
    propertyware,
    reminders,
    renewals,
    rentmanager,
    rentvine,
    reports,
    search,
    sms,
    standing_tasks,
    trust,
    turnovers,
    twilio,
)
from helixis.api.routes import (
    auth as auth_route,
)
from helixis.browser.rpc import handle_browser_websocket
from helixis.connectors.composio_client import ComposioClient
from helixis.db.engine import (
    SchemaVerificationError,
    get_pool,
    init_pool,
    shutdown_pool,
    verify_schema,
)
from helixis.llm.router import configure_litellm_retries
from helixis.memory.manager import MemoryManager, set_memory_manager
from helixis.observability import (
    configure_langfuse,
    configure_sentry,
    shutdown_langfuse,
    shutdown_sentry,
)
from helixis.observability.latency import configure_latency
from helixis.observability.tracing import configure_tracer
from helixis.observability.tracing import flush as flush_run_trace
from helixis.settings import (
    Settings,
    assert_no_openai_default,
    get_settings,
    weakened_safety_defaults,
)
from helixis.streaming.bus import EventBus
from helixis.webhooks.rentvine_router import router as rentvine_webhooks_router
from helixis.webhooks.router import router as buildium_webhooks_router

# Without this, Python's root logger stays at WARNING and every
# ``logger.info(...)`` in the helixis package is silently dropped —
# which left Render logs blind to the calendar-tools-loaded
# diagnostics the connector layer emits. ``force=True`` so we win
# over uvicorn's basicConfig if it ran first.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    settings: Settings = app.state.settings

    # Policy gate FIRST — if the reasoning model resolves to OpenAI we
    # refuse to boot. Cheaper to fail loudly here than to discover the
    # 401 on the first inbound webhook.
    assert_no_openai_default(settings)
    for weakened in weakened_safety_defaults(settings):
        logger.warning("safety default off: %s", weakened)

    # Install LiteLLM retry + Gemini-to-Gemini fallback (PR #71). One
    # Vertex 503 used to take down the user's whole chat turn; the
    # retry config catches that class of error before it ever reaches
    # the orchestrator's except block.
    configure_litellm_retries(settings)

    # Per-company daily token ledger (0050) — counts every completion in
    # this process; /agent/run enforces the ceiling. Best-effort by design.
    from helixis.llm.spend import configure_spend_tracking

    configure_spend_tracking()

    # Initialize observability FIRST so every subsequent LLM call is traced.
    # Returns False (and logs a warning) if keys aren't configured — the
    # rest of the app keeps running with tracing simply off.
    app.state.langfuse_enabled = configure_langfuse(settings)
    # Companion run-tracer: emits the tool-call + handoff spans that the
    # LiteLLM callback can't see (same key gate; no-op when off).
    configure_tracer(settings)
    # Latency budgets: resolve SLO thresholds so over-budget runs/tools alert
    # through the tracer + logs. Advisory only; no-op when disabled.
    app.state.latency_enabled = configure_latency(settings)

    # Initialize error tracking. Before PostHog + agent setup so any
    # exception during subsequent init reaches Sentry too.
    app.state.sentry_enabled = configure_sentry(settings)

    # Initialize product analytics (PostHog). Same graceful-fallback pattern.
    app.state.posthog_enabled = configure_posthog(settings)

    # Initialize Postgres pool (used by webhook queue, Buildium persistence).
    # Tolerates missing DSN so the agent API remains usable without Buildium.
    if settings.supabase.db_url:
        try:
            await init_pool(
                settings.supabase.db_url,
                max_size=settings.supabase.pool_max_web,
            )
        except Exception as exc:  # noqa: BLE001 — log + continue degraded
            logger.warning("Postgres pool unavailable: %s", exc)
        else:
            # PR #101 — Pool came up; now gate on schema visibility.
            # If the pool's view of the schema is stale (e.g.
            # PgBouncer backend bindings predate a recent migration),
            # we refuse to boot rather than serve partial-shape
            # responses for the next 30 minutes until somebody
            # notices and restarts the web service.
            if settings.supabase.verify_schema_on_boot:
                from helixis.api.routes.buildium import RECENT_EVENTS_COLUMNS

                try:
                    await verify_schema(
                        get_pool(),
                        {"buildium_events": RECENT_EVENTS_COLUMNS},
                    )
                except SchemaVerificationError:
                    logger.error(
                        "asyncpg pool cannot see required columns — restart the web "
                        "service to drain the pool (Render → Manual Deploy → Restart). "
                        "If the column truly does not exist in the DB, apply the "
                        "relevant migration.",
                        exc_info=True,
                    )
                    raise

    # Reconcile durable confirmations (migration 0022). Any ``pending`` row is
    # from a process that died holding the live wait — this one just started
    # and holds none yet — so mark them ``orphaned`` and let a stale Approve
    # resolve against a truthful record instead of a silent no-op. Best-effort:
    # a failure here must never block boot.
    if settings.supabase.db_url:
        try:
            from helixis.db.repositories.pending_confirmations import store_from_pool

            confirm_store = store_from_pool()
            if confirm_store is not None:
                orphaned = await confirm_store.orphan_all_pending()
                if orphaned:
                    logger.info("reconciled %d orphaned pending confirmation(s) at boot", orphaned)
        except Exception as exc:  # noqa: BLE001 — reconcile is best-effort
            logger.warning("pending-confirmation reconcile skipped: %s", exc)

    # Initialize memory
    memory = MemoryManager(settings)
    await memory.initialize()
    app.state.memory_manager = memory
    set_memory_manager(memory)

    # Warm mem0's spaCy/BM25 model during boot, off the request path. mem0
    # lazy-loads spaCy on the first search() and caches it for the process
    # lifetime (~20s one-time), so without this the FIRST chat turn after a
    # deploy pays that cost. Blocking here is safe — the app isn't serving
    # yet, so this can't freeze a live request the way a mid-turn load would.
    # The throwaway search has no side effects (a sentinel company matches no
    # rows; no searched_by → no audit row). Best-effort: a failure — including
    # spaCy's SystemExit when a model can't load — must never break startup.
    if memory.available:
        try:
            await asyncio.wait_for(
                memory.search("warmup", company_id="__warmup__"),
                timeout=60,
            )
            logger.info("memory warmup complete (spaCy/BM25 model loaded at boot)")
        except (Exception, SystemExit):  # noqa: BLE001 — warmup is best-effort
            logger.warning("memory warmup skipped (non-fatal)", exc_info=True)

    # Initialize Composio
    composio = ComposioClient(settings)
    await composio.initialize()
    app.state.composio_client = composio

    # Single structured "what's actually wired up" snapshot. One grep on
    # ``helixis startup complete`` against Render logs tells you which
    # integrations are live without having to chase per-subsystem
    # warnings scattered across the boot sequence.
    logger.info(
        "helixis startup complete",
        extra={
            "langfuse": bool(
                settings.langfuse.enabled
                and settings.langfuse.public_key
                and settings.langfuse.secret_key
            ),
            "posthog": bool(settings.posthog.project_api_key),
            "memory": memory.available,
            "composio": bool(settings.composio.api_key),
            "supabase": bool(settings.supabase.url),
            "buildium": bool(settings.buildium.client_id),
            "db_pool": bool(settings.supabase.db_url),
        },
    )
    logger.info("Helixis backend started on %s:%s", settings.api.host, settings.api.port)
    yield

    # Shutdown — deploy-drain cleanup BEFORE the pool closes. Every merge to
    # main is a Render deploy, so restarts are routine, not rare: any confirm
    # gate this process was awaiting is dead the moment we exit. Release its
    # state deterministically NOW instead of leaving the user to a 15-minute
    # reaper wait:
    #   * parked idempotency claims (awaiting_approval — execute() provably
    #     never started) → failed_retryable, so the user's re-run just works;
    #   * pending confirmation rows → orphaned, so a stale Approve resolves
    #     against a truthful record (mirrors the boot-time reconcile above).
    # Best-effort: shutdown must never hang on cleanup.
    if settings.supabase.db_url:
        try:
            from helixis.db.repositories.action_idempotency import PgIdempotencyStore
            from helixis.db.repositories.pending_confirmations import store_from_pool

            released = await PgIdempotencyStore(get_pool()).release_awaiting()
            if released:
                logger.info("released %d parked (awaiting-approval) claim(s) at shutdown", released)
            confirm_store = store_from_pool()
            if confirm_store is not None:
                orphaned = await confirm_store.orphan_all_pending()
                if orphaned:
                    logger.info("orphaned %d pending confirmation(s) at shutdown", orphaned)
        except Exception as exc:  # noqa: BLE001 — cleanup is best-effort
            logger.warning("shutdown drain cleanup skipped: %s", exc)

    shutdown_posthog()
    shutdown_sentry()
    shutdown_langfuse()
    flush_run_trace()
    await shutdown_pool()
    logger.info("Helixis backend shutting down")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if settings is None:
        settings = get_settings()

    app = FastAPI(
        title="Occupella",
        description="AI-native agent system for property managers",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Store settings and event bus on app state
    app.state.settings = settings
    app.state.event_bus = EventBus()

    # Reconfigure the module-level rate limiters from YAML / env values.
    # Routes import the limiters directly; this keeps their call sites
    # simple while still letting the tiers be operator-controlled.
    apply_rate_limits(settings.api.rate_limits)

    # Middleware
    setup_middleware(app, settings)

    # Routes
    app.include_router(a2p.router, prefix="/api/v1")
    app.include_router(billing.router, prefix="/api/v1")
    app.include_router(mirror.router, prefix="/api/v1")
    app.include_router(activity.router, prefix="/api/v1")
    app.include_router(admin.router, prefix="/api/v1")
    app.include_router(agent.router, prefix="/api/v1")
    app.include_router(auth_route.router, prefix="/api/v1")
    # Backup codes live beside the rest of auth but in their own module —
    # see api/routes/mfa.py for why redemption is the interesting half.
    app.include_router(mfa.router, prefix="/api/v1")
    app.include_router(chat.router, prefix="/api/v1")
    app.include_router(compliance.router, prefix="/api/v1")
    app.include_router(company.router, prefix="/api/v1")
    app.include_router(connectors.router, prefix="/api/v1")
    app.include_router(buildium.router, prefix="/api/v1")
    # Rentvine credential management — the WRITE half of the connector, which
    # had no route at all until 2026-09-22 (gotcha 153). Mounted regardless of
    # `rentvine.enabled`: the PUT refuses while the connector is dark and the
    # read + disconnect deliberately do not, so a company that connected before
    # a deployment turned the flag off can still see and undo it.
    app.include_router(rentvine.router, prefix="/api/v1")
    app.include_router(rentmanager.router, prefix="/api/v1")  # same posture as Rentvine's
    app.include_router(propertyware.router, prefix="/api/v1")
    app.include_router(doorloop.router, prefix="/api/v1")
    app.include_router(operations.router, prefix="/api/v1")
    app.include_router(properties.router, prefix="/api/v1")
    app.include_router(renewals.router, prefix="/api/v1")
    app.include_router(turnovers.router, prefix="/api/v1")
    app.include_router(feedback.router, prefix="/api/v1")
    app.include_router(reminders.router, prefix="/api/v1")
    app.include_router(reports.router, prefix="/api/v1")
    app.include_router(search.router, prefix="/api/v1")
    app.include_router(standing_tasks.router, prefix="/api/v1")
    # SMS consent management (Stage 3) — JWT-authed, company-scoped CRUD
    # over the helixis_sms_consent ledger + the settings-card status probe.
    app.include_router(sms.router, prefix="/api/v1")
    app.include_router(maps.router, prefix="/api/v1")
    app.include_router(health.router, prefix="/api/v1")
    app.include_router(crm.router, prefix="/api/v1")
    app.include_router(documents.router, prefix="/api/v1")
    app.include_router(dev.router, prefix="/api/v1")
    app.include_router(debug.router, prefix="/api/v1")
    # Public SMS privacy/terms pages a campaign registration cites.
    # ⚠ NO /api/v1 prefix and NO auth on purpose: Twilio and the carrier
    # reviewer fetch these unauthenticated, and a page they cannot read is
    # the rejection the pages exist to prevent (see api/routes/trust.py).
    app.include_router(trust.router)
    # Twilio customer-care SMS webhooks (inbound replies + delivery
    # receipts). NOT JWT-authed — the X-Twilio-Signature verification
    # (fail-closed, against twilio.webhook_base_url) is the authentication
    # mechanism, same stance as the Buildium webhook router below.
    app.include_router(twilio.router, prefix="/api/v1")
    # Buildium webhooks are mounted WITHOUT the /api/v1 prefix; the URL
    # registered with Buildium is ${PUBLIC_BASE_URL}/webhooks/buildium.
    # Do NOT add auth middleware to this router — the HMAC signature is
    # the authentication mechanism.
    app.include_router(buildium_webhooks_router)
    # Rentvine webhooks, same stance and same absence of auth middleware — the
    # signature is the authentication. ⚠ It is mounted while the SCHEME is
    # still unknown, which is safe precisely because the route FAILS CLOSED:
    # with no scheme configured `verify_signature` refuses every delivery
    # before it looks at anything else. Mounting a refusing route is what gives
    # a customer a URL to paste and a 401 to see, instead of a 404 that reads
    # as "Occupella is broken"; and on Rentvine polling is primary, so the
    # mirror stays correct either way.
    app.include_router(rentvine_webhooks_router)

    # WebSocket for browser automation RPC
    from helixis.api.auth import AuthenticatedUser, get_ws_user

    @app.websocket("/browser/ws")
    async def browser_ws(
        websocket: WebSocket,
        session: str = "",
        user: AuthenticatedUser = Depends(get_ws_user),
    ):
        await handle_browser_websocket(websocket, session)

    return app


# Default app instance for uvicorn
app = create_app()


def cli() -> None:
    """CLI entrypoint for `helixis` command."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "helixis.main:app",
        host=settings.api.host,
        port=settings.api.port,
        reload=True,
    )
