"""Rentvine credential management. ADR 0010 Decision (e), Phase 6.9.

**Why this module exists at all.** ``rentvine/credentials.py`` has always been
able to READ ``company_rentvine_credentials`` and turn a row into a working
client. Nothing anywhere could WRITE one — the only way a credential had ever
reached that table was a hand-typed INSERT. So the whole connector, its sync,
its tools and its mirror were reachable only by an operator with database
access: gotcha 62's ``comms_triage`` shape at the scale of an onboarding step,
and the eleventh recorded instance in this repo.

It mirrors ``api/routes/buildium.py``'s credential half on purpose — same admin
floor, same masked read, same disconnect-purges rule — because the two
connectors are one product surface and a customer switching between them should
not meet two different security postures. Three things are deliberately NOT the
same, each for a reason written at its own route:

  - the WRITE route refuses while the connector is dark (``/credentials`` PUT);
  - the reads and the disconnect do NOT (gotcha 120's asymmetry);
  - a company already on Buildium gets an honest **409** rather than a 500,
    because migration 0100's ``assert_one_pms_per_company`` trigger refuses the
    INSERT and a raw ``UniqueViolationError`` reads as "Occupella is broken".

Two screens call these routes: the onboarding wizard's Rentvine step
(helixis-onboarding) and Settings → Connectors (``RentvineApiDetail.tsx``),
which ``GET /connectors`` lists while the connector is on or keys are stored
(``connectors.list_connectors``). Before that card, a revoked key could be
replaced nowhere in the app, while chat sent people to that page to do it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, SecretStr

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.rate_limit import connectors_limiter
from helixis.common.support import contact_support
from helixis.db.company_pms import one_pms_refusal
from helixis.db.engine import get_pool
from helixis.pms.registry import RENTVINE
from helixis.rentvine import discovery, transport
from helixis.rentvine.client import RentvineAPIError, RentvineClient
from helixis.rentvine.credentials import decrypt_rentvine_settings_from, fetch_rentvine_row
from helixis.rentvine.purge import purge_rentvine_mirror
from helixis.rentvine.signature import scheme_from_settings
from helixis.rentvine.sync import SYNC_STATE_STAGE
from helixis.rentvine.sync_cycle import (
    SyncRun,
    abandon_claim,
    claim_sync,
    run_claimed_sync,
    start_initial_sync,
    sync_status,
)
from helixis.security import CredentialCipher
from helixis.settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rentvine", tags=["rentvine"])

#: The collection ``/test`` reads, and it is deliberately the one the SYNC
#: reads first (``rentvine/sync.py::SOURCES[0]``) rather than a cheaper
#: endpoint chosen for being cheap. A connection test that exercises a path the
#: product never uses proves the credentials parse and nothing about whether
#: the product works — gotcha 85's rule (measure the surface the user is on)
#: pointed at a health check.
_TEST_PATH = "/properties"


class RentvineCredentialsInput(BaseModel):
    """Plaintext input for connecting a Rentvine account.

    ADR 0010 Decision (e): auth is HTTP Basic — the access key as username and
    the secret as password — against ``https://{account_code}.rentvine.com``.
    There is no third HMAC component; the original brief's "3-part credential"
    was half right.
    """

    #: The customer's subdomain, NOT a secret — it is in every request URL.
    #:
    #: ⚠ **OPTIONAL, because a customer should only have to paste the two
    #: keys.** Absent, the route discovers it (``rentvine/discovery.py``) and
    #: stores only a subdomain the credential actually authenticated against.
    #: Supplied, it is used as given — a customer who knows their own address
    #: bar must never be overruled by a guess.
    account_code: str | None = Field(default=None, max_length=100)
    # repr=False: Sentry records a failing handler's locals by repr, and this
    # body is one of them (``test_no_secrets_in_repr.py``).
    api_key: str = Field(min_length=4, repr=False)
    api_secret: str = Field(min_length=8, repr=False)


class RentvineCredentialsStatus(BaseModel):
    """Read-side view: never includes any plaintext secret.

    ⚠ There is no ``api_secret_last4`` and migration 0100 stores none. Rentvine
    shows a secret once, at creation; a last4 exists to help somebody recognise
    a key they can still look up, and there is no such surface for this one. So
    four characters of a secret would be exposure with no use.
    """

    configured: bool
    account_code: str | None = None
    api_key_last4: str | None = None
    last_tested_at: str | None = None
    #: Set only on the disconnect response (ADR 0010 e1).
    purged_rows: int | None = None
    #: Set only on the GET: whether this deployment will accept a Rentvine
    #: credential at all. The onboarding wizard reads it BEFORE asking for
    #: keys, so a customer on a deployment where the connector is off is told
    #: so up front instead of after pasting both keys and hitting the 503.
    connector_enabled: bool | None = None
    #: Whether a webhook signing key is stored. Never the key, and no last4
    #: (migration 0102's note: four characters of a key are exposure with no use).
    webhook_secret_saved: bool = False
    #: Set only on the GET: whether this deployment can check a Rentvine
    #: signature at all (the ``HELIXIS_RENTVINE__WEBHOOK_SIGNATURE_*`` settings).
    #: Until it can, the receiver refuses every delivery, so the card must not
    #: present webhooks as working (rule 14).
    webhook_scheme_ready: bool | None = None


class RentvineWebhookSecretInput(BaseModel):
    """The per-account signing key Rentvine shows under Actions → Webhooks.

    ⚠ ``SecretStr``, so the model's repr is masked and the key cannot reach a
    log line or an error report's local variables through it. Its length and
    shape are checked in the route rather than with ``Field`` constraints,
    because a failed constraint echoes the input back in the 422 body.
    """

    secret: SecretStr


class RentvineTestResult(BaseModel):
    ok: bool
    properties_count: int | None = None
    error: str | None = None


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
    """Admin gate on EVERY route here, reads included.

    Identical reasoning to ``buildium._require_admin`` and identical breadth:
    the credentials are a SINGLE per-company row the whole team shares, so a
    member overwriting or deleting them breaks the connector for everyone — and
    the disconnect additionally deletes the mirror. Fails CLOSED: an
    unverifiable role is refused, because ``require_role`` normalizes ``None``
    to deny.

    Imported locally to avoid a module-level cycle with ``routes.company``.
    """
    from helixis.api.roles import ADMIN
    from helixis.api.routes.company import require_role

    await require_role(
        pool,
        user.user_id,
        company_id,
        ADMIN,
        detail=f"Only an admin can {action} the Rentvine connection.",
    )


def _require_connector_enabled() -> None:
    """Refuse to STORE credentials while the connector is dark.

    ⚠ This is not a tidiness gate, and it is why the write is asymmetric with
    the read and the disconnect. Saving a credential on a deployment where
    ``rentvine.enabled`` is false buys the customer nothing — the sync cycle
    declines to run, the agent tools decline to open a client — and it
    ACTIVELY COSTS them something, because migration 0100's exclusivity trigger
    now refuses their Buildium connection (ADR 0010 a1). So the dark state is
    strictly worse than not connecting at all, and an honest refusal is the
    only answer that leaves them somewhere they can act from.
    """
    if not get_settings().rentvine.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The Rentvine connector is not enabled on this deployment yet, so "
                "saving credentials would connect nothing. " + contact_support()
            ),
        )


async def _fetch_row(pool: asyncpg.Pool, company_id: str) -> dict[str, Any] | None:
    """Read the encrypted row through a caller-supplied pool.

    Distinct from ``rentvine.credentials.fetch_rentvine_row`` for the same
    reason Buildium's is: the routes receive a pool the test suite injects,
    while the sync and the tools reach for the global one.
    """
    row = await pool.fetchrow(
        """
        SELECT company_id, account_code, encrypted_api_key, api_key_last4,
               encrypted_api_secret, encrypted_webhook_secret, base_url, last_tested_at
        FROM public.company_rentvine_credentials
        WHERE company_id = $1
        """,
        company_id,
    )
    return dict(row) if row else None


def _row_to_status(row: dict[str, Any] | None) -> RentvineCredentialsStatus:
    if row is None:
        return RentvineCredentialsStatus(configured=False)
    tested = row["last_tested_at"]
    return RentvineCredentialsStatus(
        configured=True,
        account_code=row["account_code"],
        api_key_last4=row["api_key_last4"],
        last_tested_at=tested.isoformat() if tested else None,
        # The receiver's own test (`decrypt_webhook_secret`): an empty value is no key.
        webhook_secret_saved=bool(row["encrypted_webhook_secret"]),
    )


#: What to tell the customer for each classified failure.
#:
#: ⚠ **Keyed on ``Failure``, deliberately NOT on ``_Meaning.operator_action``.**
#: That flag answers "is this the customer's to fix", which is true of both 401
#: and 403 — and those need OPPOSITE instructions. Gotcha 102 is the standing
#: record of collapsing exactly that pair: telling somebody to reconnect a
#: credential Rentvine has already accepted sends them to redo something that
#: works, while telling somebody with a revoked key to check a role sends them
#: nowhere at all.
_TEST_ADVICE: dict[transport.Failure, str] = {
    transport.Failure.CREDENTIALS: (
        "Rentvine rejected the access key or secret. Re-copy both from Rentvine "
        "(Settings → API) and save them again — the secret is shown only once, "
        "so a fresh key may be needed."
    ),
    transport.Failure.PERMISSION: (
        "Rentvine accepted the key and refused this request. The key's ROLE in "
        "Rentvine does not allow reading properties — fix the role there; the "
        "key itself is fine and does not need replacing."
    ),
    transport.Failure.NOT_FOUND: (
        "Rentvine does not recognise that account subdomain. Check the "
        "account code — it is the part before .rentvine.com in your URL."
    ),
    transport.Failure.BAD_REQUEST: (
        "Rentvine rejected the request as malformed. That is an Occupella bug "
        "rather than anything wrong with your account — " + contact_support()
    ),
    # Occupella's own circuit breaker, not an answer from Rentvine (rule 28).
    transport.Failure.UNAVAILABLE: (
        "Rentvine isn't responding right now, so Occupella has paused calls to it "
        "for a few minutes. Nothing is wrong with what you entered; test again in "
        "about ten minutes."
    ),
}

#: Anything the spec does not describe, including 429 and every 5xx. The
#: client has already retried a retryable one to exhaustion by the time this is
#: reached (``RETRY_BACKOFF_S``), so "try again" is advice the product has
#: already taken — say what is actually known instead.
_TEST_ADVICE_UNDOCUMENTED = (
    "Rentvine answered with an error Occupella does not recognise. Nothing is "
    "wrong with what you entered; the request did not get a usable answer."
)


#: A failure inside Occupella, not an answer from Rentvine.
_TEST_CRASHED = (
    "Something went wrong on Occupella's side while checking your Rentvine "
    "connection. Nothing you entered needs to change. Try the test once more, "
    "and if it fails again, " + contact_support() + "."
)


# ── Routes ─────────────────────────────────────────────────────────────


@router.get("/credentials")
async def get_credentials(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> RentvineCredentialsStatus:
    """Masked metadata for the saved credentials, or ``configured: false``.

    Works whether or not the connector is enabled: somebody who connected
    before a deployment turned the flag off still needs to see what is stored.
    """
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="view")
    result = _row_to_status(await _fetch_row(pool, company_id))
    settings = get_settings()
    result.connector_enabled = bool(settings.rentvine.enabled)
    result.webhook_scheme_ready = scheme_from_settings(settings.rentvine) is not None
    return result


_ASK_FOR_SUBDOMAIN = (
    "Occupella could not work out your Rentvine address from those keys. "
    "Open Rentvine and look at your browser's address bar: the word before "
    "'.rentvine.com' is your account code. Enter it and try again."
)


async def _resolve_account_code(
    pool: asyncpg.Pool,
    payload: RentvineCredentialsInput,
    user: AuthenticatedUser,
    company_id: str,
) -> str:
    """The subdomain to store — supplied by the customer, or discovered.

    ⚠ **NOTHING IS STORED THAT WAS NOT VERIFIED.** A discovered code is only
    returned when the credential authenticated against it, so the failure mode
    is an honest refusal naming one field, never a plausible wrong host quietly
    written to the row the worker syncs from (gotchas 55/57: a guard that
    cannot see declines to assert).
    """
    if payload.account_code:
        try:
            return transport.normalize_account_code(payload.account_code)
        except transport.RentvineTransportError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from None

    name = await pool.fetchval("SELECT name FROM public.companies WHERE id = $1", company_id)
    candidates = discovery.candidate_codes(company_name=str(name or ""), email=user.email or "")
    result = await discovery.discover_account_code(
        api_key=payload.api_key,
        api_secret=payload.api_secret,
        candidates=candidates,
    )
    if result.found:
        logger.info(
            "rentvine discovery: company %s resolved to %r after %d candidates",
            company_id,
            result.found,
            len(result.tried),
        )
        return result.found

    if result.ambiguous:
        # ⚠ Under ADR 0010's model this cannot happen — one credential, one
        # tenant — so it means the model is wrong (a wildcard host routing on
        # the credential alone is the likely shape). Storing either would hide
        # that; saying so is what makes it findable.
        logger.warning(
            "rentvine discovery: company %s matched %d subdomains %r — refusing to pick",
            company_id,
            len(result.ambiguous),
            result.ambiguous,
        )
    raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=_ASK_FOR_SUBDOMAIN)


@router.put("/credentials")
async def upsert_credentials(
    payload: RentvineCredentialsInput,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> RentvineCredentialsStatus:
    """Encrypt and store the company's Rentvine credentials."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="change")
    _require_connector_enabled()

    cipher = CredentialCipher(get_settings())
    # ⚠ AFTER `_require_connector_enabled()`, and that order is the whole
    # guarantee: discovery sends this credential to Rentvine, and a deployment
    # that has the connector switched off must never do that on a customer's
    # behalf. A behavioural test cannot see the difference — both orders 503 —
    # so `test_rentvine_account_discovery` walks the statements.
    account_code = await _resolve_account_code(pool, payload, user, company_id)
    # Derived through the ONE template (gotcha 25) rather than formatted here,
    # and it refuses anything that is not a bare subdomain label.
    base_url = transport.base_url(account_code)

    try:
        await pool.execute(
            """
            INSERT INTO public.company_rentvine_credentials (
                company_id, account_code, encrypted_api_key, api_key_last4,
                encrypted_api_secret, base_url, last_tested_at
            ) VALUES ($1, $2, $3, $4, $5, $6, NULL)
            ON CONFLICT (company_id) DO UPDATE SET
                account_code         = EXCLUDED.account_code,
                encrypted_api_key    = EXCLUDED.encrypted_api_key,
                api_key_last4        = EXCLUDED.api_key_last4,
                encrypted_api_secret = EXCLUDED.encrypted_api_secret,
                base_url             = EXCLUDED.base_url,
                last_tested_at       = NULL
            """,
            company_id,
            account_code,
            cipher.encrypt(payload.api_key),
            cipher.last4(payload.api_key),
            cipher.encrypt(payload.api_secret),
            base_url,
        )
    except asyncpg.exceptions.UniqueViolationError as exc:
        # ⚠ THE ONLY WAY THIS FIRES IS THE EXCLUSIVITY TRIGGER, and turning it
        # into an honest refusal is the whole point of catching it.
        # `assert_one_pms_per_company` (0100, made four-way by 0107) raises with
        # `errcode = 'unique_violation'` when the company already holds another
        # system's credentials (ADR 0010 a1 — every PMS writes the same
        # `buildium_id` column, so holding two silently overwrites mirror rows).
        # The table's own primary key cannot produce this: the statement above
        # resolves a company_id collision with DO UPDATE.
        #
        # ⚠ And the trigger does NOT fire on that UPDATE path, which is what
        # makes re-saving credentials for an existing Rentvine company work:
        # it is declared `before insert or update OF company_id`, and
        # company_id is not in the SET list.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            # Names the system the trigger named; the text is unchanged when
            # that is Buildium.
            detail=one_pms_refusal(RENTVINE, exc),
        ) from None

    return _row_to_status(await _fetch_row(pool, company_id))


