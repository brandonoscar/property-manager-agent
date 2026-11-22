"""JWT authentication for Helixis API routes.

Verifies Supabase Auth JWTs and resolves the authenticated user
with their company memberships.

Supabase supports two JWT signing schemes:
  - **Modern ECC (ES256)** — asymmetric, signed with the project's
    private key. The verifier fetches the public key set from the
    project's ``/auth/v1/.well-known/jwks.json`` endpoint.
  - **Legacy HS256** — symmetric, signed with the project's JWT secret.
    The shared secret lives in ``settings.auth.jwt_secret``.

**Measured, not assumed** (helixis-test, 2026-11-11): the project issues
**ES256**. Its edge logs record what every request's bearer token carried,
and on 2026-10-07 every ``authenticated`` token was ES256 with a ``kid`` and
``iss`` = ``https://abcdefghijklmnopqrst.supabase.co/auth/v1``; only the
legacy API keys (``anon``, ``service_role``) were HS256, with ``iss``
"supabase", and they are not user tokens. On 2026-10-09, 21 seconds after the
last sign-in, a ``Python-urllib/3.11`` request from Render fetched the JWKS,
which this module does only for an ES256/RS256 token.

So each token is verified by the scheme its header names, and by that one
alone. An ES256/RS256 token whose JWKS check fails is refused; it is never
retried as HS256 (that fallback could only ever fail for a real ES256 token,
and it kept a second way in open for anyone holding the legacy secret). HS256
stays for a token whose header says HS256, only while
``HELIXIS_AUTH__JWT_SECRET`` is set: unsetting it turns HS256 off.

Every token must also name this project's issuer (:func:`expected_issuer`).
With no ``supabase.url`` there is no issuer to compare and no JWKS to fetch,
so every token is refused as "Authentication not configured" (503): an
environment never accepts a token whose issuer nobody checked. Production
always sets the URL; the JWKS fetch above was built from it.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

import jwt
from fastapi import HTTPException, Query, Request, WebSocket, status
from pydantic import BaseModel

from helixis.api.session_liveness import SessionCheckUnavailableError, session_revoked
from helixis.common.support import SUPPORT_EMAIL
from helixis.settings import get_settings

logger = logging.getLogger(__name__)

#: What a caller whose device was signed out is told. Distinct from "Token has
#: expired" so a bug report carries which of the two happened — the frontend
#: deliberately shows neither (`api.ts` never renders a 401 `detail`), so this
#: string is for the log and the report, not the screen.
_REVOKED_DETAIL = "This device was signed out. Sign in again."

#: What a caller is told when their sign-in could not be CHECKED (the session
#: lookup failed, or the project's signing keys could not be fetched). A 503 and
#: never a 401: a 401 tells the app the sign-in is bad, and an outage is not.
_UNCHECKED_DETAIL = "We couldn't check your sign-in just now. Try again in a moment."

#: Seconds a client should wait before trying again after a 503 above.
_RETRY_AFTER = "5"

#: The asymmetric algorithms Supabase signing keys use. The project's keys are
#: ES256 (measured, see the module note); RS256 is Supabase's other choice.
_ASYMMETRIC_ALGS = ("ES256", "RS256")

# Cached PyJWKClient. Built lazily on first use so unit tests that don't
# touch a real Supabase project don't pay the network cost. The client
# itself caches keys in memory and refreshes them on rotation.
_jwks_client: jwt.PyJWKClient | None = None
_jwks_url: str | None = None


def _get_jwks_client(supabase_url: str, anon_key: str = "") -> jwt.PyJWKClient:
    """Build (or return cached) PyJWKClient for the project's JWKS endpoint.

    Supabase exposes the JWKS at ``/auth/v1/.well-known/jwks.json``. Some
    projects accept it anonymously; others require the project's anon key
    in the ``apikey`` header. We always send the anon key when configured
    so we work in both cases.
    """
    global _jwks_client, _jwks_url
    target = f"{supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
    if _jwks_client is None or _jwks_url != target:
        headers = {"apikey": anon_key} if anon_key else None
        _jwks_client = jwt.PyJWKClient(target, cache_keys=True, headers=headers, timeout=10)
        _jwks_url = target
    return _jwks_client


def reset_jwks_client() -> None:
    """Drop the cached PyJWKClient. Tests and config reloads call this so
    the next request rebuilds with the current settings."""
    global _jwks_client, _jwks_url
    _jwks_client = None
    _jwks_url = None


class AuthenticatedUser(BaseModel):
    """Resolved identity from a verified Supabase JWT.

    ⚠ **NO `aal` CLAIM, AND ADDING ONE NAIVELY LOCKS CUSTOMERS OUT.** 2FA today
    is enforced by `TwoFactorGate` in the browser and by nothing here, so a
    crafted request carrying a valid **aal1** token reaches every route — a
    control that says "On" and binds only the person at the screen (gotcha 67:
    a UI gate cannot bind an API).

    The fix is not "require aal2". Supabase ships no recovery codes, so OUR
    backup codes live outside its AAL model: redeeming one is answered by
    `api/routes/mfa.py`, admits the person, and leaves the Supabase session at
    **aal1** with nothing recording that the second factor was satisfied. A
    blanket aal2 requirement therefore refuses exactly the people on the
    recovery path — lost phone, redeemed a code, now locked out of the API and
    unable to reach Settings to turn 2FA off. Worse than the gap it closes, and
    invisible until a real person loses a real phone.

    What works needs session-scoped server-side state ("this `session_id`
    satisfied the second factor") so the policy reads *aal2 OR redeemed* —
    a migration, keyed on the SESSION and never the user, because a
    user-keyed marker lets one redemption bless every aal1 token they ever
    hold. Held for a founder decision rather than shipped: the failure mode
    is locking real customers out of their own accounts.
    `tests/unit/test_mfa_backend_enforcement_tripwire.py` fires the day
    somebody adds the claim without the accommodation.
    """

    user_id: str
    email: str | None = None
    role: str = "authenticated"
    company_ids: list[str] = []
    #: GoTrue's ``session_id`` claim — which row in ``auth.sessions`` this
    #: request is riding on. It marks "this device" in the sessions list, keeps
    #: that row from being the one somebody revokes by accident, and — since
    #: 2026-09-09 — is what ``session_liveness.session_revoked`` looks up, so a
    #: signed-out device stops being served on its next request instead of an
    #: hour later.
    #:
    #: ⚠ Optional on purpose. It is not documented as a guaranteed claim and
    #: this container cannot decode a live production token to prove it is
    #: present, so an absent claim must degrade to "cannot tell which device
    #: you are on" rather than crash a settings page or — far worse — label
    #: the wrong row as current. Gotcha 55: a guard that cannot see declines
    #: to assert.
    session_id: str | None = None


def expected_issuer(supabase_url: str) -> str:
    """The ``iss`` GoTrue writes into this project's access tokens.

    The project URL plus ``/auth/v1``, no trailing slash: measured on
    helixis-test's edge logs as ``https://abcdefghijklmnopqrst.supabase.co/auth/v1``.
    A trailing slash on the configured URL is dropped first, as the JWKS URL does.
    """
    return f"{supabase_url.strip().rstrip('/')}/auth/v1"


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _unchecked() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=_UNCHECKED_DETAIL,
        headers={"Retry-After": _RETRY_AFTER},
    )


def _verified(token: str, key: object, algorithms: list[str], issuer: str) -> dict:
    """``jwt.decode`` with the checks every token gets: signature, ``exp``,
    audience "authenticated" and this project's issuer."""
    try:
        return jwt.decode(
            token,
            key,  # type: ignore[arg-type]
            algorithms=algorithms,
            audience="authenticated",
            issuer=issuer,
            options={"require": ["sub", "exp", "role", "iss"]},
        )
    except jwt.ExpiredSignatureError:
        raise _unauthorized("Token has expired")
    except jwt.InvalidIssuerError:
        # The signature already held (PyJWT checks it before the claims), so a
        # signer we trust wrote another issuer: most likely a supabase.url that
        # does not match the project. Logged with both, neither is a secret.
        logger.warning(
            "Refused a token issued by %r; this API expects %r (HELIXIS_SUPABASE__URL)",
            _unverified_issuer(token),
            issuer,
        )
        raise _unauthorized("Invalid token: it was not issued by this project's Supabase Auth")
    except jwt.InvalidTokenError as exc:
        raise _unauthorized(f"Invalid token: {exc}")


def _unverified_issuer(token: str) -> object:
    try:
        return jwt.decode(token, options={"verify_signature": False}).get("iss")
    except jwt.InvalidTokenError:
        return None


def _decode_jwt(token: str) -> dict:
    """Decode and verify a Supabase JWT.

    The header's ``alg`` picks the ONE verifier that runs: ES256/RS256 against
    the project's JWKS, HS256 against ``auth.jwt_secret``; any other is refused.
    Every token must carry this project's issuer. Returns the decoded claims;
    raises HTTPException(401) for a token that is not good, and (503) when it
    could not be checked: no ``supabase.url`` configured, or the JWKS could not
    be fetched.
    """
    settings = get_settings()
    supabase_url = settings.supabase.url.strip().rstrip("/")
    anon_key = settings.supabase.anon_key
    jwt_secret = settings.auth.jwt_secret

    if not supabase_url:
        # No issuer to compare and no JWKS to fetch. Refusing every token is the
        # only answer that never accepts one whose issuer nobody checked.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication not configured",
        )
    issuer = expected_issuer(supabase_url)

    # The unverified header routes to the verifier. A header that won't parse
    # is a malformed token: 401 without trying either.
    try:
        unverified_header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise _unauthorized(f"Invalid token: {exc}")
    alg = unverified_header.get("alg", "")

    if alg in _ASYMMETRIC_ALGS:
        try:
            signing_key = _get_jwks_client(supabase_url, anon_key).get_signing_key_from_jwt(token)
        except (jwt.PyJWKClientConnectionError, json.JSONDecodeError) as exc:
            # The key set could not be fetched (network, a 5xx, not JSON). That
            # says nothing about the token: "try again", never "signed out".
            logger.warning("Couldn't fetch the JWKS for alg=%s url=%s: %s", alg, supabase_url, exc)
            raise _unchecked()
        except (jwt.PyJWKClientError, jwt.InvalidTokenError) as exc:
            # The set was read and has no key for this token (an unknown kid).
            logger.warning("JWKS has no key for alg=%s url=%s: %s", alg, supabase_url, exc)
            raise _unauthorized(f"Invalid token: {exc}")
        return _verified(token, signing_key.key, list(_ASYMMETRIC_ALGS), issuer)

    if alg == "HS256":
        if not jwt_secret:
            raise _unauthorized(
                "Token uses HS256 but no jwt_secret is configured. "
                "Set HELIXIS_AUTH__JWT_SECRET to your Supabase project's "
                "JWT secret (Settings → API → JWT Settings)."
            )
        return _verified(token, jwt_secret, ["HS256"], issuer)

    # An algorithm we don't verify (e.g. "none"). Be explicit about which.
    raise _unauthorized(
        f"Invalid token: unsupported alg='{alg}'. "
        "Expected ES256 / RS256 (Supabase ECC keys) or HS256 (legacy)."
    )


