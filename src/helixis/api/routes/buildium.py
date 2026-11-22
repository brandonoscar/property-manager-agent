"""Buildium credential management endpoints.

Two flows:
  - **API credentials** — the secrets Helixis needs to *call* Buildium.
    Pasted into the UI, encrypted with the project Fernet key, persisted
    in ``company_buildium_credentials``.
  - **Webhook signing secret** — the secret **Buildium generates** when
    the user creates a webhook subscription on their side. The user
    pastes it into Helixis so we can HMAC-verify inbound events.
    Helixis no longer generates this secret — Buildium owns it.

Plaintext never round-trips through the read API. The masked ``last4``
column powers the read path so the frontend can render ``••••1234``
without a decrypt round-trip.

Phase A.2 adds the inbox-execution surface:
  - ``GET /buildium/recent-events`` accepts ``?include=`` to expand the
    response with ``action_surface`` / ``enrichment`` jsonb columns.
  - ``GET /buildium/events/{event_id}`` returns one row in full.
  - ``POST /buildium/events/{event_id}/actions/{action_index}/execute``
    runs the planner-proposed action via the handler registry.
  - ``POST /buildium/events/{event_id}/freeform`` records the PM's
    free-text follow-up as an activity row (LLM routing is Phase A.3).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from helixis.agent.handlers import HandlerError, get_handler
from helixis.agent.handlers.staleness import revalidate_task
from helixis.agent.narration import narrate_tool
from helixis.agent.planner import ActionPlanner, PlannerError, ProposedAction
from helixis.agent.planner.tool_catalog import mark_unavailable, unavailable_reason
from helixis.agents.idempotency import (
    GuardResult,
    PreSideEffectError,
    derive_key,
    guarded_action,
)
from helixis.agents.orchestrator import run_agent_streaming
from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.rate_limit import connectors_limiter
from helixis.buildium.backfill import run_initial_sync
from helixis.buildium.client import BuildiumClient
from helixis.buildium.credentials import (
    PRODUCTION_BASE_URL,
    SANDBOX_BASE_URL,
    decrypt_buildium_settings_from,
)
from helixis.buildium.errors import BuildiumApiError
from helixis.buildium.purge import purge_buildium_mirror
from helixis.common.events import (
    assistant_message_end_event,
    assistant_message_start_event,
    assistant_message_token_event,
    confirm_gate_event,
    narration_event,
    suggestion_chip_event,
)
from helixis.common.support import contact_support
from helixis.common.types import AgentRequest
from helixis.db.company_pms import DEFAULT_PMS, company_pms, one_pms_refusal
from helixis.db.engine import get_pool
from helixis.db.repositories import ActivityEntry, write_activity
from helixis.db.repositories.action_idempotency import (
    IdempotencyStore,
    PgIdempotencyStore,
)
from helixis.detectors.card_freshness import finding_states_for_events
from helixis.geo.backfill import geocode_company_properties
from helixis.observability.context import trace_context
from helixis.pms.registry import BUILDIUM
from helixis.security import CredentialCipher
from helixis.settings import BuildiumSettings, get_settings
from helixis.streaming import run_registry
from helixis.streaming.sse import sse_event_generator
from helixis.webhooks.inbox_filter import inbox_like_patterns

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/buildium", tags=["buildium"])


# Both Buildium API hosts, defined beside the check that refuses any other
# (``buildium.credentials.BUILDIUM_BASE_URLS``). The frontend toggles between
# them via the ``environment`` field; storing the resolved URL on the row keeps
# the /test endpoint trivial.


class BuildiumCredentialsInput(BaseModel):
    """Plaintext input for upserting Buildium API credentials.

    Per Buildium's developer docs, API auth uses **only** the
    ``x-buildium-client-id`` and ``x-buildium-client-secret`` headers.
    There's no ``account_id`` in any auth flow — that field appears
    only in webhook payloads from Buildium and is irrelevant for our
    single-customer-per-company model. We dropped it from the form.
    """

    # repr=False: Sentry records a failing handler's locals by repr, and this
    # body is one of them (``test_no_secrets_in_repr.py``).
    client_id: str = Field(min_length=4, repr=False)
    client_secret: str = Field(min_length=8, repr=False)
    environment: str = Field(default="sandbox", pattern="^(sandbox|production)$")


class BuildiumCredentialsStatus(BaseModel):
    """Read-side view: never includes any plaintext secrets."""

    configured: bool
    client_id_last4: str | None = None
    webhook_secret_last4: str | None = None
    environment: str | None = None
    last_tested_at: str | None = None
    test_account_name: str | None = None
    test_properties_count: int | None = None
    # Set only on the disconnect response: how many Buildium-derived
    # mirror rows were purged (Buildium ToS §18 — API data must be
    # removed when the license ends). Additive-optional per ADR-0003.
    purged_rows: int | None = None


class BuildiumTestResult(BaseModel):
    ok: bool
    account_name: str | None = None
    properties_count: int | None = None
    error: str | None = None


class WebhookSecretStatus(BaseModel):
    """Status response for GET /webhook-secret.

    The actual secret is never returned over the wire — only whether
    it's set and the last 4 characters for UI confirmation."""

    configured: bool
    last4: str | None = None


class SetWebhookSecretRequest(BaseModel):
    """User pastes the secret Buildium showed them when they created
    the webhook subscription. Min length is 8 chars to catch obvious
    typos; Buildium's actual secrets are typically ~44 base64 chars."""

    secret: str = Field(min_length=8, max_length=512, repr=False)


class InboxEventSummary(BaseModel):
    """PR #92 — the agent-written narrative for one event. Rendered
    at the top of the Inbox detail pane above the field grid. The
    summarizer (``helixis.agent.summarizer``) populates ``text``
    when ``status == 'succeeded'``; other statuses surface as a
    shimmer (pending/retrying) or Resummarize pill (failed) on the
    frontend.

    All fields default to the empty-but-non-failing shape so
    pre-migration deploys (no ``summary_*`` columns on the row)
    render as if the summarizer had succeeded with empty text —
    the inbox short-circuits empty text to "no summary yet" without
    blowing up."""

    status: str = "succeeded"
    text: str | None = None
    error: str | None = None
    attempts: int = 0
    last_attempted_at: str | None = None


class RecentEvent(BaseModel):
    """Default ``/recent-events`` row shape (backwards-compatible).

    Callers can ask for the heavier jsonb columns via the
    ``?include=action_surface,enrichment`` query param — the response
    keeps the same row id but adds ``action_surface`` /
    ``enrichment`` / ``executed_action_index`` when requested. Pydantic
    is configured to drop ``None`` fields on serialisation so the
    skinny call site still sees the original shape verbatim.
    """

    id: str
    event_name: str
    event_time: str
    status: str
    last_error: str | None = None
    # Phase A.2 — present only when the caller requested them via
    # ``?include=``. Pydantic drops them when ``None`` on the wire so
    # the default response shape is byte-identical to the pre-A.2
    # contract.
    action_surface: dict[str, Any] | None = None
    enrichment: dict[str, Any] | None = None
    executed_action_index: int | None = None
    #: For a ``Risk.FlagRaised`` card only: how much of it is STILL TRUE, read
    #: at request time from ``helixis_risk_flags``. A finding heals when the
    #: detector stops emitting it, and nothing ever told the card — measured
    #: 2026-09-09, 35 of 81 risk cards described a problem that no longer
    #: existed and 9 more overstated their count. Absent on every other card
    #: type (Pydantic drops ``None``), so the wire shape is unchanged for them.
    finding_state: dict[str, Any] | None = None
    # ``?include=``-gated like the three above: absent unless asked for, so the
    # default response shape is unchanged for every existing caller.
    execution_result: dict[str, Any] | None = None
    executed_at: str | None = None
    # PR #89 — planner lifecycle state surfaced to the inbox. Always
    # included (cheap text columns, no ``?include`` gate). Defaults
    # to ``'succeeded'`` so old / pre-migration rows render as if the
    # planner ran successfully; the inbox only shimmers / shows the
    # Replan pill when these come through populated by the worker.
    planner_status: str = "succeeded"
    planner_error: str | None = None
    # PR #92 — summarizer lifecycle + text. Always included for the
    # same reason as planner_status (no ``?include`` gate). The
    # inbox detail pane reads this directly off the list response so
    # selecting a row doesn't require a second fetch.
    summary: InboxEventSummary = Field(default_factory=InboxEventSummary)


class FullBuildiumEvent(BaseModel):
    """Shape of ``GET /buildium/events/{event_id}``."""

    id: str
    event_name: str
    event_time: str
    status: str
    last_error: str | None = None
    action_surface: dict[str, Any] | None = None
    enrichment: dict[str, Any] | None = None
    executed_action_index: int | None = None
    executed_at: str | None = None
    executed_by_user_id: str | None = None
    execution_result: dict[str, Any] | None = None
    # PR #89 — planner state (see RecentEvent for rationale).
    planner_status: str = "succeeded"
    planner_error: str | None = None
    # PR #92 — summarizer state (see RecentEvent for rationale).
    summary: InboxEventSummary = Field(default_factory=InboxEventSummary)


class ChainPick(BaseModel):
    """One committed pick on the Phase A.2.5 click-chain.

    The frontend assembles this list as the user clicks through the
    ActionChainModal and posts it back on ``/execute`` so the
    backend can persist what the user clicked through to and the
    activity log can reconstruct the conversation.
    """

    model_config = ConfigDict(extra="forbid")

    prompt: str
    label: str
    parameter_patch: dict[str, Any] = Field(default_factory=dict)


class ExecuteActionRequest(BaseModel):
    """Body of the action-execute endpoint.

    Phase A.2.5: ``chain`` carries the click-chain picks the user made
    in the ActionChainModal. The backend deep-merges each pick's
    ``parameter_patch`` (in order) into the action's base parameters
    BEFORE ``parameter_overrides`` is applied — chain comes first,
    overrides win last.

    PR #70: ``session_id`` is the optional bus channel the client opened
    via ``/events/{event_id}/thread/stream``. When set, the executor
    publishes narration + assistant-turn events on it so a subscribed
    client sees the confirmation streaming in rather than as one
    response-body flash."""

    parameter_overrides: dict[str, Any] | None = None
    freeform_note: str | None = Field(default=None, max_length=4000)
    chain: list[ChainPick] | None = None
    session_id: str | None = None


class ExecuteActionResponse(BaseModel):
    status: str
    result: dict[str, Any]


class FreeformNoteRequest(BaseModel):
    note: str = Field(min_length=1, max_length=4000)
    # The client's live inbox SSE session (``thread:{uuid}`` from
    # ``subscribeInboxThread``). The agent must publish to THIS session or its
    # reply + any confirm_gate land on a channel nobody is watching. Optional
    # for back-compat; falls back to ``webhook:{event_id}``.
    session_id: str | None = None


class FollowupRequest(BaseModel):
    """Body of the Phase A.2.5 deeper-followup endpoint.

    ``previous_picks`` is the chain so far. ``freeform_hint`` is
    optional and carries either:
      * the user-typed text from the modal's "Other…" expander, or
      * the ``followup_hint`` field on the option they just picked
        (when that option had ``needs_followup: true``).
    """

    model_config = ConfigDict(extra="forbid")

    previous_picks: list[ChainPick] = Field(default_factory=list)
    freeform_hint: str | None = Field(default=None, max_length=2000)