@router.delete("/credentials")
async def delete_credentials(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> RentvineCredentialsStatus:
    """Tombstone the credentials row AND purge the Rentvine-derived mirror.

    ADR 0010 e1 applies Buildium's §18 discipline here without having read
    Rentvine's terms, on the ground that deleting more is the safe side of an
    unanswered contractual question. Reconnecting rebuilds the mirror from a
    fresh sync, so nothing is lost that Rentvine itself does not still hold.

    ⚠ This does NOT revoke the key inside Rentvine (ADR 0010 e2). No surface
    may describe it as a revocation.

    ⚠ It works while the connector is dark, deliberately. A customer who
    connected before the flag was turned off must still be able to disconnect —
    and a company that cannot delete this row cannot connect Buildium either,
    because the exclusivity trigger refuses while it exists.
    """
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="disconnect")
    await pool.execute(
        "DELETE FROM public.company_rentvine_credentials WHERE company_id = $1",
        company_id,
    )
    # Purge AFTER the credentials are gone, same ordering as Buildium's. A sync
    # that starts after the delete finds no row and refuses. A sync already
    # running loaded the login before the delete, so it reads the saved login
    # again before each stage and after its last write
    # (`rentvine.sync._check_login`): finding it gone, it starts no further
    # stage, purges again and stops. The stage in flight still finishes, and
    # that second purge removes what it wrote after this one. Only a read of
    # the login that fails (a database error) leaves what was already written.
    # The purge is idempotent, so a raise here is a retryable 500 with the
    # credentials already tombstoned.
    deleted = await purge_rentvine_mirror(pool, company_id)
    return RentvineCredentialsStatus(configured=False, purged_rows=sum(deleted.values()))


