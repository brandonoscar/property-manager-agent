"""DoorLoop: the connect surface (design/doorloop-connector.md, slices A0, A1).

These routes write one company's DoorLoop API key, test it, sync it on demand
and remove it. Settings → Connectors (``DoorLoopApiDetail.tsx``) calls them;
the onboarding wizard's DoorLoop option is not built yet, and when it is it
will be built against the request and response field names here (ADR 0003).

They copy Propertyware's (``api/routes/propertyware.py``), not its code:

  - an admin floor on every route, reads included;
  - the write and the test refuse with a 503 while the deployment switch is
    off (``pms.registry.enabled``); the read and the disconnect do not
    (gotcha 120's asymmetry);
  - a workspace on another system gets the one-PMS trigger's refusal as a 409
    that names it (``one_pms_refusal``), never a 500;
  - the disconnect deletes the row, then purges the mirror, and is never
    described as revoking the key;
  - a passed test stamps ``last_tested_at`` and starts the first sync;
  - Sync now answers at once and the page polls the run (``sync_cycle``), and
    the sync status names each stale table with its reason, in words.

Where they differ: there is one value (the API key), and Sync now needs no
"full pass" request, because every DoorLoop pass reads every list whole
(``doorloop.sync``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, SecretStr, model_validator

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.rate_limit import connectors_limiter
from helixis.common.support import contact_support
from helixis.db.company_pms import one_pms_refusal
from helixis.db.engine import get_pool
from helixis.doorloop import client, credentials
from helixis.doorloop import sync as dl_sync
from helixis.doorloop.purge import purge_doorloop_mirror
from helixis.pms import registry, sync_cycle
from helixis.pms.auth import header_value
from helixis.pms.errors import Failure, PmsApiError
from helixis.pms.registry import DOORLOOP
from helixis.pms.sync_cycle import SyncRun
from helixis.security import CredentialCipher
from helixis.settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/doorloop", tags=["doorloop"])

#: How long ``/test`` waits for DoorLoop. The web app stops waiting for any
#: request at 45 s (``api.ts``), and the shared client's retries can outlast
#: that, so the bound is here, in code, with a sentence for when it is hit.
_TEST_TIMEOUT_S = 35.0

#: Bounds on what is pasted. DoorLoop does not document its key format to this
#: build, so they only catch a paste gone wrong: an empty or cut-off copy, or
#: a page of text.
_API_KEY_MIN, _API_KEY_MAX = 16, 4096

#: Where a DoorLoop user makes a key, and the plan it needs. Said wherever the
#: key is asked for, so a customer on Starter or Pro learns it before looking
#: for a menu their plan does not have (design, "What must the customer be on?").
_WHERE_THE_KEY_IS = (
    "In DoorLoop, open Settings → Zapier & API Keys and create a key. API keys need "
    "DoorLoop's Premium plan."
)


class DoorLoopCredentialsInput(BaseModel):
    """What the customer pastes: one API key.

    ⚠ The model never refuses what was sent; the route does. A failed model
    check answers 422 with the submitted value in the body (FastAPI echoes it,
    ``SecretStr`` or not), so anything that is not a string, and a body that
    is not an object, arrive here as an empty string, and the route refuses it
    with a sentence that repeats nothing.
    """

    api_key: SecretStr

    @model_validator(mode="before")
    @classmethod
    def _never_refuse(cls, data: Any) -> dict[str, str]:
        sent = data if isinstance(data, dict) else {}
        return {
            name: value if isinstance(value := sent.get(name), str) else ""
            for name in cls.model_fields
        }


class DoorLoopCredentialsStatus(BaseModel):
    """The masked read. Never the key; its last four characters only."""

    configured: bool
    api_key_last4: str | None = None
    #: Set only by a passed test; cleared whenever the key is saved.
    last_tested_at: str | None = None
    #: Set only on the disconnect response.
    purged_rows: int | None = None
    #: Set only on the GET: whether this deployment accepts a DoorLoop
    #: connection at all, so the panel says so before anyone pastes a key
    #: rather than after the save's 503.
    connector_enabled: bool | None = None


class DoorLoopTestResult(BaseModel):
    ok: bool
    #: DoorLoop's own count of properties, when its answer states one, else
    #: ``None``. Never 0 for "not said" (rule 14).
    properties_count: int | None = None
    error: str | None = None


class DoorLoopSyncResult(BaseModel):
    """What a sync did, or is doing, in the field names of Propertyware's
    ``PropertywareSyncResult``: the page reads both the same way.

    ``POST`` starts one and answers at once; ``GET`` reports it. A pass takes
    minutes and the web app stops waiting for a request at 45 s (gotcha 199).
    """

    ok: bool
    #: Rows written per mirror table, without its ``helixis_`` prefix
    #: (``{"units": 37}``). Keyed by table, not by freshness stage: two tables
    #: share the leases stage. A stale table is left out: its count is of a
    #: read that failed or stopped part-way, which is not the account's.
    rows: dict[str, int] = Field(default_factory=dict)
    #: Tables that did not sync this time, by the same names. What is on
    #: screen for them is from the last sync that worked, never empty.
    stale: list[str] = Field(default_factory=list)
    #: For a stale table, names it and why, in words a person can act on.
    error: str | None = None
    #: True while the sync this answer describes is still running.
    running: bool = False
    #: When that sync began (ISO, server clock). The page waits for the run
    #: with THIS start, so it never reports an older run as its own.
    started_at: str | None = None


# ── Helpers ────────────────────────────────────────────────────────────


def _require_company(user: AuthenticatedUser) -> str:
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
    """Admin floor on EVERY route here, reads included: the key is one row the
    whole team shares, a member replacing or deleting it breaks the connector
    for everyone, and the disconnect also deletes the mirror. Fails closed:
    ``require_role`` refuses an unknown role.

    Imported locally to avoid a module-level cycle with ``routes.company``.
    """
    from helixis.api.roles import ADMIN
    from helixis.api.routes.company import require_role

    await require_role(
        pool,
        user.user_id,
        company_id,
        ADMIN,
        detail=(
            f"Only an admin can {action} the DoorLoop connection. Ask an admin of this "
            "workspace to do it."
        ),
    )


def _require_switched_on(refused: str) -> None:
    """Refuse a save, a test or a sync while the connector is switched off.

    Saving a connection on a deployment that will not use it buys nothing and
    costs the customer their other systems: the one-PMS trigger refuses every
    other connection while the row exists. The read and the disconnect stay
    open.
    """
    if not registry.enabled(DOORLOOP):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The DoorLoop connector is switched off on this Occupella deployment, so "
                f"{refused}. To have it switched on, " + contact_support() + "."
            ),
        )


async def _fetch_row(pool: asyncpg.Pool, company_id: str) -> dict[str, Any] | None:
    """The masked status's columns. The encrypted key is never selected here."""
    row = await pool.fetchrow(
        """
        SELECT company_id, api_key_last4, last_tested_at
        FROM public.company_doorloop_credentials
        WHERE company_id = $1::uuid
        """,
        company_id,
    )
    return dict(row) if row else None


