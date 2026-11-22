"""Propertyware: the connect surface (design/propertyware-connector.md, slice 2).

The read side (``helixis.propertyware``) could always load a
``company_propertyware_credentials`` row and sync from it; these routes are
what writes one, tests it, syncs it on demand and removes it. Two screens call
them: Settings → Connectors (``PropertywareApiDetail.tsx``) and the onboarding
wizard's Propertyware option (helixis-onboarding), which is built against the
request and response field names here (ADR 0003), so they do not change.

They copy Rentvine's behaviour (``api/routes/rentvine.py``), not its code:

  - an admin floor on every route, reads included;
  - the write, the test and Sync now refuse with a 503 while the deployment
    switch is off (``pms.registry.enabled``); the read and the disconnect do
    not (gotcha 120's asymmetry);
  - a workspace on another system gets the one-PMS trigger's refusal as a 409
    that names it (``one_pms_refusal``), never a 500;
  - the disconnect deletes the row, then purges the mirror, and is never
    described as revoking the key;
  - a passed test stamps ``last_tested_at`` and starts the first sync.

Where it differs, the reason is at the code: all three values are checked
before saving (each becomes a request header), the test reads ONE building,
and the sync status names each stale table with its reason, in words.
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
from helixis.pms import registry, sync_cycle
from helixis.pms.auth import header_value
from helixis.pms.errors import Failure, PmsApiError
from helixis.pms.http import operation_key
from helixis.pms.registry import PROPERTYWARE
from helixis.pms.sync_cycle import SyncRun
from helixis.propertyware import _paths, client, credentials
from helixis.propertyware import sync as pw_sync
from helixis.propertyware.purge import purge_propertyware_mirror
from helixis.security import CredentialCipher
from helixis.settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/propertyware", tags=["propertyware"])

#: What ``/test`` reads: the list the sync reads first, so a passed test says
#: the product's own first read works, not just that the keys parse. One
#: building (``limit=1``), as the design says: the count comes from
#: Propertyware's ``X-Total-Count``, never from the one row asked for.
_TEST_PATH = client.READS[0]

#: How long ``/test`` waits for Propertyware. The web app stops waiting for any
#: request at 45 s (``api.ts``), and the shared client's retries can outlast
#: that, so the bound is here, in code, with a sentence for when it is hit.
_TEST_TIMEOUT_S = 35.0

#: Bounds on what is pasted. Propertyware does not document the formats, so
#: they only catch a paste gone wrong: an empty or cut-off copy, or a page of
#: text. A secret is shown once and is password-like, hence its higher floor.
_SYSTEM_ID_MAX = 100
_CLIENT_ID_MIN, _CLIENT_ID_MAX = 4, 512
_CLIENT_SECRET_MIN, _CLIENT_SECRET_MAX = 8, 512


class PropertywareCredentialsInput(BaseModel):
    """What the customer pastes: the organization ID (sent as
    ``x-propertyware-system-id``) and the API key's client ID and secret.

    ⚠ The model never refuses what was sent; the route does. A failed model
    check answers 422 with the submitted value in the body (FastAPI echoes it,
    ``SecretStr`` or not), so anything that is not a string, and a body that
    is not an object, arrive here as empty strings, and the route refuses
    them with a sentence that repeats nothing.
    """

    system_id: str
    client_id: SecretStr
    client_secret: SecretStr

    @model_validator(mode="before")
    @classmethod
    def _never_refuse(cls, data: Any) -> dict[str, str]:
        sent = data if isinstance(data, dict) else {}
        return {
            name: value if isinstance(value := sent.get(name), str) else ""
            for name in cls.model_fields
        }


class PropertywareCredentialsStatus(BaseModel):
    """The masked read. Never a secret, and no last4 of the secret: Propertyware
    shows a secret once, so four characters of it would help nobody recognise
    anything (migration 0107 stores none). The client ID's last four are shown:
    Propertyware calls client IDs safe to share."""

    configured: bool
    #: The organization ID. Not a secret: it identifies the account.
    system_id: str | None = None
    client_id_last4: str | None = None
    #: Set only by a passed test; cleared whenever the keys are saved.
    last_tested_at: str | None = None
    #: Set only on the disconnect response.
    purged_rows: int | None = None
    #: Set only on the GET: whether this deployment accepts a Propertyware
    #: connection at all, so the panel and the wizard say so before anyone
    #: types keys rather than after the save's 503.
    connector_enabled: bool | None = None


class PropertywareTestResult(BaseModel):
    ok: bool
    #: Propertyware's own count of buildings (``X-Total-Count``), or ``None``
    #: when it did not say. Never 0 for "not said" (rule 14).
    properties_count: int | None = None
    error: str | None = None


class PropertywareSyncResult(BaseModel):
    """What a sync did, or is doing, in the field names of Rentvine's
    ``RentvineSyncResult``: the page and the wizard read both the same way.

    ``POST`` starts one and answers at once; ``GET`` reports it. A sync takes
    minutes and the web app stops waiting for a request at 45 s (gotcha 199).
    """

    ok: bool
    #: Rows written per mirror table, without its ``helixis_`` prefix
    #: (``{"units": 37}``). Keyed by table, not by freshness stage: two tables
    #: share each of two stages, and a stage key would show one table's count
    #: as the other's. A stale table is left out: its count is of a read that
    #: failed or stopped part-way, which is not the account's.
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
    """Admin floor on EVERY route here, reads included, for Rentvine's reason:
    the credentials are one row the whole team shares, a member replacing or
    deleting them breaks the connector for everyone, and the disconnect also
    deletes the mirror. Fails closed: ``require_role`` refuses an unknown role.

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
            f"Only an admin can {action} the Propertyware connection. Ask an admin of "
            "this workspace to do it."
        ),
    )