#: Bounds on a pasted signing key. Rentvine does not document its format, so
#: these only catch a paste gone wrong: an empty or cut-off copy, or a page of text.
_WEBHOOK_SECRET_MIN = 8
_WEBHOOK_SECRET_MAX = 512

_WEBHOOK_SECRET_MALFORMED = (
    "That doesn't look like a whole signing key. Copy it again from Rentvine "
    "(Actions → Webhooks) and paste only the key."
)


@router.put("/webhook-secret")
async def set_webhook_secret(
    payload: RentvineWebhookSecretInput,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> RentvineCredentialsStatus:
    """Encrypt and store the account's webhook signing key. Saving again replaces it.

    The receiver (``webhooks/rentvine_router.py``) reads this column through
    ``decrypt_webhook_secret``. Until this route, nothing wrote it, so every
    delivery would have been refused even once the signature scheme is known.
    """
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="change")
    _require_connector_enabled()

    # Stripped because the capture tool strips its copy before deriving the
    # scheme (`--analyse`), and the receiver must sign with the same bytes.
    secret = payload.secret.get_secret_value().strip()
    if not _WEBHOOK_SECRET_MIN <= len(secret) <= _WEBHOOK_SECRET_MAX or any(
        c.isspace() for c in secret
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=_WEBHOOK_SECRET_MALFORMED
        )

    saved = await pool.fetchval(
        """
        UPDATE public.company_rentvine_credentials
        SET encrypted_webhook_secret = $2
        WHERE company_id = $1
        RETURNING company_id
        """,
        company_id,
        CredentialCipher(get_settings()).encrypt(secret),
    )
    if saved is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Connect Rentvine first: save your access key and secret, then "
                "paste the signing key."
            ),
        )
    logger.info("rentvine webhook signing key saved for company=%s", company_id)
    # Re-read rather than echo: the answer is what the row now holds (rule 6).
    return _row_to_status(await _fetch_row(pool, company_id))


