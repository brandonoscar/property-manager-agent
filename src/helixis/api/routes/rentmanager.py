"""Rent Manager connection: save, read, test, sync and disconnect.

Two screens call these routes: Settings → Connectors
(``RentManagerApiDetail.tsx``, listed by ``GET /connectors`` while the switch
is on or keys are stored) and the onboarding wizard's Rent Manager option
(helixis-onboarding). The field names are that wizard's contract (ADR 0003):
``test_rentmanager_routes`` pins them, and the web app's service test reads
the same names.

The same product surface as Rentvine's routes (``api/routes/rentvine.py``),
copied in behaviour, not code:

  - every route needs an admin, reads included: the credentials are ONE row
    the whole team shares, and the disconnect also deletes the mirror;
  - saving, testing and syncing refuse while the connector is switched off;
    the read and the disconnect do not (gotcha 120): keys saved before the
    switch went off must stay visible and removable, and the one-PMS trigger
    refuses every other system while the row exists;
  - a company on another system gets a readable 409 (``one_pms_refusal``);
  - disconnecting is a purge, never a revocation (``rentmanager/purge.py``).

Where it departs from Rentvine, each for a reason written where it happens:
the save reads its own body (FastAPI's 422 echoes the whole body, password
included); the test confirms the location (design R2) and counts properties
from one page's total header; ``last_tested_at`` is stamped only on the
credentials that were tested; and saving or disconnecting drops this
process's session token for the old credentials.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator

from helixis import settings as app_settings
from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.rate_limit import connectors_limiter
from helixis.common.support import contact_support
from helixis.db.company_pms import one_pms_refusal
from helixis.db.engine import get_pool
from helixis.pms import auth as pms_auth
from helixis.pms import registry, sync_cycle
from helixis.pms.auth import header_value, host_label
from helixis.pms.errors import Failure, PmsApiError
from helixis.rentmanager import client as rm_client
from helixis.rentmanager.credentials import (
    TABLE,
    CredentialsUnusableError,
    decrypt_row,
    fetch_row,
    version_of,
)
from helixis.rentmanager.purge import purge_rentmanager_mirror
from helixis.rentmanager.resources import RESOURCES
from helixis.rentmanager.sync import (
    DERIVED_TABLES,
    INCREMENTAL_OFF,
    SYNC_STATE_STAGE,
    ask_for_full_pass,
)
from helixis.security import CredentialCipher

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rentmanager", tags=["rentmanager"])

PMS = registry.RENTMANAGER
NAME = registry.PMS[PMS].name

#: What ``/test`` counts: the collection the sync reads first, read the way
#: the sync reads it (gotcha 85: test the surface the product uses).
_TEST_RESOURCE = "properties"

#: ``location_id`` is a Postgres ``integer`` (migration 0107). A larger number
#: would fail the insert with a driver error instead of a sentence.
_MAX_LOCATION_ID = 2_147_483_647

#: Bounds on a paste gone wrong. Rent Manager documents no lengths; these are
#: Occupella's, generous, and only stop a page of text being stored.
_MAX_USERNAME = 256
_MAX_PASSWORD = 1024
_MAX_PARTNER_TOKEN = 4096


class RentManagerCredentialsInput(BaseModel):
    """What the connect form sends. The names are the wizard's contract.

    ⚠ Secrets are ``SecretStr``, so a repr or a log line shows ``**********``.
    The route validates this model itself (``_parse``) and answers each
    problem with a sentence naming the field, never its value.
    """

    #: The subdomain in ``https://{corp_id}.api.rentmanager.com``. Not a secret.
    corp_id: str
    #: The one location this workspace reads (design R2). Not a secret.
    location_id: int
    username: SecretStr
    password: SecretStr
    #: Only if Rent Manager issued one (design R1). Blank means none.
    partner_token: SecretStr | None = None

    @field_validator("location_id", mode="before")
    @classmethod
    def _whole_positive_number(cls, value: Any) -> Any:
        # A JSON true is not location 1, and "1" or 1.0 is not the integer the
        # contract names. The message is never shown: ``_parse`` words it.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("not a whole number")
        if not 0 < value <= _MAX_LOCATION_ID:
            raise ValueError("out of range")
        return value


class RentManagerCredentialsStatus(BaseModel):
    """The masked read, and the disconnect's answer. Never a secret.

    ⚠ No last4 of the password or the partner token: four characters of a
    secret are exposure with no use (migration 0107). The username's last four
    help an admin recognise which Rent Manager user is saved.
    """

    configured: bool
    corp_id: str | None = None
    location_id: int | None = None
    username_last4: str | None = None
    #: Whether a partner token is stored. Never the token.
    partner_token_saved: bool = False
    #: Set only by a passed test; cleared whenever the saved details change.
    last_tested_at: str | None = None
    #: Set only on the disconnect's answer.
    purged_rows: int | None = None
    #: Set only on the GET: whether this deployment accepts Rent Manager
    #: details at all. The wizard and the panel read it BEFORE asking for them.
    connector_enabled: bool | None = None


class RentManagerTestResult(BaseModel):
    ok: bool
    #: Rent Manager's own count of the location's properties, or ``None`` when
    #: it did not state one. Never 0 for a count that was not stated.
    properties_count: int | None = None
    error: str | None = None


class RentManagerSyncResult(BaseModel):
    """What a sync did, or is doing (Rentvine's ``RentvineSyncResult`` names).

    ``POST /sync`` starts one and answers at once; ``GET /sync`` reports it. A
    sync reads at one request a second (design R6), far longer than the web
    app waits for one request.
    """

    ok: bool
    #: Rows written per stage ("units": 37). A stage that failed is not here.
    rows: dict[str, int] = Field(default_factory=dict)
    #: Stages whose read or write failed: what's on screen for them is from
    #: the last sync that worked, NOT empty. The reasons stay in the log.
    stale: list[str] = Field(default_factory=list)
    error: str | None = None
    #: True while the sync this answer describes is still running.
    running: bool = False
    #: When that sync began (ISO, server clock). The page waits for THIS run.
    started_at: str | None = None


# ── Refusals, in the customer's words ────────────────────────────────────


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
    pool: Any, user: AuthenticatedUser, company_id: str, *, action: str
) -> None:
    """Admin gate on EVERY route here, reads included. Fails closed: an
    unverifiable role is refused (``require_role`` reads ``None`` as deny).
    Imported locally to avoid a module-level cycle with ``routes.company``."""
    from helixis.api.roles import ADMIN
    from helixis.api.routes.company import require_role

    await require_role(
        pool,
        user.user_id,
        company_id,
        ADMIN,
        detail=(
            f"Only an admin can {action} the {NAME} connection. Ask an admin of this "
            "workspace to do it."
        ),
    )


def _require_connector_enabled() -> None:
    """Refuse to save, test or sync while the connector is switched off.

    ⚠ Saved details on a switched-off deployment sync nothing, and they cost
    the customer something: the one-PMS trigger refuses every other system
    while the row exists. An honest refusal leaves them somewhere to act from.
    """
    if not registry.enabled(PMS):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"The {NAME} connector is not enabled on this deployment yet, so nothing "
                "can be saved, tested or synced. To have it turned on, " + contact_support() + "."
            ),
        )


#: What to say about a field that did not pass, by field. Never the value.
_FIELD_ADVICE: dict[str, str] = {
    "corp_id": (
        "Enter your Rent Manager corp ID: the part before .api.rentmanager.com in "
        "your Rent Manager API address, in letters, digits and hyphens only."
    ),
    "location_id": "Enter the Rent Manager location ID as a whole number above 0, such as 1.",
    "username": "Enter the username of the Rent Manager user Occupella signs in as.",
    "password": "Enter that Rent Manager user's password.",
    "partner_token": (
        "Paste the partner token as text, or leave it empty if Rent Manager did not issue one."
    ),
}

_BODY_UNREADABLE = (
    "Occupella couldn't read that request. Enter the corp ID, location ID, username "
    "and password, and save again."
)


def _refuse_field(field: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail=_FIELD_ADVICE.get(field, _BODY_UNREADABLE),
    )


async def _parse(request: Request) -> RentManagerCredentialsInput:
    """The connect form, validated with a sentence per problem.

    ⚠ The body is read here, not declared as the route's parameter, because
    FastAPI's own 422 echoes the WHOLE request body back for any field that
    fails, password included (measured: a missing ``username`` answered
    ``"input": {..., "password": "..."}``). A ``ValidationError`` carries the
    input too, so it is never logged, shown or chained (``from None``).
    """
    try:
        data = json.loads(await request.body() or b"null")
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail=_BODY_UNREADABLE)
    try:
        return RentManagerCredentialsInput.model_validate(data)
    except ValidationError as exc:
        locs = [err.get("loc") or () for err in exc.errors(include_input=False)]
        fields = [str(loc[0]) for loc in locs if loc]
        first = next((f for f in _FIELD_ADVICE if f in fields), "")
    raise _refuse_field(first)


class _Prepared(BaseModel):
    """The columns one save writes. Ciphertext and masked values only."""

    corp_id: str
    location_id: int
    encrypted_username: str
    username_last4: str
    # Ciphertext, but still never printed (tests/unit/test_no_secrets_in_repr.py).
    encrypted_password: str = Field(repr=False)
    encrypted_partner_token: str | None = Field(repr=False)
    base_url: str


def _username_tail(username: str) -> str:
    """The username's last four characters, or ``""`` when that would be the
    whole of it. The username is encrypted at rest (design "Credentials"), so
    the masked read never shows all of one."""
    return username[-4:] if len(username) > 4 else ""


def _prepare(payload: RentManagerCredentialsInput, cipher: CredentialCipher) -> _Prepared:
    """Check what the guards check, then encrypt. Raises a 422 naming the
    field, never the value. Plaintext lives only inside this call."""
    try:
        corp_id = host_label(payload.corp_id, what="the Rent Manager corp id")
    except ValueError:
        raise _refuse_field("corp_id") from None
    username = payload.username.get_secret_value().strip()
    if not username or len(username) > _MAX_USERNAME:
        raise _refuse_field("username")
    # Not stripped: a space can be part of a password.
    if not payload.password.get_secret_value().strip():
        raise _refuse_field("password")
    if len(payload.password.get_secret_value()) > _MAX_PASSWORD:
        raise _refuse_field("password")
    partner: str | None = None
    if payload.partner_token is not None and payload.partner_token.get_secret_value().strip():
        # It travels as a header (client.request_headers): no control characters.
        try:
            partner = header_value(
                payload.partner_token.get_secret_value(), what="the partner token"
            )
        except ValueError:
            raise _refuse_field("partner_token") from None
        if len(partner) > _MAX_PARTNER_TOKEN:
            raise _refuse_field("partner_token")
    return _Prepared(
        corp_id=corp_id,
        location_id=payload.location_id,
        encrypted_username=cipher.encrypt(username),
        username_last4=_username_tail(username),
        encrypted_password=cipher.encrypt(payload.password.get_secret_value()),
        # Blank is none: never an empty stored value.
        encrypted_partner_token=cipher.encrypt(partner) if partner else None,
        # From the guarded corp id through the client's one template, never
        # from the request. The loader never reads it back.
        base_url=rm_client.base_url(corp_id),
    )


# ── The status read ──────────────────────────────────────────────────────


async def _fetch_status_row(pool: Any, company_id: str) -> dict[str, Any] | None:
    """What the masked read shows, and nothing it does not: no ciphertext
    leaves the database for it."""
    row = await pool.fetchrow(
        f"""
        SELECT corp_id, location_id, username_last4, last_tested_at,
               coalesce(encrypted_partner_token, '') <> '' AS partner_token_saved
        FROM public.{TABLE}
        WHERE company_id = $1::uuid
        """,  # noqa: S608 — module constant
        company_id,
    )
    return dict(row) if row else None


def _row_to_status(row: dict[str, Any] | None) -> RentManagerCredentialsStatus:
    if row is None:
        return RentManagerCredentialsStatus(configured=False)
    tested = row["last_tested_at"]
    return RentManagerCredentialsStatus(
        configured=True,
        corp_id=row["corp_id"],
        location_id=row["location_id"],
        username_last4=row["username_last4"] or None,
        partner_token_saved=bool(row["partner_token_saved"]),
        last_tested_at=tested.isoformat() if tested else None,
    )


def _forget_token(company_id: str, row: dict[str, Any] | None) -> None:
    """Drop this process's session token for a saved credential set that is
    being replaced or removed. The token counts as a secret (terms rule 8).
    Other processes stop using it on their own: the key carries the version."""
    if row is not None:
        pms_auth.forget(rm_client.token_cache_key(company_id, row["location_id"], version_of(row)))


# ── Routes ───────────────────────────────────────────────────────────────


@router.get("/credentials")
async def get_credentials(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> RentManagerCredentialsStatus:
    """The masked saved details, or ``configured: false``. Answers whether or
    not the connector is switched on: details saved before it went off must
    stay visible."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="view")
    result = _row_to_status(await _fetch_status_row(pool, company_id))
    result.connector_enabled = registry.enabled(PMS)
    return result


@router.put(
    "/credentials",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": RentManagerCredentialsInput.model_json_schema()}
            },
        }
    },
)
async def upsert_credentials(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> RentManagerCredentialsStatus:
    """Encrypt and store the company's Rent Manager details. Saving again
    replaces all of them and clears ``last_tested_at``: new details have not
    passed a test. It clears ``capabilities`` too: a write refused under the old
    user may be allowed now (``pms.refusals``; the refusal says to save)."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="change")
    _require_connector_enabled()
    payload = await _parse(request)
    prepared = _prepare(payload, CredentialCipher(app_settings.get_settings()))

    previous = await fetch_row(pool, company_id)
    try:
        await pool.execute(
            f"""
            INSERT INTO public.{TABLE} (
                company_id, corp_id, location_id, encrypted_username, username_last4,
                encrypted_password, encrypted_partner_token, base_url, last_tested_at
            ) VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, NULL)
            ON CONFLICT (company_id) DO UPDATE SET
                corp_id                 = EXCLUDED.corp_id,
                location_id             = EXCLUDED.location_id,
                encrypted_username      = EXCLUDED.encrypted_username,
                username_last4          = EXCLUDED.username_last4,
                encrypted_password      = EXCLUDED.encrypted_password,
                encrypted_partner_token = EXCLUDED.encrypted_partner_token,
                base_url                = EXCLUDED.base_url,
                last_tested_at          = NULL,
                capabilities            = '{{}}'::jsonb
            """,  # noqa: S608 — module constant
            company_id,
            prepared.corp_id,
            prepared.location_id,
            prepared.encrypted_username,
            prepared.username_last4,
            prepared.encrypted_password,
            prepared.encrypted_partner_token,
            prepared.base_url,
        )
    except asyncpg.exceptions.UniqueViolationError as exc:
        # ⚠ Only the one-PMS trigger raises this here (migration 0107): the
        # table's own key is resolved by DO UPDATE, and the trigger fires on
        # INSERT and on an update OF company_id, which the SET list never
        # names. So re-saving for a Rent Manager company is an ordinary update.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=one_pms_refusal(PMS, exc)
        ) from None
    _forget_token(company_id, previous)
    logger.info("rentmanager credentials saved for company=%s", company_id)
    # Re-read rather than echo: the answer is what the row now holds (rule 6).
    return _row_to_status(await _fetch_status_row(pool, company_id))


@router.delete("/credentials")
async def delete_credentials(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> RentManagerCredentialsStatus:
    """Delete the saved details AND everything synced from Rent Manager.

    ⚠ Nothing changes inside Rent Manager: the user Occupella signed in as
    keeps working there. No surface may describe this as revoking access.

    ⚠ Works while the connector is switched off, deliberately: a company that
    cannot delete this row cannot connect any other system either.
    """
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="disconnect")
    previous = await fetch_row(pool, company_id)
    await pool.execute(f"DELETE FROM public.{TABLE} WHERE company_id = $1::uuid", company_id)  # noqa: S608
    _forget_token(company_id, previous)
    # Purge AFTER the row is gone: a pass that starts now finds no details,
    # and one already reading checks again before and after it writes
    # (``sync.run_pass``). Idempotent, so a failure here is a retryable 500.
    deleted = await purge_rentmanager_mirror(pool, company_id)
    logger.info("rentmanager disconnected for company=%s", company_id)
    return RentManagerCredentialsStatus(configured=False, purged_rows=sum(deleted.values()))


#: What to tell the customer for each failure the test can meet.
#:
#: ⚠ Keyed on ``Failure``: a 401 and a 403 need OPPOSITE instructions (gotcha
#: 102). Re-saving fixes a refused password; it cannot fix a role, and telling
#: somebody whose details Rent Manager accepted to reconnect sends them to
#: redo something that works.
_TEST_ADVICE: dict[Failure, str] = {
    Failure.CREDENTIALS: (
        f"{NAME} refused the username or password. Check them by signing in to "
        f"{NAME} as that user, then save them here again."
    ),
    Failure.PERMISSION: (
        f"{NAME} accepted this user and refused it what the test asked for: reading "
        f"your properties through the API. In {NAME}, check that API access is turned "
        "on for your company and that this user's role may read properties. The "
        "username and password are fine and do not need changing."
    ),
    Failure.NOT_FOUND: (
        f"{NAME} doesn't recognise that address or location. Check the corp ID (the "
        "part before .api.rentmanager.com) and the location ID, then save them again."
    ),
    Failure.BAD_REQUEST: (
        f"{NAME} rejected the request Occupella sent as malformed. Nothing you entered "
        "is known to be wrong: " + contact_support("and we will look into it.")
    ),
    Failure.CONFLICT: (
        f"{NAME} answered that something changed while Occupella was reading. Run the test again."
    ),
    Failure.NEEDS_OVERRIDE: (
        f"{NAME} asked for an override, which Occupella never sends. Nothing you entered "
        "needs to change: " + contact_support("and we will look into it.")
    ),
    # Occupella's own hold or circuit breaker: nothing reached Rent Manager.
    Failure.UNAVAILABLE: (
        f"{NAME} isn't answering right now, so Occupella has paused calls to it for a "
        "few minutes. Nothing you entered needs to change: test again in a few minutes."
    ),
}

#: Anything else, 429 and 5xx included. Retryable ones were already retried by
#: the time this is reached, so it says what to do next, not "try again now".
_TEST_ADVICE_UNDOCUMENTED = (
    f"{NAME} answered with an error Occupella does not recognise. Nothing is known "
    "to be wrong with what you entered: test again later, and if it keeps failing, "
    + contact_support()
    + "."
)

#: A failure inside Occupella, not an answer from Rent Manager.
_TEST_CRASHED = (
    f"Something went wrong on Occupella's side while checking your {NAME} connection. "
    "Nothing you entered needs to change. Try the test once more, and if it fails "
    "again, " + contact_support() + "."
)

_NOTHING_SAVED = (
    f"No {NAME} connection is saved for this workspace. Save your corp ID, location "
    "ID, username and password first."
)

_UNREADABLE = f"Occupella can't read the saved {NAME} details. Save them again, then run the test."

_CHANGED_DURING_TEST = (
    f"The saved {NAME} details changed while the test ran, so this result is about "
    "the old ones. Run the test again."
)


def _unreachable(corp_id: str) -> str:
    return (
        f"Occupella couldn't reach {NAME} at {corp_id}.api.rentmanager.com. Check the "
        "corp ID: it is the part before .api.rentmanager.com in your Rent Manager API "
        "address. If it is right, test again in a few minutes."
    )


def _location_unconfirmed(stored: int) -> str:
    return (
        f"{NAME} accepted the sign-in but didn't say which location it opened, so "
        f"Occupella can't confirm it reads location {stored}, and nothing will sync "
        "until it can. This is likely on Occupella's side: "
        + contact_support("and we will look into it.")
    )


def _location_mismatch(current: int, stored: int) -> str:
    return (
        f"{NAME} signed this user in to location {current}, not location {stored}, "
        f"which is the one saved here. If location {current} is the one this workspace "
        f"should read, change the location ID to {current} and save again. Otherwise, "
        f"give this user access to location {stored} in {NAME}."
    )


@router.post("/test")
async def test_connection(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> RentManagerTestResult:
    """Sign in with the saved details, confirm the location, and count the
    properties there. On success, stamp ``last_tested_at`` and start the
    first sync, so the mirror does not wait for the worker's next tick."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="test")
    _require_connector_enabled()

    row = await fetch_row(pool, company_id)
    if row is None:
        return RentManagerTestResult(ok=False, error=_NOTHING_SAVED)
    try:
        creds = decrypt_row(row, app_settings.get_settings(), company_id=company_id)
    except CredentialsUnusableError as exc:
        # The message names the problem, never a value.
        logger.warning("rentmanager /test for company=%s: %s", company_id, exc)
        return RentManagerTestResult(ok=False, error=_UNREADABLE)

    try:
        async with rm_client.build_client(creds) as client:
            current = await rm_client.current_location_id(client)
            if current is None:
                return RentManagerTestResult(
                    ok=False, error=_location_unconfirmed(creds.location_id)
                )
            if current != creds.location_id:
                return RentManagerTestResult(
                    ok=False, error=_location_mismatch(current, creds.location_id)
                )
            # ⚠ ONE page of one row, and Rent Manager's total header. The
            # rows on that page are the page size, not the portfolio, so a
            # count not stated stays unstated (gotcha 26's class).
            count = await rm_client.count_of(client, RESOURCES[_TEST_RESOURCE])
    except PmsApiError as exc:
        # The shared client masks every credential in its errors.
        logger.warning("rentmanager /test failed for company=%s: %s", company_id, exc)
        if exc.status == 0 and exc.failure is Failure.UNDOCUMENTED:
            return RentManagerTestResult(ok=False, error=_unreachable(creds.corp_id))
        advice = _TEST_ADVICE.get(exc.failure, _TEST_ADVICE_UNDOCUMENTED)
        return RentManagerTestResult(ok=False, error=advice)
    except Exception:  # noqa: BLE001 — the test must answer, never 500
        logger.warning("rentmanager /test crashed for company=%s", company_id, exc_info=True)
        return RentManagerTestResult(ok=False, error=_TEST_CRASHED)

    # Stamped only if the saved details are still the ones tested: a save that
    # landed while the test ran has not passed anything.
    stamped = await pool.fetchval(
        f"""
        UPDATE public.{TABLE}
        SET last_tested_at = now()
        WHERE company_id = $1::uuid
          AND corp_id = $2
          AND location_id = $3
          AND encrypted_username = $4
          AND encrypted_password = $5
          AND encrypted_partner_token IS NOT DISTINCT FROM $6
        RETURNING company_id
        """,  # noqa: S608 — module constant
        company_id,
        row["corp_id"],
        row["location_id"],
        row["encrypted_username"],
        row["encrypted_password"],
        row["encrypted_partner_token"],
    )
    if stamped is None:
        return RentManagerTestResult(ok=False, error=_CHANGED_DURING_TEST)
    if await _full_pass_asked(pool, company_id):
        sync_cycle.start_now(pool, PMS, company_id)
    return RentManagerTestResult(ok=True, properties_count=count)