def _require_switched_on(refused: str) -> None:
    """Refuse a save, a test or a sync while the connector is switched off.

    Saving a connection on a deployment that will not sync it buys nothing and
    costs the customer their other systems: the one-PMS trigger refuses every
    other connection while the row exists (Rentvine's
    ``_require_connector_enabled``). The read and the disconnect stay open.
    """
    if not registry.enabled(PROPERTYWARE):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The Propertyware connector is switched off on this Occupella deployment, "
                f"so {refused}. To have it switched on, " + contact_support() + "."
            ),
        )


async def _fetch_row(pool: asyncpg.Pool, company_id: str) -> dict[str, Any] | None:
    """The masked status's columns, through the pool the route was given.
    The encrypted columns are never selected here."""
    row = await pool.fetchrow(
        """
        SELECT company_id, system_id, client_id_last4, last_tested_at
        FROM public.company_propertyware_credentials
        WHERE company_id = $1::uuid
        """,
        company_id,
    )
    return dict(row) if row else None


def _row_to_status(row: dict[str, Any] | None) -> PropertywareCredentialsStatus:
    if row is None:
        return PropertywareCredentialsStatus(configured=False)
    tested = row["last_tested_at"]
    return PropertywareCredentialsStatus(
        configured=True,
        system_id=row["system_id"],
        client_id_last4=row["client_id_last4"],
        last_tested_at=tested.isoformat() if tested else None,
    )


_BAD_SYSTEM_ID = (
    "That organization ID can't be used. Paste only your Propertyware organization ID, "
    "on one line, and save again."
)
_BAD_CLIENT_ID = (
    "That client ID can't be used. Copy the client ID of your Propertyware API key "
    "(Setup → Administration Setup → API Keys), paste only the ID, and save again."
)
_BAD_CLIENT_SECRET = (
    "That client secret can't be used. Paste only the secret, on one line. Propertyware "
    "shows a secret only once: if you no longer have it, create a new API key and paste "
    "its client ID and secret."
)