@router.delete("/webhook-secret")
async def delete_webhook_secret(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> RentvineCredentialsStatus:
    """Forget the signing key; the API keys stay. Every delivery is refused after
    this, which costs freshness on four object types and never data (the poll).

    Works while the connector is dark, like the disconnect.
    """
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="change")
    await pool.execute(
        """
        UPDATE public.company_rentvine_credentials
        SET encrypted_webhook_secret = NULL
        WHERE company_id = $1
        """,
        company_id,
    )
    logger.info("rentvine webhook signing key removed for company=%s", company_id)
    return _row_to_status(await _fetch_row(pool, company_id))


@router.post("/test")
async def test_connection(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> RentvineTestResult:
    """Read one page of one property with the saved credentials.

    On success stamps ``last_tested_at`` so the connector card can show a
    recent-tested state.

    ⚠ Unlike Buildium's ``/test`` there is no account-id backfill sidecar here,
    and nothing needs one: Rentvine deliveries are routed by the account
    subdomain in the webhook URL, which the customer typed, so there is no
    field for a probe to discover (``fetch_rentvine_row_by_account_code``).
    """
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="test")
    _require_connector_enabled()

    row = await _fetch_row(pool, company_id)
    if row is None:
        return RentvineTestResult(ok=False, error="No saved credentials.")

    rsettings = decrypt_rentvine_settings_from(row, get_settings())
    try:
        async with RentvineClient(rsettings) as client:
            page = await client.get_page(_TEST_PATH, page=1, page_size=1)
    except RentvineAPIError as exc:
        logger.warning("rentvine /test failed for company=%s: %s", company_id, exc)
        advice = _TEST_ADVICE.get(exc.failure, _TEST_ADVICE_UNDOCUMENTED)
        return RentvineTestResult(ok=False, error=advice)
    except Exception:  # noqa: BLE001 — the test must answer, never 500
        # ⚠ The exception text stays in the log. It used to go to the customer
        # verbatim ("Could not reach Rentvine: <whatever the bug said>"): an
        # internal detail, and not something they can act on (rule 16).
        logger.warning("rentvine /test crashed for company=%s", company_id, exc_info=True)
        return RentvineTestResult(ok=False, error=_TEST_CRASHED)

    await pool.execute(
        """
        UPDATE public.company_rentvine_credentials
        SET last_tested_at = now()
        WHERE company_id = $1
        """,
        company_id,
    )
    # ⚠ `total_items` is the ACCOUNT's property count and `len(page.rows)` is
    # the one row we asked for. Reporting the second as "properties found"
    # would tell every customer they have one property (gotcha 26's class), so
    # an unstated total stays unstated rather than collapsing into a number.
    # The first mirror sync starts now rather than at the worker's next hourly
    # tick, the way Buildium's /test kicks its initial sync. Otherwise the
    # wizard's launch screen scans an empty mirror and times out.
    _kick_initial_sync(pool, company_id)
    return RentvineTestResult(ok=True, properties_count=page.total_items)