# ── Helpers ────────────────────────────────────────────────────────────


def _cipher() -> CredentialCipher:
    return CredentialCipher(get_settings())


def _resolve_base_url(environment: str) -> str:
    return PRODUCTION_BASE_URL if environment == "production" else SANDBOX_BASE_URL


def _environment_from_url(base_url: str) -> str:
    return "production" if base_url == PRODUCTION_BASE_URL else "sandbox"


def _require_company(user: AuthenticatedUser) -> str:
    """Pick the active company for v1: single-membership users only.

    Multi-company will pass the company_id explicitly on every call once
    the UI exposes a company switcher; until then we just assert the
    user has at least one membership.
    """
    if not user.company_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "No company associated with this account. Sign in again, or "
                + contact_support("if that does not fix it.")
            ),
        )
    return user.company_ids[0]


async def _require_admin(
    pool: asyncpg.Pool, user: AuthenticatedUser, company_id: str, *, action: str
) -> None:
    """Admin gate for EVERY Buildium-connection route — reads included.

    The Buildium credentials + webhook secret are a SINGLE per-company row that
    the whole team shares, so a member overwriting/deleting them (or rotating
    the webhook secret) silently breaks event routing and API access for
    everyone. Fails CLOSED: an unverifiable role (no ``company_users`` row) is
    least-privilege and refused (``require_role`` normalizes ``None`` → deny).
    Imported locally to avoid a module-level cycle with ``api.routes.company``.

    ⚠ RAISED manager → ADMIN on 2026-09-04 (founder direction), and WIDENED to
    the reads in the same change. The mutations were already guarded; the two
    GETs were not, so a member could see that Buildium was connected and its
    last-4 — never a secret, but a surface they have no business in. "Owner"
    was asked for and means ADMIN here: ADR 0005 deleted the owner role because
    it collides with a Buildium RENTAL owner, and the ``company_users`` CHECK
    constraint refuses the string outright (roles.py). Do not reintroduce it.

    ⚠ This gate deliberately does NOT cover the inbox routes in this module
    (``followup`` / ``freeform`` / ``replan`` / ``resummarize``). Those re-run
    the planner or summarizer, which costs LLM spend a member can burn, but
    they write nothing to Buildium — ``execute`` is separately guarded and the
    agent's own confirm gates and role floors still apply. Working the Inbox is
    what a member is FOR, so spend-amplification is accepted here rather than
    quietly closed. Revisit if turn burn shows up per-member, not before.
    """
    from helixis.api.roles import ADMIN
    from helixis.api.routes.company import require_role

    await require_role(
        pool,
        user.user_id,
        company_id,
        ADMIN,
        detail=f"Only an admin can {action} the Buildium connection.",
    )


async def _fetch_row(pool: asyncpg.Pool, company_id: str) -> dict[str, Any] | None:
    """Fetch the encrypted credentials row using a caller-supplied pool.

    Kept distinct from :func:`fetch_buildium_row` so the routes can
    continue to receive a pool via dependency injection (which is what
    the existing test suite monkeypatches), while the agent tools call
    the global-pool version in :mod:`helixis.buildium.credentials`.
    """
    row = await pool.fetchrow(
        """
        SELECT
            company_id, encrypted_client_id, encrypted_client_secret,
            client_id_last4, encrypted_webhook_secret, webhook_secret_last4,
            account_id, base_url, last_tested_at
        FROM public.company_buildium_credentials
        WHERE company_id = $1
        """,
        company_id,
    )
    return dict(row) if row else None


def _decrypt_buildium_settings(row: dict[str, Any]) -> BuildiumSettings:
    """Decrypt this company's Buildium credentials using the routes-module
    ``get_settings`` import (which the test suite patches).
    """
    return decrypt_buildium_settings_from(row, get_settings())


def _row_to_status(row: dict[str, Any] | None) -> BuildiumCredentialsStatus:
    if row is None:
        return BuildiumCredentialsStatus(configured=False)
    return BuildiumCredentialsStatus(
        configured=True,
        client_id_last4=row["client_id_last4"],
        webhook_secret_last4=row["webhook_secret_last4"],
        environment=_environment_from_url(row["base_url"]),
        last_tested_at=row["last_tested_at"].isoformat() if row["last_tested_at"] else None,
    )


# ── Routes ─────────────────────────────────────────────────────────────


@router.get("/credentials")
async def get_credentials(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> BuildiumCredentialsStatus:
    """Return masked metadata for the company's saved credentials, or
    ``configured: false`` when no row exists. Never returns plaintext."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="view")
    row = await _fetch_row(pool, company_id)
    return _row_to_status(row)


@router.put("/credentials")
async def upsert_credentials(
    payload: BuildiumCredentialsInput,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> BuildiumCredentialsStatus:
    """Encrypt and upsert the Buildium API credentials for the user's company."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    cipher = _cipher()
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="change")
    base_url = _resolve_base_url(payload.environment)

    # ``account_id`` was made nullable by migration 0005 and is no
    # longer collected from the user — Buildium auth doesn't use it.
    # Existing rows keep whatever value they had; new rows write NULL.
    try:
        await pool.execute(
            """
            INSERT INTO public.company_buildium_credentials (
                company_id, encrypted_client_id, encrypted_client_secret,
                client_id_last4, base_url, last_tested_at
            ) VALUES ($1, $2, $3, $4, $5, NULL)
            ON CONFLICT (company_id) DO UPDATE SET
                encrypted_client_id     = EXCLUDED.encrypted_client_id,
                encrypted_client_secret = EXCLUDED.encrypted_client_secret,
                client_id_last4         = EXCLUDED.client_id_last4,
                base_url                = EXCLUDED.base_url,
                last_tested_at          = NULL
            """,
            company_id,
            cipher.encrypt(payload.client_id),
            cipher.encrypt(payload.client_secret),
            cipher.last4(payload.client_id),
            base_url,
        )
    except asyncpg.exceptions.UniqueViolationError as exc:
        # ⚠ Only the one-PMS trigger raises this here (`assert_one_pms_per_company`,
        # migrations 0100/0103/0107): the company already holds another system's
        # credentials. The primary key cannot, because ON CONFLICT turns a second
        # save into an update, and the trigger does not fire on that update (it
        # is declared `update OF company_id`). Uncaught, it was a 500, which skips
        # CORS, so the browser showed "failed to fetch" instead of the reason.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=one_pms_refusal(BUILDIUM, exc),
        ) from None
    row = await _fetch_row(pool, company_id)
    return _row_to_status(row)