def _checked(value: str, *, longest: int, shortest: int = 1, refusal: str) -> str:
    """``value`` stripped, if it is whole and safe on a header line; else a 422
    with ``refusal``. Each value becomes a request header on every call, and
    ``header_value`` is what the loader and the client refuse it with later
    (``credentials.decrypt_row``, ``HeaderAuth``), so it is refused here
    first, in words. The 422 never repeats what was sent.
    """
    try:
        cleaned = header_value(value, what="a Propertyware credential")
    except ValueError:
        cleaned = ""
    if not shortest <= len(cleaned) <= longest:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=refusal)
    return cleaned


# ── Routes: the credentials ────────────────────────────────────────────


@router.get("/credentials")
async def get_credentials(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PropertywareCredentialsStatus:
    """Masked metadata for the saved connection, or ``configured: false``.
    Answers with the connector switched off: a connection saved before it was
    switched off still has to be seen to be removed."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="view")
    result = _row_to_status(await _fetch_row(pool, company_id))
    result.connector_enabled = registry.enabled(PROPERTYWARE)
    return result


@router.put("/credentials")
async def upsert_credentials(
    payload: PropertywareCredentialsInput,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PropertywareCredentialsStatus:
    """Check, encrypt and store the company's Propertyware connection."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="change")
    _require_switched_on("keys can't be saved")

    system_id = _checked(payload.system_id, longest=_SYSTEM_ID_MAX, refusal=_BAD_SYSTEM_ID)
    client_id = _checked(
        payload.client_id.get_secret_value(),
        shortest=_CLIENT_ID_MIN,
        longest=_CLIENT_ID_MAX,
        refusal=_BAD_CLIENT_ID,
    )
    cipher = CredentialCipher(get_settings())
    # Checked and encrypted in one expression, so no named local ever holds
    # the plaintext secret (an error report captures locals).
    encrypted_secret = cipher.encrypt(
        _checked(
            payload.client_secret.get_secret_value(),
            shortest=_CLIENT_SECRET_MIN,
            longest=_CLIENT_SECRET_MAX,
            refusal=_BAD_CLIENT_SECRET,
        )
    )
    try:
        # ⚠ ``base_url`` is never written: the column default holds the one
        # host, the loader never reads it, and the client sends every request
        # to its own constant. A stored value must never redirect a request
        # that carries the secret. ``last_tested_at`` is cleared on every save:
        # keys that changed have not passed a test. ``capabilities`` too: a
        # write refused under the old keys may work under these
        # (``credentials.refused_at``; the refusal tells the person to save).
        await pool.execute(
            """
            INSERT INTO public.company_propertyware_credentials (
                company_id, system_id, encrypted_client_id, client_id_last4,
                encrypted_client_secret, last_tested_at
            ) VALUES ($1::uuid, $2, $3, $4, $5, NULL)
            ON CONFLICT (company_id) DO UPDATE SET
                system_id               = EXCLUDED.system_id,
                encrypted_client_id     = EXCLUDED.encrypted_client_id,
                client_id_last4         = EXCLUDED.client_id_last4,
                encrypted_client_secret = EXCLUDED.encrypted_client_secret,
                last_tested_at          = NULL,
                capabilities            = '{}'::jsonb
            """,
            company_id,
            system_id,
            cipher.encrypt(client_id),
            cipher.last4(client_id),
            encrypted_secret,
        )
    except asyncpg.exceptions.UniqueViolationError as exc:
        # The one way this fires is the one-PMS trigger (migration 0107):
        # the company holds another system's credentials. The upsert's own
        # conflict is resolved by DO UPDATE. Re-saving Propertyware keys still
        # works: the trigger runs for the proposed row (a BEFORE INSERT
        # trigger does, even when ON CONFLICT then updates), but it looks only
        # at the OTHER systems' tables.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=one_pms_refusal(PROPERTYWARE, exc)
        ) from None

    logger.info("propertyware credentials saved for company=%s", company_id)
    # Re-read rather than echo: the answer is what the row now holds (rule 6).
    return _row_to_status(await _fetch_row(pool, company_id))