#: asyncio keeps only a weak reference to a task, so a fire-and-forget task
#: with no other reference can be garbage-collected mid-crawl.
_background: set[asyncio.Task[bool]] = set()


class RentvineSyncResult(BaseModel):
    """What a Rentvine sync did, or is doing, in terms the page can say.

    ``POST /rentvine/sync`` STARTS one and answers at once; ``GET`` reports it.
    Measured 2026-10-03 on Render: one sync of a 13-property sandbox took
    86,978 ms, and the web app stops waiting for a request at 45 s
    (``api.ts`` ``DEFAULT_TIMEOUT_MS``). Run inside the request, every sync that
    worked was reported as "timed out … try again" (gotcha 199).
    """

    ok: bool
    #: Rows written per record type ("units": 37), as the mirror stamps them.
    rows: dict[str, int] = Field(default_factory=dict)
    #: Record types whose fetch failed: what's on screen for them is from the
    #: last good sync, NOT empty (gotcha 106). The reasons stay in the log.
    stale: list[str] = Field(default_factory=list)
    error: str | None = None
    #: True while the sync this answer describes is still running.
    running: bool = False
    #: When that sync began (ISO, server clock). The page waits for the run
    #: with THIS start, so it never reports an older run's counts as its own.
    started_at: str | None = None