def _row_to_status(row: dict[str, Any] | None) -> DoorLoopCredentialsStatus:
    if row is None:
        return DoorLoopCredentialsStatus(configured=False)
    tested = row["last_tested_at"]
    return DoorLoopCredentialsStatus(
        configured=True,
        api_key_last4=row["api_key_last4"],
        last_tested_at=tested.isoformat() if tested else None,
    )


_BAD_API_KEY = (
    f"That API key can't be used. {_WHERE_THE_KEY_IS} Copy the whole key, paste only the "
    "key on one line, and save again."
)


def _checked_key(value: str) -> str:
    """``value`` stripped, if it is whole and safe on a header line; else a 422
    with :data:`_BAD_API_KEY`. The key becomes the bearer header on every call,
    and ``header_value`` is what the loader and the client refuse it with later
    (``credentials.decrypt_row``, ``client.BearerAuth``), so it is refused here
    first, in words. The 422 never repeats what was sent."""
    try:
        cleaned = header_value(value, what="a DoorLoop API key")
    except ValueError:
        cleaned = ""
    if not _API_KEY_MIN <= len(cleaned) <= _API_KEY_MAX:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=_BAD_API_KEY)
    return cleaned


def _sealed(cipher: CredentialCipher, key: str) -> tuple[str, str]:
    """The key encrypted, and its last four characters for the masked read."""
    return cipher.encrypt(key), cipher.last4(key)


# ── Routes: the credentials ────────────────────────────────────────────