async def _full_pass_asked(pool: Any, company_id: str) -> bool:
    """Ask for a full pass before a sync whose counts the page shows: only a
    full pass's counts are totals (``sync.ask_for_full_pass``). ``False`` when
    the ask could not be stored; the caller then starts no sync, rather than
    one whose counts may be only what changed."""
    try:
        await ask_for_full_pass(pool, company_id)
    except Exception:  # noqa: BLE001 — the caller answers; the log has the cause
        logger.warning(
            "rentmanager: could not ask for a full pass for company=%s", company_id, exc_info=True
        )
        return False
    return True


#: Mirror table -> the stage a page names it by. Rentvine's stage names
#: (``SYNC_STATE_STAGE``), plus turnovers, which Occupella opens itself.
_STAGE: dict[str, str] = {
    **SYNC_STATE_STAGE,
    **{table: table.removeprefix("helixis_") for table in DERIVED_TABLES},
}

#: The tables whose row counts a page shows: one per stage, never a link
#: table (its rows are not records anyone counts) and never a derived one
#: (turnovers are opened, not synced).
_COUNTED: dict[str, str] = {
    table: stage for table, stage in SYNC_STATE_STAGE.items() if table != "helixis_lease_tenants"
}

_SYNC_DID_NOT_FINISH = (
    f"The {NAME} sync didn't finish. Run Test connection to check the saved details; "
    "if the test passes and syncing still fails, " + contact_support() + "."
)
_SYNC_NOT_STARTED = (
    f"Occupella couldn't start the {NAME} sync just now. Click Sync now again in a minute; "
    "if it keeps failing, " + contact_support() + "."
)
_SYNC_NOT_KNOWN = (
    f"Occupella has no record of a {NAME} sync since it last restarted. Click Sync now to run one."
)
_SYNC_NOTHING_CAME = (
    f"Nothing came through from {NAME} this time. What's on screen is from the last "
    "sync that worked. Run Test connection to see what Rent Manager says, then Sync "
    "now again."
)