@router.delete("/credentials")
async def delete_credentials(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PropertywareCredentialsStatus:
    """Delete the connection AND purge every mirror row for the company.

    ⚠ This does NOT revoke the key inside Propertyware; no surface may call
    it a revocation. Reconnecting re-syncs everything Propertyware still holds.

    ⚠ Open with the connector switched off, on purpose: while the row exists
    the one-PMS trigger refuses every other system's connection.
    """
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="disconnect")
    await pool.execute(
        "DELETE FROM public.company_propertyware_credentials WHERE company_id = $1::uuid",
        company_id,
    )
    # The purge runs AFTER the row is gone: a sync that starts now finds no
    # credentials, and one already reading finds them gone before it writes
    # (``sync._still_connected``). Idempotent, so a raise here is a retryable
    # 500 with the row already deleted.
    deleted = await purge_propertyware_mirror(pool, company_id)
    logger.info("propertyware disconnected for company=%s", company_id)
    return PropertywareCredentialsStatus(configured=False, purged_rows=sum(deleted.values()))


# ── Routes: the test ───────────────────────────────────────────────────

#: The design's rule: a 401 or a 403 on the test names all three values,
#: because the status for a wrong organization ID is not documented (PW-2,
#: marked where the sync's reasons make the same guess: ``sync.read_failure``).
_ALL_THREE = "the organization ID, client ID and client secret"


def _refused_advice() -> str:
    """The 403 sentence, decided by the test read's own entry in the index:
    a beta-marked read needs Propertyware's beta program, any other needs the
    key's permission. Never "reconnect": the key itself was accepted (gotcha 102)."""
    key = operation_key("GET", _TEST_PATH)
    if key in _paths.BETA:
        needs = (
            "Reading buildings needs Propertyware's beta program: ask Propertyware support "
            "to add your account."
        )
    else:
        needs = (
            f"The API key lacks {_paths.PERMISSIONS[key]}: create a key in Propertyware that "
            "has it, and save it here."
        )
    return (
        f"Propertyware refused to let this API key read buildings. {needs} If the key "
        f"already has it, check that {_ALL_THREE} all belong to the same Propertyware "
        "account."
    )


#: What to tell the customer for each classified failure, keyed on ``Failure``
#: so a 401 and a 403 can never share an instruction (gotcha 102).
_TEST_ADVICE: dict[Failure, str] = {
    Failure.CREDENTIALS: (
        f"Propertyware did not accept these credentials. Check all three, {_ALL_THREE}, "
        "and save them again. Propertyware shows a secret only once: if you no longer have "
        "it, create a new API key (Setup → Administration Setup → API Keys) and save its "
        "client ID and secret."
    ),
    Failure.PERMISSION: _refused_advice(),
    Failure.NOT_FOUND: (
        "Propertyware could not find your buildings with these credentials. Check the "
        "organization ID and save the keys again; if it is right, " + contact_support() + "."
    ),
    Failure.BAD_REQUEST: (
        "Propertyware rejected the request as malformed. That is an Occupella bug, not "
        "anything wrong with what you entered: " + contact_support() + "."
    ),
    Failure.CONFLICT: (
        "Propertyware answered the test in a way Occupella does not expect. Nothing you "
        "entered needs to change: " + contact_support() + "."
    ),
    Failure.NEEDS_OVERRIDE: (
        "Propertyware answered the test in a way Occupella does not expect. Nothing you "
        "entered needs to change: " + contact_support() + "."
    ),
    # Occupella's own circuit breaker, not an answer from Propertyware.
    Failure.UNAVAILABLE: (
        "Propertyware isn't responding right now, so Occupella has paused calls to it for "
        "a few minutes. Nothing is wrong with what you entered: test again in about ten "
        "minutes."
    ),
}