_SYNC_DID_NOT_FINISH = (
    "The Rentvine sync didn't finish. Nothing you entered needs to change; "
    "try again in a few minutes, and if it keeps failing, " + contact_support() + "."
)
_SYNC_NOT_KNOWN = (
    "Occupella has no record of a Rentvine sync since it last restarted. Click Sync now to run one."
)


def _ended(run: SyncRun) -> RentvineSyncResult:
    """How a finished run ended, in the page's terms."""
    started_at = run.started_at.isoformat()
    result = run.result
    if result is None:
        return RentvineSyncResult(ok=False, error=_SYNC_DID_NOT_FINISH, started_at=started_at)
    stale = sorted(SYNC_STATE_STAGE.get(t, t) for t in result.failed)
    return RentvineSyncResult(
        ok=not stale,
        rows={SYNC_STATE_STAGE.get(t, t): n for t, n in result.rows_by_table.items()},
        stale=stale,
        error=(
            None
            if not stale
            else "Some of Rentvine didn't sync this time: "
            + ", ".join(stale).replace("_", " ")
            + ". What's on screen for those is from the last sync that worked."
        ),
        started_at=started_at,
    )


@router.post("/sync")
async def sync_now_route(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> RentvineSyncResult:
    """Start re-reading Rentvine into the mirror: Settings → Connectors' Sync
    now and the Operations page's Re-sync.

    Answers at once with the run's start time; ``GET /rentvine/sync`` reports
    the run until it ends. A click while a sync is already going (the one a
    passed test starts, or a second click) waits on THAT run, rather than
    starting a second crawl or being told to come back later.

    Buildium's button is ``POST /buildium/backfill``. A Rentvine workspace
    pressing that got "No saved credentials." for a connection that works.
    Admin-only, like Buildium's, because a sync spends the account's API quota.
    """
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="sync")
    _require_connector_enabled()
    if await fetch_rentvine_row(company_id) is None:
        return RentvineSyncResult(
            ok=False, error="No Rentvine connection is saved for this workspace."
        )
    run, started = claim_sync(company_id)
    if started and not _spawn(run_claimed_sync(pool, company_id, run), company_id):
        # The slot was taken before the task existed. Left held, every later
        # click would wait on a run that never ends.
        abandon_claim(company_id, run)
        return _ended(run)
    return RentvineSyncResult(ok=True, running=True, started_at=run.started_at.isoformat())