@router.get("/credentials")
async def get_credentials(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> DoorLoopCredentialsStatus:
    """Masked metadata for the saved connection, or ``configured: false``.
    Answers with the connector switched off: a connection saved before it was
    switched off still has to be seen to be removed."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="view")
    result = _row_to_status(await _fetch_row(pool, company_id))
    result.connector_enabled = registry.enabled(DOORLOOP)
    return result


@router.put("/credentials")
async def upsert_credentials(
    payload: DoorLoopCredentialsInput,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> DoorLoopCredentialsStatus:
    """Check, encrypt and store the company's DoorLoop API key."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="change")
    _require_switched_on("an API key can't be saved")

    # Checked and sealed in one expression, so this frame never holds the
    # plaintext key in a named local (an error report captures locals).
    encrypted_key, last4 = _sealed(
        CredentialCipher(get_settings()), _checked_key(payload.api_key.get_secret_value())
    )
    try:
        # ⚠ ``base_url`` is never written: the column default holds the one
        # host, the loader never reads it, and the client sends every request
        # to its own constant. ``last_tested_at`` is cleared on every save: a
        # key that changed has not passed a test. ``capabilities`` too.
        await pool.execute(
            """
            INSERT INTO public.company_doorloop_credentials (
                company_id, encrypted_api_key, api_key_last4, last_tested_at
            ) VALUES ($1::uuid, $2, $3, NULL)
            ON CONFLICT (company_id) DO UPDATE SET
                encrypted_api_key = EXCLUDED.encrypted_api_key,
                api_key_last4     = EXCLUDED.api_key_last4,
                last_tested_at    = NULL,
                capabilities      = '{}'::jsonb
            """,
            company_id,
            encrypted_key,
            last4,
        )
    except asyncpg.exceptions.UniqueViolationError as exc:
        # The one way this fires is the one-PMS trigger (migration 0109): the
        # company holds another system's credentials. The upsert's own
        # conflict is resolved by DO UPDATE, and the trigger looks only at the
        # OTHER systems' tables, so re-saving a DoorLoop key still works.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=one_pms_refusal(DOORLOOP, exc)
        ) from None

    logger.info("doorloop credentials saved for company=%s", company_id)
    # Re-read rather than echo: the answer is what the row now holds (rule 6).
    return _row_to_status(await _fetch_row(pool, company_id))


@router.delete("/credentials")
async def delete_credentials(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> DoorLoopCredentialsStatus:
    """Delete the connection AND purge every mirror row for the company.

    ⚠ This does NOT revoke the key inside DoorLoop; no surface may call it a
    revocation.

    ⚠ Open with the connector switched off, on purpose: while the row exists
    the one-PMS trigger refuses every other system's connection.
    """
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="disconnect")
    await pool.execute(
        "DELETE FROM public.company_doorloop_credentials WHERE company_id = $1::uuid",
        company_id,
    )
    # The purge runs AFTER the row is gone, as Propertyware's does. Idempotent,
    # so a raise here is a retryable 500 with the row already deleted.
    deleted = await purge_doorloop_mirror(pool, company_id)
    logger.info("doorloop disconnected for company=%s", company_id)
    return DoorLoopCredentialsStatus(configured=False, purged_rows=sum(deleted.values()))


# ── Routes: the test ───────────────────────────────────────────────────

#: What to tell the customer for each classified failure, keyed on ``Failure``
#: so a wrong key and a key without access never share an instruction
#: (gotcha 102).
_TEST_ADVICE: dict[Failure, str] = {
    Failure.CREDENTIALS: (
        f"DoorLoop did not accept this API key. {_WHERE_THE_KEY_IS} Copy the whole key, "
        "save it here, and test again."
    ),
    # DoorLoop's 405 (``client.STATUS_MEANINGS``): the key was accepted and
    # its access role does not cover properties. Never "reconnect".
    Failure.PERMISSION: (
        "DoorLoop accepted this API key, but its access role can't read properties. In "
        "DoorLoop, give the key a role that can read properties, or create a key that has "
        "one, then save it here and test again."
    ),
    Failure.NOT_FOUND: (
        "DoorLoop answered that it has no properties list at the address Occupella used. "
        "That is an Occupella problem, not anything you entered: " + contact_support() + "."
    ),
    Failure.BAD_REQUEST: (
        "DoorLoop rejected the request as malformed. That is an Occupella bug, not anything "
        "wrong with what you entered: " + contact_support() + "."
    ),
    Failure.CONFLICT: (
        "DoorLoop answered the test in a way Occupella does not expect. Nothing you entered "
        "needs to change: " + contact_support() + "."
    ),
    Failure.NEEDS_OVERRIDE: (
        "DoorLoop answered the test in a way Occupella does not expect. Nothing you entered "
        "needs to change: " + contact_support() + "."
    ),
    # Occupella's own circuit breaker, not an answer from DoorLoop.
    Failure.UNAVAILABLE: (
        "DoorLoop isn't responding right now, so Occupella has paused calls to it for a few "
        "minutes. Nothing is wrong with what you entered: test again in about ten minutes."
    ),
}