#: 429 and every 5xx, after the client has already retried them.
_TEST_ADVICE_UNDOCUMENTED = (
    "Propertyware did not give a usable answer, after several tries. Nothing is wrong with "
    "what you entered: test again in a few minutes, and if it keeps failing, "
    + contact_support()
    + "."
)

_TEST_TIMED_OUT = (
    "Propertyware did not answer in time. Nothing is wrong with what you entered: test "
    "again in a few minutes, and if it keeps happening, " + contact_support() + "."
)

#: A failure inside Occupella, not an answer from Propertyware.
_TEST_CRASHED = (
    "Something went wrong on Occupella's side while checking your Propertyware connection. "
    "Nothing you entered needs to change. Test once more, and if it fails again, "
    + contact_support()
    + "."
)

_NO_CONNECTION = (
    "No Propertyware connection is saved for this workspace. Save your organization ID, "
    "client ID and client secret first."
)

_UNUSABLE_ROW = (
    "Occupella could not read the saved Propertyware keys. Save the organization ID, "
    "client ID and client secret again, and if that does not fix it, " + contact_support() + "."
)


def _stated_total(headers: Mapping[str, str]) -> int | None:
    """Propertyware's own count (``X-Total-Count``), or ``None`` when it did
    not say. Never the number of rows in the answer: that is the one asked for."""
    wanted = client.PAGING.total_header.lower()
    raw = next((v for k, v in headers.items() if k.lower() == wanted), None)
    if raw is None:
        return None
    try:
        total = int(str(raw).strip())
    except ValueError:
        return None
    return total if total >= 0 else None