@router.get("/sync")
async def sync_status_route(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> RentvineSyncResult:
    """The Rentvine sync running in this process now, or how the last one
    ended. What the page polls after ``POST /rentvine/sync``."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="sync")
    run = sync_status(company_id)
    if run is None:
        return RentvineSyncResult(ok=False, error=_SYNC_NOT_KNOWN)
    if run.finished_at is None:
        return RentvineSyncResult(ok=True, running=True, started_at=run.started_at.isoformat())
    return _ended(run)


def _spawn(coro: Coroutine[Any, Any, Any], company_id: str) -> bool:
    """Run a sync in the background, holding a reference until it ends."""
    try:
        task = asyncio.create_task(coro)
    except Exception:  # noqa: BLE001 — the caller says what happened
        coro.close()
        logger.warning("could not start a Rentvine sync for company=%s", company_id)
        return False
    _background.add(task)
    task.add_done_callback(_background.discard)
    return True


def _kick_initial_sync(pool: asyncpg.Pool, company_id: str) -> None:
    """Start the first mirror sync without blocking the /test response."""
    try:
        task = asyncio.create_task(start_initial_sync(pool, company_id))
    except Exception:  # noqa: BLE001 — never fail the user-visible test on this
        logger.warning("could not start initial Rentvine sync for company=%s", company_id)
        return
    _background.add(task)
    task.add_done_callback(_background.discard)


__all__ = ["router"]