@router.delete("/credentials")
async def delete_credentials(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> BuildiumCredentialsStatus:
    """Tombstone the credentials row AND purge the Buildium-derived
    mirror (Buildium ToS §18: API data must be removed when the API
    license ends — Occupella holds that data for the customer, so
    disconnect carries the obligation). Reconnecting later rebuilds the
    mirror from a fresh initial sync, so nothing is lost that Buildium
    itself doesn't still hold."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="disconnect")
    await pool.execute(
        "DELETE FROM public.company_buildium_credentials WHERE company_id = $1",
        company_id,
    )
    # Purge AFTER the creds are gone (an interleaved webhook can't
    # repopulate — signature resolution fails without the secret). The
    # purge is idempotent; if it raises, the 500 is retryable and the
    # creds are already tombstoned, so the license-end state holds.
    deleted = await purge_buildium_mirror(pool, company_id)
    return BuildiumCredentialsStatus(configured=False, purged_rows=sum(deleted.values()))


@router.post("/test")
async def test_connection(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> BuildiumTestResult:
    """Decrypt the saved API creds and call Buildium with a cheap GET.

    Uses ``GET /rentals?limit=1`` because every Buildium plan exposes
    rentals; ``/administration/accounts/me`` does not exist on every
    plan tier. On success updates ``last_tested_at`` so the UI can
    show a recent-tested green check.

    Side effect: also auto-backfills ``account_id`` from
    ``GET /administration/account`` so inbound webhooks can be routed
    back to this company by ``fetch_buildium_row_by_account_id``. The
    field is nullable (migration 0005) and is no longer collected from
    the user, so this endpoint is the only place it gets populated.
    """
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="test/reconnect")
    row = await _fetch_row(pool, company_id)
    if row is None:
        return BuildiumTestResult(ok=False, error="No saved credentials.")

    bsettings = _decrypt_buildium_settings(row)

    account_id: int | None = None
    try:
        async with BuildiumClient(bsettings) as client:
            properties = await client.request("GET", "/rentals", query={"limit": 1})
            count = len(properties) if isinstance(properties, list) else 0
            account_name = (
                properties[0].get("Name")
                if isinstance(properties, list) and properties and isinstance(properties[0], dict)
                else None
            )

            # Webhook routing depends on a stored account_id. /rentals
            # already passed, so the creds are good — fetching the
            # account info is a best-effort sidecar that must never
            # fail the user-visible test.
            try:
                account_info = await client.request("GET", "/administration/account")
                if isinstance(account_info, dict) and "Id" in account_info:
                    account_id = int(account_info["Id"])
            except (BuildiumApiError, KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "buildium /administration/account failed for company=%s: %s",
                    company_id,
                    exc,
                )
    except BuildiumApiError as exc:
        logger.warning("buildium /test failed for company=%s: %s", company_id, exc)
        return BuildiumTestResult(ok=False, error=f"Buildium rejected the request: {exc}")
    except Exception as exc:  # noqa: BLE001 — surface any unexpected error to the UI
        logger.warning("buildium /test crashed for company=%s: %s", company_id, exc)
        return BuildiumTestResult(ok=False, error=f"Network error: {exc}")

    if (
        row.get("account_id") is not None
        and account_id is not None
        and row["account_id"] != account_id
    ):
        logger.warning(
            "Buildium account_id changed for company=%s: was=%s now=%s — "
            "credentials likely rotated",
            company_id,
            row["account_id"],
            account_id,
        )

    try:
        await pool.execute(
            """
            UPDATE public.company_buildium_credentials
            SET last_tested_at = NOW(),
                account_id = COALESCE($2, account_id)
            WHERE company_id = $1
            """,
            company_id,
            account_id,
        )
    except asyncpg.UniqueViolationError:
        # ``account_id`` is globally unique (company_buildium_credentials_
        # account_id_uidx): one Buildium account maps to exactly one workspace,
        # so webhooks route unambiguously. The creds themselves are VALID here
        # (the /rentals GET above passed) — only the account claim collides,
        # because this Buildium account is already connected to another company.
        # WITHOUT this catch the violation escapes as an unhandled 500, which
        # skips CORSMiddleware and the browser sees an opaque "failed to fetch"
        # instead of the real reason (same trap as /auth/bootstrap). Record the
        # successful cred test, then return a clear, CORS-safe error.
        await pool.execute(
            "UPDATE public.company_buildium_credentials SET last_tested_at = NOW() "
            "WHERE company_id = $1",
            company_id,
        )
        logger.warning(
            "buildium /test: account_id=%s already linked to another workspace "
            "(company=%s) — refusing to double-connect",
            account_id,
            company_id,
        )
        return BuildiumTestResult(
            ok=False,
            error=(
                f"This Buildium account (ID {account_id}) is already connected to a "
                "different Occupella workspace. Each Buildium account can be linked to "
                "one workspace at a time — disconnect it there first, or sign in to "
                "that workspace."
            ),
        )
    # Initial Buildium backfill (initial-sync): creds just validated, so kick a
    # one-time full mirror pull right after onboarding connects Buildium. Fire-
    # and-forget — the UI polls GET /buildium/sync-status; a re-test while the
    # sync is genuinely running, or already 'done', is a no-op so repeated
    # clicks don't restart it.
    #
    # ⚠ A STALE 'syncing' ROW RESTARTS, and leaving it out was the defect.
    # `sync_state.is_stale` exists precisely because this task is fired and
    # forgotten, so a deploy mid-crawl strands the row at 'syncing' for the
    # life of the account — and the old gate (`status in ('pending','failed')`)
    # refused to restart exactly that state. The banner told the customer their
    # import had stopped, they came here and re-tested, and nothing happened,
    # silently. Stale means nothing is going to move it, so a restart is the
    # only thing that can be true (gotchas 102/106: an instruction that cannot
    # change anything is worse than none).
    if account_id is not None:
        try:
            from helixis.db.repositories import sync_state as _sync_state

            existing = await _sync_state.get_sync_state(pool, company_id)
            restart = existing is None or existing.status in ("pending", "failed")
            if not restart and existing is not None:
                restart = _sync_state.is_stale(existing.status, existing.started_at)
            if restart:
                asyncio.create_task(
                    run_initial_sync(bsettings, pool, company_id=company_id, account_id=account_id)
                )
        except Exception:  # noqa: BLE001 — never fail the user-visible test on this
            logger.warning(
                "could not start initial Buildium sync for company=%s",
                company_id,
                exc_info=True,
            )

    return BuildiumTestResult(ok=True, account_name=account_name, properties_count=count)


@router.get("/sync-status")
async def sync_status(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Onboarding-backfill progress for the caller's company (initial-sync).

    The frontend's "Buildium data is still syncing…" banner polls this and
    clears when ``status == 'done'``. ``unknown`` = no sync has been kicked yet
    (e.g. Buildium not connected). Tolerant: a missing table (pre-migration)
    degrades to ``unknown`` rather than 500ing.
    """
    company_id = _require_company(user)
    pool = get_pool()
    from helixis.db.repositories import sync_state as _sync_state

    try:
        state = await _sync_state.get_sync_state(pool, company_id)
    except asyncpg.exceptions.UndefinedTableError:
        logger.warning("buildium_sync_state table not provisioned yet — returning 'unknown'")
        return {
            "status": "unknown",
            "counts": {},
            "started_at": None,
            "finished_at": None,
            "last_error": None,
            "stale": False,
        }
    if state is None:
        return {
            "status": "unknown",
            "counts": {},
            "started_at": None,
            "finished_at": None,
            "last_error": None,
            "stale": False,
        }
    return state.to_public()


class BackfillResponse(BaseModel):
    """Per-stage row counts from a mirror backfill run."""

    ok: bool
    properties: int = 0
    units: int = 0
    tenants: int = 0
    leases: int = 0
    balances: int = 0
    transactions: int = 0
    work_orders: int = 0
    applicants: int = 0
    vendors: int = 0
    owners: int = 0
    tasks: int = 0
    gl_accounts: int = 0
    bills: int = 0
    bank_accounts: int = 0
    errors: list[str] = []
    error: str | None = None


@router.post(
    "/backfill",
    summary="Pull the Buildium account into the cockpit entity mirror",
)
async def run_backfill(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> BackfillResponse:
    """Bulk-pull properties/units/tenants/leases/balances/transactions into
    the 0038 entity mirror (Workstream A). Idempotent — re-running upserts.

    Requires saved credentials AND a stored ``account_id`` (populated by
    ``POST /buildium/test``), because every mirror row is keyed to the
    Buildium account for webhook routing consistency.

    ⚠ **THIS IS THE BUTTON THE STALLED-IMPORT BANNER SENDS PEOPLE TO**, so it
    goes through ``run_initial_sync`` rather than calling ``backfill_company``
    directly. It used to do the latter and wrote no ``buildium_sync_state`` at
    all, so the customer did exactly what the banner told them, watched their
    data arrive, and "your Buildium import stopped before it finished" stayed
    on screen for the life of the account — a working product wearing a
    permanent failure notice.
    """
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="run a backfill for")
    row = await _fetch_row(pool, company_id)
    if row is None:
        return BackfillResponse(ok=False, error="No saved credentials.")
    if row.get("account_id") is None:
        return BackfillResponse(
            ok=False,
            error="No account_id stored yet — run the connection test first.",
        )

    bsettings = _decrypt_buildium_settings(row)
    try:
        # `reraise=True` so the message below stays the provider's own words
        # rather than a generic failure; the row is marked `failed` either way.
        result = await run_initial_sync(
            bsettings,
            pool,
            company_id=company_id,
            account_id=int(row["account_id"]),
            reraise=True,
        )
    except Exception as exc:  # noqa: BLE001 — surface any unexpected error to the UI
        logger.warning("buildium /backfill crashed for company=%s: %s", company_id, exc)
        return BackfillResponse(ok=False, error=f"Backfill failed: {exc}")
    if result is None:  # pragma: no cover — unreachable while reraise=True
        return BackfillResponse(ok=False, error="Backfill failed.")

    # Geocode any newly-synced properties immediately, so "properties near X"
    # / "vendors near property" work the moment a user hits Sync data instead
    # of waiting for the worker's next mirror-sync tick. Same best-effort
    # discipline as mirror_sync: no-op without a Maps key or the 0053 column,
    # cached (only NULL-geog rows), and never allowed to fail the sync.
    try:
        await geocode_company_properties(pool, settings=get_settings(), company_id=company_id)
    except (Exception, SystemExit):  # noqa: BLE001 — geocoding is an enhancement, never load-bearing
        logger.warning(
            "buildium /backfill: geocode pass failed for company=%s", company_id, exc_info=True
        )

    return BackfillResponse(
        ok=result.ok,
        properties=result.properties,
        units=result.units,
        tenants=result.tenants,
        leases=result.leases,
        balances=result.balances,
        transactions=result.transactions,
        work_orders=result.work_orders,
        applicants=result.applicants,
        vendors=result.vendors,
        owners=result.owners,
        tasks=result.tasks,
        gl_accounts=result.gl_accounts,
        bills=result.bills,
        bank_accounts=result.bank_accounts,
        errors=result.errors,
    )


@router.get(
    "/webhook-secret",
    summary="Whether a Buildium webhook signing secret is configured",
)
async def get_webhook_secret(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> WebhookSecretStatus:
    """Returns whether the user has stored Buildium's signing secret,
    and the last 4 characters for UI confirmation. Never returns the
    actual secret (it's encrypted at rest and only used server-side
    to verify inbound webhook signatures)."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="view")
    row = await _fetch_row(pool, company_id)
    if row is None or row["encrypted_webhook_secret"] is None:
        return WebhookSecretStatus(configured=False, last4=None)
    return WebhookSecretStatus(
        configured=True,
        last4=row["webhook_secret_last4"],
    )


@router.put(
    "/webhook-secret",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Store the signing secret Buildium generated",
)
async def set_webhook_secret(
    body: SetWebhookSecretRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    """Store the secret Buildium showed the user when they created the
    webhook subscription on their side.

    Buildium generates the signing secret — Helixis cannot. Calling this
    again with a new value rotates the secret (e.g. after the user
    rotates the secret in Buildium's dashboard).
    """
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="set the webhook secret for")

    row = await _fetch_row(pool, company_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Save your Buildium API credentials first — the webhook "
                "secret is stored alongside them."
            ),
        )

    cipher = _cipher()
    await pool.execute(
        """
        UPDATE public.company_buildium_credentials
        SET encrypted_webhook_secret = $2,
            webhook_secret_last4     = $3
        WHERE company_id = $1
        """,
        company_id,
        cipher.encrypt(body.secret),
        cipher.last4(body.secret),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/webhook-secret")
async def delete_webhook_secret(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> BuildiumCredentialsStatus:
    """Tombstone the webhook secret (forces rotation). Leaves API creds intact."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="delete the webhook secret for")
    await pool.execute(
        """
        UPDATE public.company_buildium_credentials
        SET encrypted_webhook_secret = NULL,
            webhook_secret_last4     = NULL
        WHERE company_id = $1
        """,
        company_id,
    )
    row = await _fetch_row(pool, company_id)
    return _row_to_status(row)


_RECENT_EVENTS_DEFAULT_LIMIT = 5
_RECENT_EVENTS_MAX_LIMIT = 50
# ⚠ ``_parse_include`` intersects the caller's list with this set and DROPS
# unknown names silently, so a client asking for a field missing here gets a
# 200 with the field absent and no hint why. Adding a column to the client
# without adding it here is a no-op that looks like a backend bug.
_INCLUDABLE_FIELDS = {
    "action_surface",
    "enrichment",
    "executed_action_index",
    # The OUTCOME of an executed action. Persisted since the execute route was
    # written (``execution_result``/``executed_at`` land in the same UPDATE as
    # ``executed_action_index``) and never once requested, so a finished inbox
    # item could only say "Sent" — never what happened.
    "execution_result",
    "executed_at",
}

# PR #101 — the always-on column subset the recent-events route SELECTs.
# Single source of truth: the route reads from this constant, and the
# web service's lifespan calls ``verify_schema`` against this tuple at
# boot so a stale pool view of the schema fails the deploy before
# serving partial-shape responses (the bug PR #96 partially addressed
# at the asyncpg layer). The ``?include=``-gated columns are NOT in
# this tuple — if those columns are missing, the route already 200s
# with a skinny shape via the ``UndefinedColumnError`` fallback below.
RECENT_EVENTS_COLUMNS: tuple[str, ...] = (
    "id",
    "event_name",
    "event_time",
    "status",
    "last_error",
    "planner_status",
    "planner_error",
    "summary_status",
    "summary_text",
    "summary_error",
    "summary_attempts",
    "summary_last_attempted_at",
)


def _parse_include(include: str | None) -> set[str]:
    """Parse ``?include=action_surface,enrichment`` into a set of
    column names. Unknown values are dropped silently — better than
    400-ing an existing caller when a future field name lands."""
    if not include:
        return set()
    requested = {part.strip() for part in include.split(",") if part.strip()}
    return requested & _INCLUDABLE_FIELDS


def _maybe_jsonb(value: Any) -> dict[str, Any] | None:
    """asyncpg returns jsonb either as ``dict`` or as ``str`` depending
    on the driver version; normalise to ``dict | None``."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        import json

        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


async def _resolve_caller_buildium_user_id(
    pool: Any, company_id: str, email: str | None
) -> int | None:
    """Resolve the logged-in Helixis user to their Buildium staff user id
    (#69, the person-routing last mile).

    v1 links by EMAIL: the caller's login email matched (case-insensitively)
    against the ``buildium_staff`` cache. No migration / no manual link step —
    if a teammate's Helixis login email equals their Buildium staff email (the
    common case), "My work" filters correctly the moment staff is synced. When
    they differ the match simply misses and the caller falls back to the "All"
    view (safe degrade); an explicit link column is the future upgrade.

    Tolerant: missing table/column/RLS → None (no filter), never a 500.
    """
    if not email:
        return None
    try:
        row = await pool.fetchrow(
            """
            SELECT buildium_user_id
              FROM buildium_staff
             WHERE company_id = $1 AND lower(email) = lower($2) AND is_active
             ORDER BY buildium_user_id
             LIMIT 1
            """,
            company_id,
            email,
        )
    except (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.UndefinedColumnError):
        logger.warning("buildium_staff lookup for caller identity failed", exc_info=True)
        return None
    if row is None:
        return None
    try:
        return int(row["buildium_user_id"])
    except (KeyError, TypeError, ValueError):
        return None


async def _fetch_staff_name_map(pool: Any, company_id: str) -> dict[int, str]:
    """Build a ``{buildium_user_id: display_name}`` map for the
    requesting company.

    PR #103 — unblocks the Assignee field on the Inbox detail pane.
    The webhook receiver resolves assignee names into the
    ``action_surface.summary`` string today (see
    ``surface_builder._assignee_label``) but never writes a
    structured field, so the frontend renders ``—`` regardless.
    Resolving at route-read time (rather than enrichment-write
    time) keeps existing rows working immediately — no backfill
    needed.

    Tolerant: missing table, RLS deny, or column drift returns an
    empty map so the route still serves the rest of the response.
    The frontend already falls back to ``—`` when the name is
    absent.
    """
    try:
        rows = await pool.fetch(
            """
            SELECT buildium_user_id, first_name, last_name
              FROM buildium_staff
             WHERE company_id = $1
            """,
            company_id,
        )
    except (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.UndefinedColumnError):
        logger.warning(
            "buildium_staff lookup failed — Inbox Assignee column will fall back to '—'",
            exc_info=True,
        )
        return {}

    out: dict[int, str] = {}
    for r in rows:
        # Defensive: a future schema change or a test that mocks the
        # pool without the staff-row shape must not 500 the inbox.
        # Skip rows that don't have all three columns we need.
        try:
            first = (r["first_name"] or "").strip()
            last = (r["last_name"] or "").strip()
            user_id = int(r["buildium_user_id"])
        except (KeyError, TypeError, ValueError):
            continue
        name = f"{first} {last}".strip()
        if name:
            out[user_id] = name
    return out


def _decorate_enrichment_assignee(
    enrichment: dict[str, Any] | None,
    staff_map: dict[int, str],
) -> dict[str, Any] | None:
    """Add ``task.assignee_name`` to enrichment in-place (returning
    the same object) when ``assigned_to_user_id`` resolves through
    the staff map.

    Safe to call with ``None`` enrichment — no-op. Safe to call
    with an empty staff map — leaves ``assignee_name`` absent so
    the frontend's existing ``'—'`` fallback kicks in.
    """
    if not enrichment:
        return enrichment
    task = enrichment.get("task")
    if not isinstance(task, dict):
        return enrichment
    assignee_id = task.get("assigned_to_user_id")
    if assignee_id is None:
        return enrichment
    try:
        key = int(assignee_id)
    except (TypeError, ValueError):
        return enrichment
    name = staff_map.get(key)
    if name:
        task["assignee_name"] = name
    return enrichment


def _decorate_enrichment_reporter(
    enrichment: dict[str, Any] | None,
    staff_map: dict[int, str],
) -> dict[str, Any] | None:
    """Fall back to ``created_by_user_id`` for the Reporter column
    when ``RequestedByUserEntity`` was absent on the Buildium task.

    Per Buildium's Tasks API docs, the four task subtypes carry the
    requester differently:

      * ``/tasks/residentrequests`` — resident reports →
        ``RequestedByUserEntity`` populated with ``Type='Resident'``.
      * ``/tasks/rentalownerrequests`` — owner reports →
        ``RequestedByUserEntity`` populated with ``Type='RentalOwner'``.
      * ``/tasks/contactrequests`` — external contact →
        ``RequestedByUserEntity`` populated.
      * ``/tasks/todorequests`` — staff to-dos → **NO**
        ``RequestedByUserEntity``. The creator IS the reporter.

    The enricher (PR #92 / PR #108) maps ``RequestedByUserEntity`` →
    ``task.requested_by_first_name`` + ``..._last_name``. For
    ``todorequests`` those are None and the Inbox shows '—' even
    though Buildium knows exactly who created the task (the
    ``CreatedByUserId`` field is always present).

    This helper closes the loop:

      1. If ``requested_by_first_name`` OR ``..._last_name`` is
         already populated, do NOTHING (the explicit requester wins).
      2. Else, look up ``created_by_user_id`` in the staff cache.
      3. Split the resolved display name on the first space and
         populate the two ``requested_by_*`` fields so the frontend's
         ``formatReporter`` renders it without any frontend changes.

    Safe to call with ``None`` enrichment, missing ``task`` key,
    missing ``created_by_user_id``, or empty staff map.
    """
    if not enrichment:
        return enrichment
    task = enrichment.get("task")
    if not isinstance(task, dict):
        return enrichment
    existing_first = (
        (task.get("requested_by_first_name") or "").strip()
        if isinstance(task.get("requested_by_first_name"), str)
        else ""
    )
    existing_last = (
        (task.get("requested_by_last_name") or "").strip()
        if isinstance(task.get("requested_by_last_name"), str)
        else ""
    )
    if existing_first or existing_last:
        # Buildium-supplied requester wins — never overwrite.
        return enrichment
    creator_id = task.get("created_by_user_id")
    if creator_id is None:
        return enrichment
    try:
        key = int(creator_id)
    except (TypeError, ValueError):
        return enrichment
    name = staff_map.get(key)
    if not name:
        return enrichment
    first, _, last = name.partition(" ")
    task["requested_by_first_name"] = first or None
    task["requested_by_last_name"] = last or None
    return enrichment


@router.get("/recent-events")
async def recent_events(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    include: str | None = Query(default=None),
    assignee: str | None = Query(default=None),
    limit: int = Query(default=_RECENT_EVENTS_DEFAULT_LIMIT, ge=1, le=_RECENT_EVENTS_MAX_LIMIT),
) -> list[RecentEvent]:
    """Recent ``buildium_events`` rows for the user's company.

    Default returns the same skinny shape pre-A.2 callers (the
    Webhooks page, the connector detail panel) depend on. Pass
    ``?include=action_surface,enrichment`` to inflate the response
    with the Phase A.1 jsonb columns — that's what the Inbox page
    uses. ``executed_action_index`` is requestable the same way so
    the inbox can filter out already-actioned rows client-side.

    Intentionally tolerant: missing table / missing enrichment
    columns / partial migration state all degrade to an empty list
    rather than 500ing the page.
    """
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()

    fields = _parse_include(include)
    # ``RECENT_EVENTS_COLUMNS`` is the always-on subset (PR #101 —
    # verified at boot via ``verify_schema`` so a stale pool view of
    # the schema fails the deploy rather than serving partial rows).
    columns = list(RECENT_EVENTS_COLUMNS)
    if "action_surface" in fields:
        columns.append("action_surface")
    if "enrichment" in fields:
        columns.append("enrichment")
    if "executed_action_index" in fields:
        columns.append("executed_action_index")
    # The outcome columns. Gated the same way, so a caller that does not ask
    # for them sees the identical response shape it saw before.
    if "execution_result" in fields:
        columns.append("execution_result")
    if "executed_at" in fields:
        columns.append("executed_at")
    select_sql = ", ".join(columns)

    # Inbox allowlist. The Inbox is the only caller that requests the
    # ``action_surface`` payload, so we key off that to show ONLY the
    # actionable event families — tasks, work orders, resident requests,
    # and leasing-pipeline milestones (see ``inbox_filter``). Everything
    # else Buildium sends (payments/charges, lease transactions,
    # tenant-identity edits, owner-ledger bookkeeping, GL setup) is
    # hidden here, which also clears rows already on disk — a Buildium
    # owner-charge billing run had fanned 20 bare "assigned to the
    # admin" cards into the General lane. The skinny shape (no includes)
    # feeds the connector's webhook-delivery debug panel, which
    # intentionally still lists every delivery, so it is left untouched.
    inbox_only = "action_surface" in fields
    where = "WHERE company_id = $1"
    params: list[Any] = [company_id, limit]  # $1 = company_id, $2 = limit
    if inbox_only:
        params.append(inbox_like_patterns())
        where += f" AND event_name LIKE ANY(${len(params)}::text[])"

    # Per-user inbox filter (#69): ``?assignee=me`` narrows to the rows the
    # webhook router assigned to the CALLER, so a 10-person team each sees
    # "My work" instead of one undifferentiated shared queue. We resolve the
    # caller's Buildium staff id by email and filter on the indexed
    # ``routing_decision->>'assigned_buildium_user_id'`` (migration 0016 added
    # that btree index for exactly this predicate). If the caller has no
    # resolvable Buildium identity (email mismatch / staff not synced), there
    # is no work we can attribute to them — return empty rather than leaking
    # the whole team's queue under a "My work" label.
    if (assignee or "").strip().lower() == "me":
        my_buildium_id = await _resolve_caller_buildium_user_id(pool, company_id, user.email)
        if my_buildium_id is None:
            return []
        params.append(str(my_buildium_id))
        where += f" AND routing_decision->>'assigned_buildium_user_id' = ${len(params)}"

    query = f"""
        SELECT {select_sql}
        FROM buildium_events
        {where}
        ORDER BY event_time DESC
        LIMIT $2
    """  # noqa: S608 — column list is allow-listed above, not user input

    try:
        rows = await pool.fetch(query, *params)
    except asyncpg.exceptions.UndefinedTableError:
        # PR #96 — bumped warning → error so this shows up in
        # Render's error log filter. UndefinedTableError on a
        # production deploy means the migrations are out of sync;
        # we want a paging-grade alert, not a quiet noop.
        logger.error(
            "buildium_events table not provisioned yet — returning empty list. "
            "Apply pending Supabase migrations (0007_buildium_events.sql).",
        )
        return []
    except asyncpg.exceptions.UndefinedColumnError:
        # Phase A.1 / A.2 / PR #89 / PR #92 columns missing on a
        # stale DB. We still fall back to the skinny shape rather
        # than 500ing the panel (legit pre-migration case during a
        # rolling deploy). PR #96 — escalated to ``error`` level
        # with ``exc_info=True`` so the specific missing column
        # name shows up in Render logs immediately and we can
        # diagnose without bisecting code. The skinny fallback no
        # longer reaches the ``_row_to_summary`` path, so the
        # response carries ``summary=None`` instead of a fake
        # ``status='succeeded' + text=null`` shape (see Pydantic
        # field changes below).
        logger.error(
            "recent-events: extended column missing — falling back to skinny shape "
            "(apply the latest supabase migration to clear this)",
            exc_info=True,
        )
        rows = await pool.fetch(
            """
            SELECT id, event_name, event_time, status, last_error
            FROM buildium_events
            WHERE company_id = $1
            ORDER BY event_time DESC
            LIMIT $2
            """,
            company_id,
            limit,
        )
        fields = set()
    except Exception:
        # PR #96 — stop the silent degradation that hid the PR #92
        # summary-truth bug for 30 minutes. The original ``except
        # Exception: return []`` swallowed every error (network
        # blip, JSON parse, asyncpg DataError, anything) and
        # returned an empty list, so the frontend saw "everything
        # is fine" while the API was actively failing. We now log
        # at ``error`` with the full traceback AND re-raise so
        # FastAPI returns 500 and the monitoring stack surfaces it.
        logger.error("recent-events query failed", exc_info=True)
        raise

    out: list[RecentEvent] = []
    # PR #103 — fetch the staff cache so we can decorate
    # ``enrichment.task.assigned_to_user_id`` with a human-readable
    # ``assignee_name``. Without this the Inbox detail pane shows
    # "—" forever even when Buildium has the assignee set (the
    # user_id was opaque to the frontend). Cheap: per-company table,
    # single-digit row count typical.
    #
    # Runs ONLY when ``enrichment`` is in the requested fields. The
    # skinny fallback path (UndefinedColumnError → simplified query
    # above) already returned without enrichment, so skipping the
    # extra round-trip there keeps that error path identical to
    # pre-PR-#103 behavior (and keeps tests that mock pool.fetch's
    # side_effect with two values working without an update).
    staff_map: dict[int, str] = {}
    if "enrichment" in fields:
        staff_map = await _fetch_staff_name_map(pool, company_id)
    # A card planned before the planner knew the company's PMS can carry a
    # write to the other one; it is marked so the Inbox hides it (gotcha 183).
    pms = await company_pms(pool, company_id) if "action_surface" in fields else DEFAULT_PMS
    for r in rows:
        # PR #102 lineage / PR #103 sweep — convert to dict ONCE so
        # ``"key" in d`` is safe + cheap. The five remaining inline
        # ``r.keys()`` sites elsewhere on this route were technically
        # correct (fresh iterator each call) but wasteful and one
        # accidental capture away from re-creating the PR #102 bug.
        d = dict(r)
        kwargs: dict[str, Any] = {
            "id": str(d["id"]),
            "event_name": d["event_name"],
            "event_time": d["event_time"].isoformat(),
            "status": d["status"],
            "last_error": d["last_error"],
        }
        # PR #89 — planner_status / planner_error may be absent on
        # the skinny fallback path (pre-migration DB). The Pydantic
        # default ('succeeded' / None) takes over when keys are
        # missing.
        if "planner_status" in d:
            kwargs["planner_status"] = d["planner_status"]
        if "planner_error" in d:
            kwargs["planner_error"] = d["planner_error"]
        # PR #92 — summary columns (same defensive present-key
        # gating as planner_*).
        if "summary_status" in d:
            kwargs["summary"] = _row_to_summary(d)
        if "action_surface" in fields:
            kwargs["action_surface"] = mark_unavailable(_maybe_jsonb(d["action_surface"]), pms)
        if "enrichment" in fields:
            enrichment = _maybe_jsonb(d["enrichment"])
            # PR #103 — inject assignee_name onto enrichment.task so
            # the Inbox header cell can render a name instead of the
            # hardcoded "—" placeholder (TODO from PR #86).
            enrichment = _decorate_enrichment_assignee(enrichment, staff_map)
            # PR #108 — for staff-created ``todorequests`` (Buildium
            # subtype with no ``RequestedByUserEntity``), fall back
            # to ``CreatedByUserId`` through the staff cache so the
            # Reporter column populates with the staff member's name.
            kwargs["enrichment"] = _decorate_enrichment_reporter(enrichment, staff_map)
        if "executed_action_index" in fields:
            kwargs["executed_action_index"] = d["executed_action_index"]
        if "execution_result" in fields:
            kwargs["execution_result"] = _maybe_jsonb(d["execution_result"])
        if "executed_at" in fields:
            # isoformat, not the datetime — the model types it ``str | None``
            # and asyncpg hands back a datetime.
            ts = d["executed_at"]
            kwargs["executed_at"] = ts.isoformat() if ts is not None else None
        out.append(RecentEvent(**kwargs))

    # ⚠ ONE query for the whole page, after the rows are built — not per card,
    # and not by widening RECENT_EVENTS_COLUMNS to drag every row's payload
    # back so a handful of risk rows can be annotated.
    #
    # ⚠ Only on the inbox path. The skinny fallback above already returned, and
    # a caller asking for the plain shape gets it byte-for-byte as before.
    if inbox_only and out:
        states = await finding_states_for_events(pool, company_id, [e.id for e in out])
        for event in out:
            event.finding_state = states.get(event.id)
    return out


def _row_to_summary(r: Any) -> InboxEventSummary:
    """Build an ``InboxEventSummary`` from an asyncpg row.

    Tolerates the skinny / pre-migration shape: columns that drop
    out of the SELECT (because of a fallback path) simply use the
    Pydantic default for that field.

    PR #102 — we convert ``r`` to a ``dict`` before doing membership
    checks. ``asyncpg.Record.keys()`` returns a SINGLE-USE ITERATOR,
    not a reusable view. The prior pattern captured it once
    (``keys = r.keys()``) and did multiple ``"key" in keys`` checks;
    the first ``in`` consumed the iterator and every subsequent
    check silently fell through to the default. Production
    serialization returned ``text=None / attempts=0`` while the DB
    had the values — only ``last_attempted_at`` round-tripped because
    its check happened before the iterator was exhausted.
    ``dict(r)`` materializes the row once into a normal Python dict
    where ``in`` and indexing both behave normally.
    """
    d = dict(r)
    last_attempted = d.get("summary_last_attempted_at")
    return InboxEventSummary(
        status=d.get("summary_status", "succeeded"),
        text=d.get("summary_text"),
        error=d.get("summary_error"),
        attempts=d.get("summary_attempts", 0),
        last_attempted_at=last_attempted.isoformat() if last_attempted is not None else None,
    )


@router.get("/events/{event_id}")
async def get_event(
    # PR #94 — UUID type-constraint so FastAPI returns 422 on a
    # non-UUID path param (e.g. ``GET /events/recent-events``) instead
    # of passing a bare string into asyncpg and getting a 500 from
    # ``invalid input for query argument: not a UUID``. Same fix
    # applied to every ``/events/{event_id}`` endpoint below.
    event_id: UUID,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> FullBuildiumEvent:
    """Return one ``buildium_events`` row in full for the Inbox detail.

    Scoped to the requesting user's company in WHERE (defence-in-depth
    on top of the existing 0007 RLS policies). 404 when the row is
    missing or owned by another tenant.
    """
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    try:
        row = await pool.fetchrow(
            """
            SELECT id, event_name, event_time, status, last_error,
                   action_surface, enrichment,
                   executed_action_index, executed_at,
                   executed_by_user_id, execution_result,
                   planner_status, planner_error,
                   summary_status, summary_text, summary_error,
                   summary_attempts, summary_last_attempted_at
            FROM buildium_events
            WHERE id = $1 AND company_id = $2
            """,
            event_id,
            company_id,
        )
    except (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.UndefinedColumnError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="buildium_events not provisioned for this environment",
        ) from None
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="event not found",
        )
    # PR #102 lineage / PR #103 sweep — convert to dict ONCE; see the
    # ``r.keys()``-was-an-iterator note in ``_row_to_summary``. PR #103
    # also decorates enrichment with the resolved assignee_name so
    # the single-event detail pane shows the name too if it ever
    # gets called directly (recent-events already covers the inbox
    # list path, but this keeps both endpoints consistent).
    d = dict(row)
    staff_map = await _fetch_staff_name_map(pool, company_id)
    enrichment = _decorate_enrichment_assignee(_maybe_jsonb(d["enrichment"]), staff_map)
    # PR #108 — same fallback chain for the single-event endpoint:
    # if RequestedByUserEntity was absent (todorequests subtype),
    # fill Reporter from the staff cache via CreatedByUserId.
    enrichment = _decorate_enrichment_reporter(enrichment, staff_map)
    return FullBuildiumEvent(
        id=str(d["id"]),
        event_name=d["event_name"],
        event_time=d["event_time"].isoformat(),
        status=d["status"],
        last_error=d["last_error"],
        action_surface=mark_unavailable(
            _maybe_jsonb(d["action_surface"]), await company_pms(pool, company_id)
        ),
        enrichment=enrichment,
        executed_action_index=d["executed_action_index"],
        executed_at=d["executed_at"].isoformat() if d["executed_at"] else None,
        executed_by_user_id=(str(d["executed_by_user_id"]) if d["executed_by_user_id"] else None),
        execution_result=_maybe_jsonb(d["execution_result"]),
        # PR #89 — planner lifecycle. Default 'succeeded' kicks in on
        # pre-migration deploys where the Pydantic model has the new
        # fields but the DB row doesn't.
        planner_status=d.get("planner_status", "succeeded"),
        planner_error=d.get("planner_error"),
        # PR #92 — summary lifecycle (see _row_to_summary for the
        # defensive-defaulting story).
        summary=_row_to_summary(d),
    )


# Handler statuses raised BEFORE the Buildium side effect could fire —
# validation (400/422) or an account scope that doesn't expose the write via
# API (501). These are provably "nothing happened", so the idempotency guard
# records them as retryable (the user may fix params and re-execute). Every
# OTHER failure is treated as ambiguous (the write MAY have fired) →
# abandoned_unknown → never auto-refired.
#
# ⚠ 409 is here for the STALENESS check, and it has to be. That check runs
# before the handler touches Buildium, so a stale card provably fired nothing
# and the claim must stay RETRYABLE — the user refreshes the Inbox and acts
# again on the same event. Left out of this set it would be classed ambiguous,
# recorded ``abandoned_unknown``, and that action could never be retried at
# all: a freshness guard that permanently bricks the card it was protecting.
# Nothing else raises ``HandlerError(409)`` (pinned by a test), so widening
# the set cannot reclassify an existing failure.
_PRE_SIDE_EFFECT_STATUSES = frozenset({400, 409, 422, 501})


def _idempotency_store(pool: asyncpg.Pool) -> IdempotencyStore:
    """Durable idempotency store for the execute endpoint.

    A thin seam so unit tests can inject an in-memory fake store."""
    return PgIdempotencyStore(pool)


async def _execute_action_once(
    *,
    store: IdempotencyStore,
    company_id: str,
    event_id: str,
    action_index: int,
    tool_id: str,
    merged_params: dict[str, Any],
    run_handler: Callable[[], Awaitable[Any]],
) -> GuardResult:
    """Run one action's Buildium write AT MOST ONCE.

    The endpoint's pre-existing ``executed_action_index`` guard is a
    check-then-act: two concurrent submits (double-click, client retry) can
    both pass the NULL check and both fire the write before either records
    completion. This wraps the handler in the durable idempotency claim so the
    side effect fires exactly once across that window.

    Confirmation is NOT gated here (``confirm=None``): the human already
    approved in the UI before this endpoint was called, so the SSE confirm gate
    does not apply to this surface — the guard provides dedup, not consent.

    ``run_handler`` performs the Buildium write and raises
    :class:`HandlerError` on failure; a validation / unsupported-scope error
    (see ``_PRE_SIDE_EFFECT_STATUSES``) is reclassified as
    :class:`PreSideEffectError` so the claim stays retryable, while every other
    failure bubbles unchanged and the guard records ``abandoned_unknown``.
    """

    async def _guarded_execute() -> dict[str, Any]:
        try:
            result = await run_handler()
        except HandlerError as exc:
            if exc.status_code in _PRE_SIDE_EFFECT_STATUSES:
                raise PreSideEffectError(str(exc)) from exc
            raise
        return result if isinstance(result, dict) else {"raw": result}

    idem_key = derive_key(
        company_id,
        tool_id,
        merged_params,
        extra=f"execute:{event_id}:{action_index}",
    )
    return await guarded_action(
        store=store,
        company_id=company_id,
        idem_key=idem_key,
        action_type=f"execute.{tool_id}",
        confirm=None,
        execute=_guarded_execute,
        window=None,
    )


@router.post("/events/{event_id}/actions/{action_index}/execute")
async def execute_event_action(
    event_id: UUID,  # PR #94 — 422 on non-UUID, no asyncpg 500
    action_index: int,
    body: ExecuteActionRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ExecuteActionResponse:
    """Execute one planner-proposed action against Buildium.

    Idempotent at the row level: once ``executed_action_index`` is
    set, the endpoint refuses to fire again (409). The handler does
    NOT mark the row executed on its own failure — a 502 leaves the
    row clean so the user can adjust parameters and retry.
    """
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()

    # Role floor (approval-pipeline PR): executing a planner action drives a
    # side-effecting Buildium write — manager-or-above, membership alone is
    # not enough (same gate as /agent/confirm approvals, ADR 0005).
    from helixis.api.roles import MANAGER
    from helixis.api.routes.company import require_role

    await require_role(
        pool,
        user.user_id,
        company_id,
        MANAGER,
        detail="Only managers and admins can execute agent actions.",
    )

    row = await pool.fetchrow(
        """
        SELECT id, event_name, account_id, action_surface, enrichment,
               executed_action_index
        FROM buildium_events
        WHERE id = $1 AND company_id = $2
        """,
        event_id,
        company_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="event not found")
    if row["executed_action_index"] is not None:
        raise HTTPException(
            status_code=409,
            detail=(f"event already actioned via action #{row['executed_action_index']}"),
        )

    action_surface = _maybe_jsonb(row["action_surface"]) or {}
    actions = action_surface.get("actions") if isinstance(action_surface, dict) else None
    if not isinstance(actions, list) or not actions:
        raise HTTPException(status_code=400, detail="event has no action surface to execute")
    if not 0 <= action_index < len(actions):
        raise HTTPException(
            status_code=400,
            detail=f"action_index {action_index} out of bounds (0..{len(actions) - 1})",
        )

    action = actions[action_index]
    raw_tool_id = (action or {}).get("tool_id")
    handler = get_handler(raw_tool_id) if isinstance(raw_tool_id, str) else None
    if handler is None or not isinstance(raw_tool_id, str):
        raise HTTPException(
            status_code=400,
            detail=f"no handler registered for tool_id {raw_tool_id!r}",
        )
    tool_id: str = raw_tool_id

    # A write to a PMS this workspace is not on can only fail, and the
    # credentials error further down would tell the user to configure
    # Buildium, which one PMS per workspace forbids (gotcha 183).
    refused = unavailable_reason(tool_id, await company_pms(pool, company_id))
    if refused is not None:
        raise HTTPException(status_code=409, detail=refused)

    # Parameter merge order (Phase A.2.5):
    #   1. action.parameters         — planner's defaults
    #   2. each chain pick's patch    — in order, the user's clicks
    #   3. parameter_overrides        — final user edits in the modal
    # Each layer wins over the previous one. Chain comes BEFORE
    # overrides so a user who picks "Reassuring" on the message-tone
    # follow-up and then edits the body on the confirmation screen
    # gets their edit applied, not overwritten.
    base_params: dict[str, Any] = dict((action or {}).get("parameters") or {})
    chain = body.chain or []
    for pick in chain:
        for k, v in (pick.parameter_patch or {}).items():
            base_params[k] = v
    overrides = body.parameter_overrides or {}
    merged_params: dict[str, Any] = {**base_params, **overrides}

    enrichment = _maybe_jsonb(row["enrichment"]) or {}
    enriched_task = enrichment.get("task") if isinstance(enrichment, dict) else {}
    enriched_task = enriched_task if isinstance(enriched_task, dict) else {}
    handler_context: dict[str, Any] = {
        "event_id": str(row["id"]),
        "event_name": row["event_name"],
        "event_task_id": enriched_task.get("id"),
        "event_unit_id": enriched_task.get("unit_id"),
        "event_property_id": enriched_task.get("property_id"),
        "assigned_to_user_id": enriched_task.get("assigned_to_user_id"),
    }

    # Thread the inbound request text (the resident request, or the inbound
    # email body for reply_to_email) so the fair-housing egress gate can run its
    # protected-class-question deflection check, not just the steering denylist.
    # Capped so a giant pasted email doesn't blow the gate's regex budget.
    _source_request = enriched_task.get("description")
    if isinstance(_source_request, str) and _source_request.strip():
        handler_context["source_request"] = _source_request[:2000]

    # Email-ingest reply: routes through Composio Gmail, NOT Buildium. The
    # handler needs the composio client, the auth user (Gmail is personal-
    # scoped), and the thread/sender off the email enrichment's task.raw.
    is_email_reply = tool_id == "reply_to_email"
    if is_email_reply:
        raw = enriched_task.get("raw")
        raw = raw if isinstance(raw, dict) else {}
        handler_context["composio"] = getattr(request.app.state, "composio_client", None)
        handler_context["composio_user_id"] = user.user_id
        handler_context["email_thread_id"] = raw.get("thread_id")
        handler_context["email_to"] = raw.get("from_email")

    # Decrypt the company's Buildium creds via the same path the
    # connector + /test endpoints use. Skipped for email replies, which
    # never touch Buildium (and may run for a company mid-onboarding).
    bsettings = None
    if not is_email_reply:
        creds_row = await _fetch_row(pool, company_id)
        if creds_row is None:
            raise HTTPException(
                status_code=400,
                detail="company has no Buildium credentials configured",
            )
        bsettings = _decrypt_buildium_settings(creds_row)

    async def _run_handler() -> Any:
        with trace_context(
            company_id=company_id,
            session_id=f"execute-{event_id}",
            trace_name=f"execute.{tool_id}",
            tags=("executor", "phase-a2", tool_id),
        ):
            if is_email_reply:
                # No Buildium client — the reply handler uses Composio.
                return await handler.execute(
                    None,  # type: ignore[arg-type]
                    merged_params,
                    context=handler_context,
                )
            # bsettings is always set on the non-email path (decrypted
            # above); assert narrows BuildiumSettings | None for mypy.
            assert bsettings is not None
            async with BuildiumClient(bsettings) as client:
                # ⚠ A card is a photograph of the record when its webhook
                # arrived. Between then and this click the task can have been
                # completed IN BUILDIUM, where we never see it — and the
                # handler would post "just following up, still outstanding"
                # onto a job that finished on Tuesday. Plausible, well-formed,
                # and false.
                #
                # Deliberately inside the guard and BEFORE any write: a
                # HandlerError with a pre-side-effect status is reclassified as
                # PreSideEffectError, so a stale card leaves the claim
                # RETRYABLE — the user refreshes and can act again — rather
                # than burning the idempotency key on a write that never fired.
                verdict = await revalidate_task(
                    client, handler_context.get("event_task_id"), enriched_task.get("status")
                )
                if verdict.stale:
                    raise HandlerError(verdict.reason, status_code=409)
                if not verdict.checked:
                    # An unchecked execution is not a checked one. Gotcha 17:
                    # the memory embedder degraded silently for nineteen days
                    # because its failure path said nothing.
                    logger.warning(
                        "executing %s on event %s WITHOUT revalidation (%s)",
                        tool_id,
                        event_id,
                        verdict.reason or "not applicable",
                    )
                return await handler.execute(client, merged_params, context=handler_context)

    try:
        guard = await _execute_action_once(
            store=_idempotency_store(pool),
            company_id=company_id,
            event_id=str(event_id),
            action_index=action_index,
            tool_id=tool_id,
            merged_params=merged_params,
            run_handler=_run_handler,
        )
    except PreSideEffectError as exc:
        # Validation / unsupported-scope: nothing fired; the claim is left
        # retryable so the user can fix parameters and re-execute.
        orig = exc.__cause__
        status_code = orig.status_code if isinstance(orig, HandlerError) else 400
        logger.warning(
            "handler %s rejected event %s before write: %s",
            tool_id,
            event_id,
            orig or exc,
        )
        raise HTTPException(
            status_code=status_code,
            detail={"error": str(orig or exc), "tool_id": tool_id},
        ) from exc
    except HandlerError as exc:
        # Ambiguous failure: the guard recorded abandoned_unknown so this exact
        # action is never auto-refired.
        logger.warning(
            "handler %s failed for event %s: %s",
            tool_id,
            event_id,
            exc,
        )
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": str(exc), "tool_id": tool_id},
        ) from exc

    if guard.outcome != "executed":
        # The write already happened (replayed), is in flight (already_pending),
        # or a prior attempt's outcome is unknown (blocked_needs_review). In every
        # case we must NOT fire again — surface a 409 the UI can render.
        detail = {
            "replayed": "this action has already been executed",
            "already_pending": "this action is already being executed",
            "blocked_needs_review": (
                "a previous attempt did not complete cleanly; this action needs "
                "review before it can be retried"
            ),
        }.get(guard.outcome, f"action not executed ({guard.outcome})")
        raise HTTPException(status_code=409, detail={"error": detail, "tool_id": tool_id})

    result: dict[str, Any] = (
        guard.result if isinstance(guard.result, dict) else {"raw": guard.result}
    )

    # Persist execution state — atomic so a partial write doesn't
    # leave the row half-actioned. The chain is folded into
    # ``execution_result`` so the Phase A.3 activity tab + future
    # audits can reconstruct exactly which click-chain the user
    # walked through.
    import json

    chain_jsonable = [pick.model_dump(mode="json") for pick in chain]
    persisted_result: dict[str, Any] = {
        **(result if isinstance(result, dict) else {"raw": result}),
        "chain": chain_jsonable,
    }

    await pool.execute(
        """
        UPDATE buildium_events
        SET executed_action_index = $2,
            executed_at           = now(),
            executed_by_user_id   = $3,
            execution_result      = $4::jsonb
        WHERE id = $1
        """,
        event_id,
        action_index,
        user.user_id,
        json.dumps(persisted_result),
    )

    # Write the human-action audit row. Description carries the
    # click-chain step count when the user walked through one — gives
    # Phase A.3's activity tab a "via 2 picks" hint without forcing
    # it to inspect the jsonb.
    summary = result.get("summary") if isinstance(result, dict) else None
    base_description = str(action.get("intent") or summary or tool_id)
    description = (
        f"{base_description} via click-chain ({len(chain)} steps)" if chain else base_description
    )
    activity_raw: dict[str, Any] = {
        "params": merged_params,
        "result_summary": summary,
        "freeform_note": body.freeform_note,
        "chain": chain_jsonable,
    }
    await write_activity(
        pool,
        ActivityEntry(
            account_id=int(row["account_id"]) if row["account_id"] is not None else None,
            company_id=company_id,
            source="webhook",
            source_label=row["event_name"],
            action_type=tool_id,
            description=description,
            entity_type="buildium_task",
            entity_id=enriched_task.get("id"),
            user_id=user.user_id,
            raw=activity_raw,
        ),
    )

    # Stream the confirmation to any client subscribed via the new SSE
    # thread endpoint (PR #70). Best-effort: a missing or unknown
    # session_id is a no-op so polling clients aren't affected.
    summary_for_stream = (
        result.get("summary") if isinstance(result, dict) else None
    ) or description
    try:
        await publish_execute_narration(
            request,
            body.session_id,
            tool_id=tool_id,
            summary_after=f"Done. {summary_for_stream}",
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Could not publish execute narration to session %s",
            body.session_id,
        )

    return ExecuteActionResponse(status="executed", result=result)


# ── Phase A.2.5 — deeper-followup endpoint ────────────────────────


@router.post("/events/{event_id}/actions/{action_index}/followup")
async def event_action_followup(
    event_id: UUID,  # PR #94 — 422 on non-UUID, no asyncpg 500
    action_index: int,
    body: FollowupRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Generate the next click-chain question or signal done.

    Called by the ActionChainModal when the user picks an option
    flagged ``needs_followup: true`` (or types into the modal's
    "Other…" expander). Returns the same shape the planner's
    ``first_followup`` field uses — the frontend renders it
    identically.

    Never persists. The chain lives in frontend state until
    ``/execute``.
    """
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()

    row = await pool.fetchrow(
        """
        SELECT id, event_name, action_surface, enrichment, executed_action_index
        FROM buildium_events
        WHERE id = $1 AND company_id = $2
        """,
        event_id,
        company_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="event not found")
    if row["executed_action_index"] is not None:
        raise HTTPException(
            status_code=409,
            detail="event already executed; cannot continue click-chain",
        )

    action_surface = _maybe_jsonb(row["action_surface"]) or {}
    actions = action_surface.get("actions") if isinstance(action_surface, dict) else None
    if not isinstance(actions, list) or not 0 <= action_index < len(actions):
        raise HTTPException(status_code=400, detail="action_index out of bounds")

    action_dict = actions[action_index]
    tool_id = (action_dict or {}).get("tool_id")
    if not isinstance(tool_id, str):
        raise HTTPException(status_code=400, detail="action has no tool_id")

    # Coerce the action dict into a ProposedAction so the planner sees
    # a typed handle. The action came from the planner originally, so
    # validation should always succeed; if it doesn't, the row is
    # corrupted and we 500 (caught and logged).
    try:
        action = ProposedAction.model_validate(action_dict)
    except Exception as exc:  # noqa: BLE001
        logger.exception("event %s has malformed action_surface", event_id)
        raise HTTPException(
            status_code=500,
            detail="event action_surface is malformed",
        ) from exc

    event_summary: str | None = None
    if isinstance(action_surface, dict):
        summary_val = action_surface.get("summary")
        if isinstance(summary_val, str):
            event_summary = summary_val

    planner = ActionPlanner(get_settings())
    try:
        followup = await planner.followup(
            company_id=company_id,
            action=action,
            previous_picks=[pick.model_dump(mode="json") for pick in body.previous_picks],
            freeform_hint=body.freeform_hint,
            event_summary=event_summary,
        )
    except PlannerError as exc:
        logger.warning(
            "followup planner failed for event %s tool %s: %s",
            event_id,
            tool_id,
            exc,
        )
        raise HTTPException(
            status_code=502,
            detail={
                "error": (
                    "Couldn't generate the next question — please switch to "
                    "free text or execute with what we have."
                ),
                "tool_id": tool_id,
            },
        ) from exc

    # The frontend discriminates on the ``done`` key, so we emit the
    # raw model_dump rather than coercing into a typed response model
    # (FastAPI doesn't union response_model nicely in v0.115).
    return followup.model_dump(mode="json")


@router.post("/events/{event_id}/freeform")
async def submit_freeform_note(
    event_id: UUID,  # PR #94 — 422 on non-UUID, no asyncpg 500
    body: FreeformNoteRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, str]:
    """Record a free-text follow-up the PM typed into the Inbox card
    AND invoke the orchestrator agent so the user gets a real AI
    response back through the inbox SSE stream.

    PR #130 — was: just persist the note to ``helixis_activity_log``
    as a placeholder while ``Phase A.3`` was outstanding. The inbox
    decision buttons (PR #129) now route through this endpoint as
    their primary submit path, so the UX of "click button → AI
    thinks → response" requires the agent to actually run here.

    The activity-log write stays (audit trail). The agent invocation
    is started as a background task — the response is returned to
    the client immediately so the freeform input's "✓ Sent" pill
    fires fast, and the agent's streamed output lands on the inbox
    SSE bus session (``webhook:{event_id}``) where the front-end
    is already subscribed. Render-side wiring already handles the
    assistant_message_* events via ``ConversationThread`` →
    ``AssistantBubble``.
    """
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()

    row = await pool.fetchrow(
        """
        SELECT id, event_name, account_id, enrichment
        FROM buildium_events
        WHERE id = $1 AND company_id = $2
        """,
        event_id,
        company_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="event not found")

    enrichment = _maybe_jsonb(row["enrichment"]) or {}
    enriched_task = enrichment.get("task") if isinstance(enrichment, dict) else {}
    enriched_task = enriched_task if isinstance(enriched_task, dict) else {}

    await write_activity(
        pool,
        ActivityEntry(
            account_id=int(row["account_id"]) if row["account_id"] is not None else None,
            company_id=company_id,
            source="manual",
            source_label=row["event_name"],
            action_type="freeform_note",
            description=body.note.strip(),
            entity_type="buildium_task",
            entity_id=enriched_task.get("id"),
            user_id=user.user_id,
            # PR #94 — coerce UUID → str for the JSONB ``raw`` blob
            # since UUID objects aren't JSON-native and the
            # ``write_activity`` serializer treats this dict opaquely.
            raw={"note": body.note, "event_id": str(event_id)},
        ),
    )

    # PR #130 — invoke the orchestrator agent so the user gets a
    # real response back. The agent publishes to the inbox SSE bus
    # session keyed to ``webhook:{event_id}`` — the frontend is
    # already subscribed via the thread/stream endpoint and renders
    # assistant_message events via the existing ConversationThread
    # plumbing.
    enriched_unit = enrichment.get("unit") if isinstance(enrichment, dict) else {}
    enriched_unit = enriched_unit if isinstance(enriched_unit, dict) else {}

    context: dict[str, Any] = {
        "inbox_event_id": str(event_id),
        "inbox_event_name": row["event_name"],
    }
    # Thread the row's anchors so the agent doesn't have to call
    # Buildium again just to know which property / task it's talking
    # about. The agent's existing buildium_* tools handle deeper
    # lookups when needed.
    if enriched_task.get("title"):
        context["inbox_task_title"] = enriched_task["title"]
    if enriched_task.get("id"):
        context["inbox_task_id"] = enriched_task["id"]
    if enriched_unit.get("address_line1"):
        context["inbox_property_address"] = enriched_unit["address_line1"]
    if enriched_unit.get("unit_number"):
        context["inbox_unit_number"] = enriched_unit["unit_number"]
    if enriched_task.get("description"):
        # Cap at 1000 chars so a paste of a giant tenant email
        # doesn't blow the context window on the planner side.
        context["inbox_task_description"] = str(enriched_task["description"])[:1000]

    # Publish to the client's LIVE inbox stream session (the ``thread:{uuid}``
    # it opened via subscribeInboxThread and now passes here), NOT a hardcoded
    # ``webhook:{event_id}`` the browser never subscribes to. The old hardcode
    # meant the agent's reply + any confirm_gate went to a dead channel — the
    # user saw "Note recorded" and nothing else, and gated writes timed out at
    # 300s. Fall back to the per-event session only for legacy callers that
    # don't send one.
    session_id = (body.session_id or "").strip() or f"webhook:{event_id}"
    bus = request.app.state.event_bus
    settings = request.app.state.settings
    composio_client = getattr(request.app.state, "composio_client", None)

    # bus.publish auto-creates the session, but the SSE stream
    # almost always created it first (the user is viewing the row
    # when they submit). Create defensively here too so the
    # asyncio.create_task path doesn't race against the SSE
    # subscribe.
    bus.create_session(session_id)

    agent_request = AgentRequest(
        task=body.note.strip(),
        company_id=company_id,
        session_id=session_id,
        context=context,
        source="inbox_freeform",
    )
    # Session lock (live-watch PR) — same rule as /agent/run: one driving
    # run per session. A double-submit on the same inbox row 409s instead
    # of interleaving two runs into the shared stream + transcript.
    if not run_registry.claim(session_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An agent run is already active for this inbox item.",
        )
    run_task = asyncio.create_task(
        run_agent_streaming(
            agent_request,
            settings,
            bus,
            composio_client=composio_client,
            user_id=user.user_id,
        )
    )
    run_task.add_done_callback(lambda _t: run_registry.release(session_id))

    return {"status": "recorded"}


# ─────────────────────────────────────────────────────────────────────
# PR #89 — user-triggered replan.
#
# When the planner times out or fails, the inbox renders a Replan
# pill that POSTs here. We reset the row's planner_status to 'pending'
# (clearing planner_error + planner_attempts) so the worker re-runs
# the planner on its next claim pass. Polling on the frontend picks
# up the new status within 15s.
#
# ``source`` is forward-compat for non-Buildium event sources
# (LeadSimple, Gmail) — today only "buildium" is supported and the
# allow-list lives in ``_PLANNER_SOURCE_TABLES`` below.
# ─────────────────────────────────────────────────────────────────────


_PLANNER_SOURCE_TABLES: dict[str, str] = {
    "buildium": "buildium_events",
    # Future: "leadsimple": "leadsimple_events", "gmail": "gmail_events",
}


class ReplanRequest(BaseModel):
    """``POST /events/{event_id}/replan`` body. ``source`` is optional
    so the existing frontend can post an empty body and default to
    buildium; future surfaces (LeadSimple, Gmail) pass an explicit
    source string."""

    model_config = ConfigDict(extra="forbid")

    source: str = "buildium"


class ReplanResponse(BaseModel):
    event_id: str
    planner_status: str  # always "pending" on success
    message: str


@router.post("/events/{event_id}/replan", response_model=ReplanResponse)
async def replan_event(
    event_id: str,
    request: Request,
    body: ReplanRequest | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ReplanResponse:
    """Reset planner state so the worker re-runs the planner on this
    event. Triggered from the inbox Replan pill when planner_status
    is 'timed_out' or 'failed'."""
    from uuid import UUID

    from helixis.webhooks.queue import reset_planner_for_replan

    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)

    source = (body.source if body else "buildium").strip().lower()
    table = _PLANNER_SOURCE_TABLES.get(source)
    if table is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unsupported source: {source!r}",
        )

    # PR #89 — validate the path-param shape at the endpoint boundary
    # so malformed UUIDs return 400 rather than the queue helper
    # exploding with a ValueError → 500. The helper's signature stays
    # strict (UUID-only) — mirrors how the rest of the inbox endpoints
    # let SQL's ``::uuid`` cast catch bad ids at the DB layer with an
    # asyncpg exception; this endpoint just lifts that catch to the
    # Python layer for a cleaner 400.
    try:
        event_uuid = UUID(event_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="event_id must be a UUID",
        ) from None

    pool = get_pool()
    try:
        updated = await reset_planner_for_replan(
            pool,
            event_uuid,
            company_id=company_id,
            table_name=table,
        )
    except asyncpg.exceptions.UndefinedColumnError:
        # PR #89 columns missing — likely the migration hasn't been
        # applied yet. Surface that explicitly rather than a misleading
        # 404 so Brandon knows what to do.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="planner_status column missing — apply migration "
            "supabase/migrations/0017_buildium_events_planner_status.sql",
        ) from None

    if not updated:
        # 404 covers both "wrong id" and "wrong tenant" — we don't
        # disambiguate to avoid leaking event existence.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")

    return ReplanResponse(
        event_id=event_id,
        planner_status="pending",
        message="planner queued for replan",
    )


# ─────────────────────────────────────────────────────────────────────
# PR #92 — user-triggered resummarize.
#
# Mirrors ``/replan`` exactly: reset ``summary_status='pending'``
# (clearing ``summary_error`` + ``summary_attempts``) so the worker
# re-runs the summarizer on the next claim pass. Inbox polls every
# 15s so the shimmer reappears immediately after click.
#
# Reuses ``_PLANNER_SOURCE_TABLES`` for the source → table mapping
# since both endpoints target the same table set. Future LeadSimple
# / Gmail support adds entries to that single dict.
# ─────────────────────────────────────────────────────────────────────


class ResummarizeRequest(BaseModel):
    """``POST /events/{event_id}/resummarize`` body. Same shape as
    ``ReplanRequest`` so the frontend can post an empty body and
    default to buildium."""

    model_config = ConfigDict(extra="forbid")

    source: str = "buildium"


class ResummarizeResponse(BaseModel):
    event_id: str
    summary_status: str  # always "pending" on success
    message: str


@router.post("/events/{event_id}/resummarize", response_model=ResummarizeResponse)
async def resummarize_event(
    event_id: str,
    request: Request,
    body: ResummarizeRequest | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ResummarizeResponse:
    """Reset summary state so the worker re-runs the summarizer on
    this event. Triggered from the inbox Resummarize pill when
    ``summary_status`` is ``'failed'`` (or when the user thinks the
    existing summary is wrong)."""
    from uuid import UUID

    from helixis.webhooks.queue import reset_summary_for_resummarize

    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)

    source = (body.source if body else "buildium").strip().lower()
    table = _PLANNER_SOURCE_TABLES.get(source)
    if table is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unsupported source: {source!r}",
        )

    # Same UUID-at-the-boundary validation as ``replan_event`` — the
    # queue helper's signature is strict-typed ``UUID`` so a
    # malformed path-param surfaces here as 400 rather than blowing
    # up further down with a less helpful message.
    try:
        event_uuid = UUID(event_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="event_id must be a UUID",
        ) from None

    pool = get_pool()
    try:
        updated = await reset_summary_for_resummarize(
            pool,
            event_uuid,
            company_id=company_id,
            table_name=table,
        )
    except asyncpg.exceptions.UndefinedColumnError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="summary_status column missing — apply migration "
            "supabase/migrations/0018_buildium_events_summary.sql",
        ) from None

    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")

    return ResummarizeResponse(
        event_id=event_id,
        summary_status="pending",
        message="summarizer queued for resummarize",
    )