def _ended(run: sync_cycle.SyncRun) -> RentManagerSyncResult:
    """How a finished run ended, in the page's terms. A stage that failed is
    named stale and never given a count, so it never reads as 0 rows. Why a
    pass read everything (``sync.INCREMENTAL_OFF``) is the operator's to
    read in the logs, not a stale table: that pass read every table."""
    started_at = run.started_at.isoformat()
    result = run.result
    if result is None:
        return RentManagerSyncResult(ok=False, error=_SYNC_DID_NOT_FINISH, started_at=started_at)
    stale = sorted(
        {_STAGE.get(table, table) for table in result.failed if table != INCREMENTAL_OFF}
    )
    rows = {
        stage: result.rows_by_table[table]
        for table, stage in _COUNTED.items()
        if table in result.rows_by_table and stage not in stale
    }
    if not stale:
        error = None
    elif not rows:
        error = _SYNC_NOTHING_CAME
    else:
        error = (
            f"Some of {NAME} didn't sync this time: "
            + ", ".join(stale).replace("_", " ")
            + ". What's on screen for those is from the last sync that worked. Click "
            "Sync now again in a few minutes; if they stay out of date, " + contact_support() + "."
        )
    return RentManagerSyncResult(
        ok=not stale, rows=rows, stale=stale, error=error, started_at=started_at
    )