#: 429 and every 5xx, after the client has already retried them.
#: Occupella held its calls because DoorLoop asked it to slow down: a 403 or
#: a 429 from DoorLoop's edge, or its quota headers nearly spent
#: (``client.EDGE_HOLD_S``, ``client.RATE_HEADERS``). Nothing was sent.
_TEST_HELD = (
    "DoorLoop asked Occupella to slow down, so Occupella has paused its calls to DoorLoop "
    "for a minute or two. Nothing is wrong with what you entered: test again in a few "
    "minutes."
)

_TEST_ADVICE_UNDOCUMENTED = (
    "DoorLoop did not give a usable answer, after several tries. Nothing is wrong with what "
    "you entered: test again in a few minutes, and if it keeps failing, " + contact_support() + "."
)

_TEST_TIMED_OUT = (
    "DoorLoop did not answer in time. Nothing is wrong with what you entered: test again in "
    "a few minutes, and if it keeps happening, " + contact_support() + "."
)

#: A failure inside Occupella, not an answer from DoorLoop.
_TEST_CRASHED = (
    "Something went wrong on Occupella's side while checking your DoorLoop connection. "
    "Nothing you entered needs to change. Test once more, and if it fails again, "
    + contact_support()
    + "."
)

_NO_CONNECTION = f"No DoorLoop connection is saved for this workspace. {_WHERE_THE_KEY_IS} " + (
    "Then save the key here."
)

_UNUSABLE_ROW = (
    "Occupella could not read the saved DoorLoop API key. Save the key again, and if that "
    "does not fix it, " + contact_support() + "."
)


def _stated_total(body: Any) -> int | None:
    """DoorLoop's own count of properties, when its answer states one, else
    ``None``. Never the number of rows on the page: that is a page."""
    # @unverified DL-5 — the list answer's shape: rows under "data" and the
    # count under "total" (a 2023 third-party client reads "data"); settled
    # by the test on a live key.
    total = body.get("total") if isinstance(body, dict) else None
    return total if isinstance(total, int) and not isinstance(total, bool) and total >= 0 else None