# ── PR #70 — conversational Inbox thread stream ────────────────────


async def _replay_thread_into_bus(
    bus: Any,
    session_id: str,
    *,
    event_name: str,
    action_surface: dict[str, Any] | None,
    enrichment: dict[str, Any] | None,
    executed_action_index: int | None,
) -> None:
    """Push the persisted thread state for this event into the bus as a
    sequence of conversational events the frontend already knows how to
    render. This is the read-only replay path the SSE endpoint runs on
    every fresh subscribe.

    The replay produces, in order:
      1. (PR #127 — removed) The planner's ``summary`` USED to be
         emitted here as the opening assistant turn. The inbox detail
         pane now shows that summary inside the EventHeader card
         (via ``SummarySection``), so re-emitting it as a chat-bubble
         turn produced an "orphan paragraph" below the workflow card
         when the dedupe (``stripDuplicateSummaryTurn``) didn't catch
         it — e.g. when the SSE turn was emitted with the floor text
         BEFORE the planner overwrote ``action_surface.summary`` with
         its narrative, and the in-memory turn outlived the row
         update. The EventHeader card is now the only summary surface.
      2. One ``SUGGESTION_CHIP`` per ``ProposedAction``. ``action_index``
         is the position in the actions array so the frontend can POST
         back to ``/actions/{i}/followup`` or ``/execute`` later.
      3. If ``executed_action_index`` is set, one closing assistant turn
         confirming the prior write. Idempotent: re-subscribing after a
         row was executed shows the same closing turn.
    """
    if action_surface is None:
        # No planner output yet — emit a one-line narration the front-
        # end renders as italic gray text so the thread isn't empty.
        await bus.publish(
            session_id,
            narration_event(f"Waiting for Occupella to finish reading the {event_name} event."),
        )
        return

    actions = action_surface.get("actions") or []

    for i, action in enumerate(actions):
        if not isinstance(action, dict):
            continue
        label = str(action.get("label") or action.get("intent") or f"Action {i}")
        params = action.get("parameters") or {}
        await bus.publish(
            session_id,
            suggestion_chip_event(
                action_index=i,
                label=label,
                parameter_patch=params if isinstance(params, dict) else {},
                is_destructive=False,
            ),
        )

    if executed_action_index is not None and 0 <= executed_action_index < len(actions):
        executed = actions[executed_action_index]
        executed_intent = (executed or {}).get("intent") if isinstance(executed, dict) else None
        closing_turn = "replay-executed"
        await bus.publish(session_id, assistant_message_start_event(closing_turn))
        await bus.publish(
            session_id,
            assistant_message_token_event(
                closing_turn,
                f"Done — {executed_intent or 'action executed'}.",
            ),
        )
        await bus.publish(session_id, assistant_message_end_event(closing_turn))
    # Keep enrichment around for future use — the v1 replay doesn't need
    # it, but the parameter is intentionally unused rather than dropped
    # so the signature is stable for upcoming live-update calls.
    _ = enrichment