def _answer(run: sync_cycle.SyncRun) -> RentManagerSyncResult:
    if run.finished_at is None:
        return RentManagerSyncResult(ok=True, running=True, started_at=run.started_at.isoformat())
    return _ended(run)


@router.post("/sync")
async def sync_now_route(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> RentManagerSyncResult:
    """Start re-reading Rent Manager into the mirror (Settings → Connectors'
    Sync now). Answers at once; ``GET /sync`` reports the run until it ends.
    A click while a sync is going in this process (the one a passed test
    starts) waits on THAT run instead of starting a second crawl; the
    worker's passes run in the worker's process. It asks for a full pass
    first: the page shows the run's row counts, and only a full pass's are
    totals. Admin-only: a sync spends the account's API quota."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="sync")
    _require_connector_enabled()
    if await fetch_row(pool, company_id) is None:
        return RentManagerSyncResult(ok=False, error=_NOTHING_SAVED)
    if not await _full_pass_asked(pool, company_id):
        return RentManagerSyncResult(ok=False, error=_SYNC_NOT_STARTED)
    # A run that could not even start comes back ended, without a result.
    return _answer(sync_cycle.start_now(pool, PMS, company_id))


@router.get("/sync")
async def sync_status_route(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> RentManagerSyncResult:
    """The Rent Manager sync running in this process, or how the last one
    ended. What the page polls after ``POST /sync``. Not switch-gated: it
    reads only what this process remembers."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    pool = get_pool()
    await _require_admin(pool, user, company_id, action="sync")
    run = sync_cycle.sync_status(PMS, company_id)
    if run is None:
        return RentManagerSyncResult(ok=False, error=_SYNC_NOT_KNOWN)
    return _answer(run)


__all__ = [
    "RentManagerCredentialsInput",
    "RentManagerCredentialsStatus",
    "RentManagerSyncResult",
    "RentManagerTestResult",
    "router",
]