def _extract_bearer_token(authorization: str | None) -> str:
    """Extract the JWT from a 'Bearer <token>' header value."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header must be 'Bearer <token>'",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return parts[1]


async def _resolve_company_ids(user_id: str) -> list[str]:
    """Look up which companies a user belongs to via Supabase REST API.

    Uses the service_key (bypasses RLS) to query company_users.
    """
    settings = get_settings()
    if not settings.supabase.url or not settings.supabase.service_key:
        return []

    import httpx

    url = f"{settings.supabase.url}/rest/v1/company_users"
    # ``order`` makes the REST result deterministic; the Python sort below is
    # the belt-and-suspenders guarantee regardless of what the API returns.
    params = {"select": "company_id", "user_id": f"eq.{user_id}", "order": "company_id.asc"}
    headers = {
        "apikey": settings.supabase.service_key,
        "Authorization": f"Bearer {settings.supabase.service_key}",
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            rows = resp.json()
            # #68: sort so ``company_ids[0]`` (the de-facto "active company" at
            # ~30 call sites) is DETERMINISTIC. Without this, a multi-company
            # user lands in whatever order Postgres happened to return — an
            # arbitrary company per request. A single-company user is
            # unaffected (one-element list).
            #
            # ⚠ DETERMINISTIC IS NOT CORRECT, and the difference cost a real
            # customer their workspace. a teammate account belonged to My Company (11
            # properties, Buildium connected) AND to their own signup shell;
            # the shell's uuid sorted first, so every request resolved to the
            # empty one. They saw a blank workspace and the invite was inert
            # for weeks. A second user in the identical state was fine because
            # their real company happened to sort first — a coin flip. The
            # existing test asserts only that the answer is the SAME each
            # time, which that state satisfied perfectly.
            #
            # The signup trigger creates every auto-provisioned company with
            # ``companies.id = auth.users.id`` (0034_reconcile_team_roles.sql),
            # so a company whose id EQUALS the caller's own user id is their
            # personal shell. Sorting those last costs no extra query and
            # breaks the tie toward the company somebody actually invited them
            # to. A user in exactly one company is unaffected either way —
            # including a brand-new customer whose only company IS their shell,
            # since a one-element list has nothing to order.
            company_ids = sorted(row["company_id"] for row in rows)
            if len(company_ids) > 1:
                # One account, one company (founder directive 2026-09-03). This
                # is now an invariant violation, not a supported shape — and it
                # is invisible without a log, which is how the last one lasted
                # weeks. Not an error: refusing here would lock out exactly the
                # people already in the broken state.
                logger.error(
                    "user %s resolves to %d companies (%s) — one-account-one-company "
                    "violated; using %s",
                    user_id,
                    len(company_ids),
                    ", ".join(company_ids),
                    min(company_ids, key=lambda cid: (cid == user_id, cid)),
                )
            return sorted(company_ids, key=lambda cid: (cid == user_id, cid))
    except Exception:
        logger.exception("Failed to resolve company_ids for user %s", user_id)
        return []


async def get_current_user(request: Request) -> AuthenticatedUser:
    """FastAPI dependency: extract and verify JWT, resolve user.

    Usage:
        @router.post("/something")
        async def endpoint(user: AuthenticatedUser = Depends(get_current_user)):
            ...
    """
    authorization = request.headers.get("Authorization")
    token = _extract_bearer_token(authorization)
    payload = _decode_jwt(token)

    user_id = payload["sub"]
    email = payload.get("email")
    role = payload.get("role", "authenticated")

    # BEFORE `_resolve_company_ids`, which is a full HTTPS round trip to
    # Supabase REST: a token that has been signed out should not spend it.
    #
    # A check that could not run is a 503, never a pass: serving the request
    # would keep a signed-out device in for as long as the database is away.
    try:
        revoked = await session_revoked(payload.get("session_id"), user_id)
    except SessionCheckUnavailableError:
        raise _unchecked()
    if revoked:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_REVOKED_DETAIL,
            headers={"WWW-Authenticate": "Bearer"},
        )

    company_ids = await _resolve_company_ids(user_id)

    return AuthenticatedUser(
        user_id=user_id,
        email=email,
        role=role,
        company_ids=company_ids,
        session_id=payload.get("session_id"),
    )


async def get_ws_user(
    websocket: WebSocket,
    token: Annotated[str | None, Query()] = None,
) -> AuthenticatedUser:
    """WebSocket authentication via ?token= query parameter."""
    if not token:
        await websocket.close(code=4001, reason="Missing token query parameter")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing token",
        )

    payload = _decode_jwt(token)
    user_id = payload["sub"]
    email = payload.get("email")
    role = payload.get("role", "authenticated")

    # The same check as the HTTP path. A socket is the LONGER-lived of the
    # two, so leaving it out would mean the one connection a signed-out
    # device keeps is the one that stays open (gotcha 25 — every layer that
    # reads the rule, not just the one where the bug was reported).
    #
    # A check that could not run closes with 1013 ("try again later"), the
    # standard code for a temporary refusal, rather than 4001 (signed out).
    try:
        revoked = await session_revoked(payload.get("session_id"), user_id)
    except SessionCheckUnavailableError:
        await websocket.close(code=1013, reason="Try again")
        raise _unchecked()
    if revoked:
        await websocket.close(code=4001, reason="Signed out")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_REVOKED_DETAIL,
        )

    company_ids = await _resolve_company_ids(user_id)

    return AuthenticatedUser(
        user_id=user_id,
        email=email,
        role=role,
        company_ids=company_ids,
        session_id=payload.get("session_id"),
    )


def require_unambiguous_company(user: AuthenticatedUser) -> str:
    """The caller's ONE company, or refuse.

    ⚠ For the three actions that cannot be taken back: buying a phone number
    (a recurring charge with no undo), filing the A2P legal attestation
    (one-way — the row locks to `submitted` and `company_id` is UNIQUE, so the
    real company can never get its own), and opening Stripe Checkout or the
    billing portal.

    Every other route resolves `company_ids[0]` and that is fine: reads and
    reversible writes can afford a wrong-workspace mistake, because the user
    sees an empty screen and says something. These three cannot. A number
    bought against the wrong company still bills monthly; an attestation filed
    against the wrong one names the wrong business to a carrier and cannot be
    edited; a subscription pays for a company whose turns nobody is spending
    while the company actually working returns 402.

    One account, one company (founder directive 2026-09-03), so more than one
    is now an invariant VIOLATION rather than a supported shape.
    `_resolve_company_ids` orders the caller's own signup shell last, which
    makes `[0]` right for the invited-teammate case — but ordering is a
    tie-break, not knowledge, and it says nothing about a user who somehow
    holds two REAL companies. Guessing is what these three cannot afford, so
    they decline instead (gotcha 55: a guard that cannot see must refuse to
    act, never act on a coin flip).

    409 rather than 403: the caller is not forbidden, their account is in a
    state the product does not support, and the fix is somebody looking at it.
    """
    if not user.company_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No company on this account",
        )
    if len(user.company_ids) > 1:
        logger.error(
            "refusing an irreversible action: user %s spans %d companies (%s)",
            user.user_id,
            len(user.company_ids),
            ", ".join(user.company_ids),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Your account belongs to more than one workspace, so we can't tell "
                "which one this should apply to. Email " + SUPPORT_EMAIL + " and we'll "
                "sort it out — nothing has been charged or submitted."
            ),
        )
    return user.company_ids[0]


def require_company_access(user: AuthenticatedUser, company_id: str) -> None:
    """Validate that the authenticated user has access to the given company_id.

    Raises HTTP 403 if the user is not a member of the company.
    """
    if company_id not in user.company_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You do not have access to company '{company_id}'",
        )