@router.get("/events/{event_id}/thread/stream")
async def event_thread_stream(
    request: Request,
    event_id: UUID,  # PR #94 — 422 on non-UUID, no asyncpg 500
    session_id: str | None = Query(default=None),
    user: AuthenticatedUser = Depends(get_current_user),
) -> StreamingResponse:
    """SSE stream of the conversational thread for one Buildium event.

    The client opens this stream when an event row is selected in the
    Inbox. The endpoint:

    1. Auth + company-scopes the row (404 on mismatch).
    2. Creates a fresh bus session keyed by ``session_id`` (client-
       generated UUID, defaults to ``webhook:{event_id}`` for caller-
       supplied determinism).
    3. Replays the persisted ``action_surface`` as conversational
       events into that session.
    4. Returns a streaming response that pumps the session's queue.

    Future writes (execute / followup / freeform) publish into the same
    session id so the stream picks them up live. The client passes its
    chosen ``session_id`` on every POST.
    """
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()

    try:
        row = await pool.fetchrow(
            """
            SELECT id, event_name, action_surface, enrichment, executed_action_index
              FROM buildium_events
             WHERE id = $1 AND company_id = $2
            """,
            event_id,
            company_id,
        )
    except (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.UndefinedColumnError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="buildium_events not provisioned for this environment",
        ) from None
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")

    bus = request.app.state.event_bus
    resolved_session = session_id or f"webhook:{event_id}"

    # ⚠ SESSION OWNERSHIP. The query above company-scopes the EVENT
    # (`WHERE id = $1 AND company_id = $2`). It says nothing about the
    # SESSION, which arrives as a query parameter — and the bus is keyed by
    # session id ALONE (streaming/bus.py: `publish` looks up
    # `self._sessions[session_id]` with no company in the key).
    #
    # So without this, a caller holding one event of their OWN could pass
    # another company's session id and receive that company's turn:
    # `create_session` is a no-op when the session exists, and `subscribe_seq`
    # replays the retained log (up to 1000 events) before going live. It leaks
    # in both directions — `_replay_thread_into_bus` below then publishes THIS
    # caller's action surface INTO that session, so the other company's
    # watchers see content that is not theirs.
    #
    # Same gate `/agent/watch` and `/agent/cancel` carry, and the same one
    # `/agent/run` was missing. `owner is None` is ALLOWED here for the reason
    # it is allowed there: a thread nobody has run yet has no run record, and
    # the default `webhook:{event_id}` form is reached only after the event
    # itself passed the company check above.
    if session_id:
        from helixis.db import engine as db_engine
        from helixis.db.repositories.orchestrator_runs import get_company_for_session

        try:
            pool_for_owner = db_engine.get_pool()
        except RuntimeError:
            pool_for_owner = None
        if pool_for_owner is not None:
            owner = await get_company_for_session(pool_for_owner, resolved_session)
            if owner is not None and owner not in user.company_ids:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="event not found")

    bus.create_session(resolved_session)

    await _replay_thread_into_bus(
        bus,
        resolved_session,
        event_name=row["event_name"],
        action_surface=_maybe_jsonb(row["action_surface"]),
        enrichment=_maybe_jsonb(row["enrichment"]),
        executed_action_index=row["executed_action_index"],
    )

    return StreamingResponse(
        sse_event_generator(bus, resolved_session),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Session-Id": resolved_session,
        },
    )