@router.post("/test")
async def test_connection(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PropertywareTestResult:
    """Read one building with the saved credentials. A pass stamps
    ``last_tested_at`` and starts the first sync; nothing else does either."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="test")
    _require_switched_on("the connection can't be tested")

    try:
        creds = await credentials.load_credentials(pool, company_id, settings=get_settings())
    except credentials.PropertywareCredentialsError:
        return PropertywareTestResult(ok=False, error=_UNUSABLE_ROW)
    if creds is None:
        return PropertywareTestResult(ok=False, error=_NO_CONNECTION)

    try:
        async with client.build_client(creds) as pw:
            answer = await asyncio.wait_for(
                pw.get(_TEST_PATH, query={"limit": 1}), timeout=_TEST_TIMEOUT_S
            )
    except PmsApiError as exc:
        # The status and Propertyware's own text stay in the log; the client
        # has already masked the credentials in it.
        logger.warning("propertyware /test failed for company=%s: %s", company_id, exc)
        return PropertywareTestResult(
            ok=False, error=_TEST_ADVICE.get(exc.failure, _TEST_ADVICE_UNDOCUMENTED)
        )
    except TimeoutError:
        logger.warning("propertyware /test timed out for company=%s", company_id)
        return PropertywareTestResult(ok=False, error=_TEST_TIMED_OUT)
    except Exception:  # noqa: BLE001 — the test must answer, never 500
        logger.warning("propertyware /test crashed for company=%s", company_id, exc_info=True)
        return PropertywareTestResult(ok=False, error=_TEST_CRASHED)

    if not isinstance(answer.body, list):
        # A 200 that is not a list of buildings proves nothing about the keys.
        logger.warning(
            "propertyware /test for company=%s: %s answered %s, not a list",
            company_id,
            _TEST_PATH,
            type(answer.body).__name__,
        )
        return PropertywareTestResult(ok=False, error=_TEST_CRASHED)

    await pool.execute(
        """
        UPDATE public.company_propertyware_credentials
        SET last_tested_at = now()
        WHERE company_id = $1::uuid
        """,
        company_id,
    )
    try:
        # The first sync starts now, not at the worker's next tick, so the
        # wizard's launch screen does not scan an empty mirror.
        sync_cycle.start_now(pool, PROPERTYWARE, company_id)
    except Exception:  # noqa: BLE001 — a passed test stays passed
        logger.warning(
            "could not start the first Propertyware sync for company=%s", company_id, exc_info=True
        )
    return PropertywareTestResult(ok=True, properties_count=_stated_total(answer.headers))


# ── Routes: the sync ───────────────────────────────────────────────────

_SYNC_DID_NOT_FINISH = (
    "The Propertyware sync didn't finish. Nothing you entered needs to change: sync again "
    "in a few minutes, and if it keeps failing, " + contact_support() + "."
)
_SYNC_NOT_KNOWN = (
    "Occupella has no record of a Propertyware sync since it last restarted. Click Sync now "
    "to run one."
)


def _short(table: str) -> str:
    return table.removeprefix("helixis_")


def _listed(words: list[str]) -> str:
    """``["units", "leases", "bills"]`` as "Units, leases and bills"."""
    text = words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]
    return text[:1].upper() + text[1:]


def _stale_sentence(failed: Mapping[str, str]) -> str:
    """Which tables did not sync and why, each cause said once after the
    tables it stopped. The reasons are the sync's own sentences
    (``pw_sync.stale_reason``); the log keeps the technical ones."""
    order = list(pw_sync.TABLE_WORDS)
    by_reason: dict[str, list[str]] = {}
    for table in sorted(failed, key=lambda t: (order.index(t) if t in order else len(order), t)):
        words = pw_sync.TABLE_WORDS.get(table, _short(table).replace("_", " "))
        by_reason.setdefault(pw_sync.stale_reason(failed[table]), []).append(words)
    causes = " ".join(f"{_listed(words)}: {reason}" for reason, words in by_reason.items())
    return (
        "Some of Propertyware didn't sync this time, so what's on screen for it is from the "
        f"last sync that worked. {causes}"
    )


def _ended(run: SyncRun) -> PropertywareSyncResult:
    """How a finished run ended, in the page's terms."""
    started_at = run.started_at.isoformat()
    result = run.result
    if result is None:
        return PropertywareSyncResult(ok=False, error=_SYNC_DID_NOT_FINISH, started_at=started_at)
    stale = sorted(_short(t) for t in result.failed)
    return PropertywareSyncResult(
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
) -> PropertywareSyncResult:
    """Start re-reading Propertyware into the mirror (Settings → Connectors'
    Sync now). Answers at once; ``GET /propertyware/sync`` reports the run
    until it ends. A click while a sync is going (the one a passed test
    started, or a second click) waits on THAT run (``sync_cycle.start_now``).
    Admin-only, because a sync spends the account's API quota.

    ⚠ Sync now reads everything whole (``pw_sync.ask_for_full_pass``), never
    only the changes since the last pass: the page shows the run's counts as
    "Synced 12 buildings, …", and a window's would read "0 buildings"."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="sync")
    _require_switched_on("Propertyware can't be synced")
    if await _fetch_row(pool, company_id) is None:
        return PropertywareSyncResult(ok=False, error=_NO_CONNECTION)
    await pw_sync.ask_for_full_pass(pool, company_id)
    run = sync_cycle.start_now(pool, PROPERTYWARE, company_id)
    if run.finished_at is not None:
        # The background task could not even be created; the cycle freed the
        # slot, so a later click does not wait on a run that never ends.
        return _ended(run)
    return PropertywareSyncResult(ok=True, running=True, started_at=run.started_at.isoformat())


@router.get("/sync")
async def sync_status_route(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PropertywareSyncResult:
    """The Propertyware sync running in this process now, or how the last one
    ended. What the page polls after ``POST /propertyware/sync``. Open with the
    connector switched off: it reads, and starts nothing."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="sync")
    run = sync_cycle.sync_status(PROPERTYWARE, company_id)
    if run is None:
        return PropertywareSyncResult(ok=False, error=_SYNC_NOT_KNOWN)
    if run.finished_at is None:
        return PropertywareSyncResult(ok=True, running=True, started_at=run.started_at.isoformat())
    return _ended(run)


__all__ = ["router"]