@router.post("/test")
async def test_connection(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> DoorLoopTestResult:
    """Read the first page of properties with the saved key. A pass stamps
    ``last_tested_at`` and starts the first sync; nothing else does either."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="test")
    _require_switched_on("the connection can't be tested")

    try:
        creds = await credentials.load_credentials(pool, company_id, settings=get_settings())
    except credentials.DoorLoopCredentialsError:
        return DoorLoopTestResult(ok=False, error=_UNUSABLE_ROW)
    if creds is None:
        return DoorLoopTestResult(ok=False, error=_NO_CONNECTION)

    try:
        async with client.build_client(creds) as dl:
            answer = await asyncio.wait_for(dl.get(client.TEST_READ), timeout=_TEST_TIMEOUT_S)
    except PmsApiError as exc:
        # The status and DoorLoop's own text stay in the log; the client has
        # already masked the key in it.
        logger.warning("doorloop /test failed for company=%s: %s", company_id, exc)
        if exc.failure is Failure.UNDOCUMENTED and not exc.sent:
            return DoorLoopTestResult(ok=False, error=_TEST_HELD)
        return DoorLoopTestResult(
            ok=False, error=_TEST_ADVICE.get(exc.failure, _TEST_ADVICE_UNDOCUMENTED)
        )
    except TimeoutError:
        logger.warning("doorloop /test timed out for company=%s", company_id)
        return DoorLoopTestResult(ok=False, error=_TEST_TIMED_OUT)
    except Exception:  # noqa: BLE001 — the test must answer, never 500
        logger.warning("doorloop /test crashed for company=%s", company_id, exc_info=True)
        return DoorLoopTestResult(ok=False, error=_TEST_CRASHED)

    if not isinstance(answer.body, dict | list):
        # A 200 with no JSON object or list proves nothing about the key.
        logger.warning(
            "doorloop /test for company=%s: %s answered %s, not JSON rows",
            company_id,
            client.TEST_READ,
            type(answer.body).__name__,
        )
        return DoorLoopTestResult(ok=False, error=_TEST_CRASHED)

    await pool.execute(
        """
        UPDATE public.company_doorloop_credentials
        SET last_tested_at = now()
        WHERE company_id = $1::uuid
        """,
        company_id,
    )
    try:
        # The first sync starts now, not at the worker's next tick, so the
        # copy is not empty for the hours until then.
        sync_cycle.start_now(pool, DOORLOOP, company_id)
    except Exception:  # noqa: BLE001 — a passed test stays passed
        logger.warning(
            "could not start the first DoorLoop sync for company=%s", company_id, exc_info=True
        )
    return DoorLoopTestResult(ok=True, properties_count=_stated_total(answer.body))


# ── Routes: the sync ───────────────────────────────────────────────────

_SYNC_DID_NOT_FINISH = (
    "The DoorLoop sync didn't finish. Nothing you entered needs to change: sync again in a "
    "few minutes, and if it keeps failing, " + contact_support() + "."
)
_SYNC_NOT_KNOWN = (
    "Occupella has no record of a DoorLoop sync since it last restarted. Click Sync now to run one."
)


def _short(table: str) -> str:
    return table.removeprefix("helixis_")


def _listed(words: list[str]) -> str:
    """``["units", "leases", "vendors"]`` as "Units, leases and vendors"."""
    text = words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]
    return text[:1].upper() + text[1:]


def _stale_sentence(failed: Mapping[str, str]) -> str:
    """Which tables did not sync and why, each cause said once after the
    tables it stopped. The reasons are the sync's own sentences
    (``dl_sync.stale_reason``); the log keeps the technical ones."""
    order = list(dl_sync.TABLE_WORDS)
    by_reason: dict[str, list[str]] = {}
    for table in sorted(failed, key=lambda t: (order.index(t) if t in order else len(order), t)):
        words = dl_sync.TABLE_WORDS.get(table, _short(table).replace("_", " "))
        by_reason.setdefault(dl_sync.stale_reason(failed[table]), []).append(words)
    causes = " ".join(f"{_listed(words)}: {reason}" for reason, words in by_reason.items())
    return (
        "Some of DoorLoop didn't sync this time, so what's on screen for it is from the last "
        f"sync that worked. {causes}"
    )


def _ended(run: SyncRun) -> DoorLoopSyncResult:
    """How a finished run ended, in the page's terms."""
    started_at = run.started_at.isoformat()
    result = run.result
    if result is None:
        return DoorLoopSyncResult(ok=False, error=_SYNC_DID_NOT_FINISH, started_at=started_at)
    stale = sorted(_short(t) for t in result.failed)
    return DoorLoopSyncResult(
        ok=not stale,
        rows={_short(t): n for t, n in result.rows_by_table.items() if t not in result.failed},
        stale=stale,
        error=_stale_sentence(result.failed) if stale else None,
        started_at=started_at,
    )


@router.post("/sync")
async def sync_now_route(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> DoorLoopSyncResult:
    """Start re-reading DoorLoop into the mirror (Settings → Connectors' Sync
    now). Answers at once; ``GET /doorloop/sync`` reports the run until it
    ends. A click while a sync is going (the one a passed test started, or a
    second click) waits on THAT run (``sync_cycle.start_now``). Admin-only,
    because a pass spends the key's hourly quota (``dl_sync.CALL_BUDGET``)."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="sync")
    _require_switched_on("DoorLoop can't be synced")
    if await _fetch_row(pool, company_id) is None:
        return DoorLoopSyncResult(ok=False, error=_NO_CONNECTION)
    run = sync_cycle.start_now(pool, DOORLOOP, company_id)
    if run.finished_at is not None:
        # The background task could not even be created; the cycle freed the
        # slot, so a later click does not wait on a run that never ends.
        return _ended(run)
    return DoorLoopSyncResult(ok=True, running=True, started_at=run.started_at.isoformat())


@router.get("/sync")
async def sync_status_route(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> DoorLoopSyncResult:
    """The DoorLoop sync running in this process now, or how the last one
    ended. What the page polls after ``POST /doorloop/sync``. Open with the
    connector switched off: it reads, and starts nothing."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="sync")
    run = sync_cycle.sync_status(DOORLOOP, company_id)
    if run is None:
        return DoorLoopSyncResult(ok=False, error=_SYNC_NOT_KNOWN)
    if run.finished_at is None:
        return DoorLoopSyncResult(ok=True, running=True, started_at=run.started_at.isoformat())
    return _ended(run)


__all__ = ["router"]