# Internal helper exposed for the execute / followup / freeform paths
# to publish narration + assistant turn events into a thread session.
# Used by the planner-side glue in those endpoints (followups). Kept as
# a module-level helper rather than a method on a class so the test
# suite can monkeypatch publish targets without instantiating a route
# fixture.


async def publish_execute_narration(
    request: Request,
    session_id: str | None,
    *,
    tool_id: str,
    summary_after: str,
) -> None:
    """Push a 2-event sequence to the per-session bus for the execute
    confirmation handshake. Called from inside the /execute endpoint
    AFTER the handler has succeeded but BEFORE the response is returned
    so a subscribed client sees the same confirmation the response
    body would have shown them via polling. No-op when ``session_id``
    is None — the client opted out of streaming."""
    if not session_id:
        return
    bus = request.app.state.event_bus
    bus.create_session(session_id)
    await bus.publish(session_id, narration_event(narrate_tool(tool_id)))
    turn_id = f"exec-{tool_id}"
    await bus.publish(session_id, assistant_message_start_event(turn_id))
    await bus.publish(session_id, assistant_message_token_event(turn_id, summary_after))
    await bus.publish(session_id, assistant_message_end_event(turn_id))


# Re-export the conversational event helpers so future call-sites
# (followup, freeform) can publish without re-importing through the
# common.events module. Intentional convenience — narrow surface.
__all__ = [
    "assistant_message_end_event",
    "assistant_message_start_event",
    "assistant_message_token_event",
    "confirm_gate_event",
    "narration_event",
    "publish_execute_narration",
    "router",
    "suggestion_chip_event",
]
