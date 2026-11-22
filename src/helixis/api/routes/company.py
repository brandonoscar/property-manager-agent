"""Company management endpoints.

PATCH /company         — rename the caller's company
POST  /company/invite  — invite a teammate into the caller's company

Built for the onboarding wizard (ADR 0003): the wizard collects a
workspace name and team invites, and until now both were UI-only
stubs because the backend had no counterpart.

Invites ride on Supabase GoTrue's admin invite (the service_key this
deployment already holds for ``_resolve_company_ids``): a new address gets a
Supabase-sent email, and its membership row exists before they first sign in.
An address that already has an account gets an invite that waits for them
(``company_member_invites``, migration 0112): they accept or decline it in the
app (``/company/invites``), and until they accept it is not a membership
anywhere. Since 2026-11-11 the backend is the only writer of ``company_users``
for anyone but the caller.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from pydantic import BaseModel, BeforeValidator, Field

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.rate_limit import connectors_limiter, invite_limiter
from helixis.api.roles import ADMIN, MANAGER, MEMBER, ROLES, normalize_role, role_at_least
from helixis.billing.seats import sync_company_seats
from helixis.buildium.purge import purge_buildium_mirror
from helixis.comms.consent_ownership import (
    revoke_for_departing_member,
    unattributed_manager_numbers,
)
from helixis.db.engine import get_pool
from helixis.db.repositories import company_knowledge as company_knowledge_repo
from helixis.db.repositories import member_invites
from helixis.db.repositories import members as members_repo
from helixis.db.repositories.activity import ActivityEntry, write_activity
from helixis.exports.company_export import build_export, export_filename
from helixis.memory.manager import get_memory_manager
from helixis.settings import get_settings
from helixis.storage_purge import purge_company_storage

logger = logging.getLogger(__name__)

# Deliberately loose — real validation is GoTrue's job; this only
# catches obvious typos without pulling in email-validator.
#
# ⚠ **`$` IS CORRECT HERE AND `\Z` DOES NOT COMPILE.** Both halves of that
# sentence were learned the hard way on 2026-09-09, and the reason is that
# THIS PATTERN IS NOT RUN BY PYTHON'S `re`. Pydantic 2 compiles `pattern=`
# with the Rust `regex` crate:
#
#   * Python's `re` lets `$` match before a trailing newline, so
#     `re.match(_EMAIL_SHAPE, "a@b.co\n")` is a MATCH. Reading the constant
#     and testing it in a REPL therefore says this route accepts a pasted
#     spreadsheet cell — and that is a conclusion drawn from the wrong engine.
#   * Rust's `$` matches end-of-haystack only. Measured against the real
#     model: `"a@b.co\n"` is REJECTED, along with leading and trailing spaces.
#   * Rust's regex has no `\Z` at all. "Fixing" the imaginary bug raises
#     `SchemaError: unrecognized escape sequence` while BUILDING the model —
#     an import-time crash that takes the whole API down at boot.
#
# So: do not "harden" this anchor. If a future reader needs to check what this
# pattern accepts, exercise the MODEL (`InviteRequest(email=...)`), never the
# string through `re`. `tests/unit/test_invite_email_normalisation.py` pins
# both directions.
_EMAIL_SHAPE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


def _normalise_email(value: object) -> object:
    """Strip and lower-case an address BEFORE the pattern sees it.

    ⚠ This is a USABILITY fix, not a security one, and the distinction matters
    because the original justification for it was wrong. Whitespace was never
    getting through — the Rust engine rejects it (see above). What actually
    happened is that pasting `"  Person@Example.com "` out of a spreadsheet or
    an email client returned a 422 whose body is a raw regex, which tells an
    admin nothing about the space they cannot see. Now it just works.

    ⚠ Lower-casing here rather than at the call sites is the part that removes
    a real footgun. `_invite_one` looks the invitee up with
    `WHERE lower(email) = $1` and the bulk route dedups on the address, so
    every caller has to remember to normalise; two of them did `.lower()` on
    the email while doing `.strip().lower()` on the role two lines away. One
    declaration means a third caller cannot get it wrong.

    Non-strings pass through untouched so Pydantic reports the type error
    itself, rather than this raising an AttributeError from inside a validator.
    """
    return value.strip().lower() if isinstance(value, str) else value


#: The one email field declaration. Both invite models use it, so the
#: normalisation and the pattern cannot drift apart between the single-address
#: and bulk routes — which is how the whitespace asymmetry got in: `role` was
#: `.strip().lower()`-ed at the call site and `email` was only `.lower()`-ed,
#: two lines apart.
EmailAddress = Annotated[
    str,
    BeforeValidator(_normalise_email),
    Field(pattern=_EMAIL_SHAPE, max_length=254),
]

router = APIRouter(prefix="/company", tags=["company"])

# Roles assignable through an invite / a role change (ADR 0005). The reconciled
# vocabulary is admin/manager/member — there is no separate un-invitable top
# role now that 'owner' is gone. Assigning 'admin' by invite is allowed (an
# admin can add another admin), but the last-admin guard keeps a company from
# ever losing its final admin.
INVITABLE_ROLES = set(ROLES)


class RenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    # Optional target — defaults to the caller's first company. Must be a
    # company the caller belongs to (review finding #3: multi-company users
    # could previously only ever act on company_ids[0]).
    company_id: str | None = None


class RenameResponse(BaseModel):
    company_id: str
    name: str


class InviteRequest(BaseModel):
    email: EmailAddress
    role: str = Field(default="member")
    # Optional target company (review finding #3) — must be one the caller
    # belongs to; defaults to their first company.
    company_id: str | None = None


MAX_BULK_INVITES = 25
"""Ceiling on one bulk invite. See ``BulkInviteRequest`` for why it exists."""


class InviteEntry(BaseModel):
    """One address in a bulk invite. Same field shapes as ``InviteRequest``.

    ⚠ The field DECLARATION is now shared too (``EmailAddress``), not just the
    pattern constant. It used to be repeated "because it is the same constant"
    — and that reasoning held for the pattern while letting the NORMALISATION
    differ, which is precisely the gap a pasted address fell through.
    """

    email: EmailAddress
    role: str = Field(default="member")


class BulkInviteRequest(BaseModel):
    """Several people at once, each with their own role.

    ⚠ THIS EXISTS FOR TWO REASONS THE CLIENT CANNOT SOLVE BY LOOPING, and
    neither is convenience:

    1. ``invite_limiter`` is deliberately tight because this endpoint sends mail
       to arbitrary addresses. A client looping five invites spends five of that
       budget and the admin watches the last two fail for a reason that is about
       our rate limiter rather than about their team.
    2. ``sync_company_seats`` pushes a quantity to STRIPE. Looping N invites
       pushes N quantities — 2, 3, 4, 5, 6 — and Stripe prorates each one, so a
       single "add four teammates" lands four proration lines on the customer's
       next invoice for a change they made once. The batch syncs ONCE, at the
       end, from the membership count the whole batch produced.

    ``MAX_BULK_INVITES`` is a blast radius, not a product cap: this is one
    request that can send arbitrary mail, and an unbounded list is a spam
    cannon pointed at our own sending reputation.
    """

    invites: list[InviteEntry] = Field(min_length=1, max_length=MAX_BULK_INVITES)
    company_id: str | None = None


class InviteOutcome(BaseModel):
    """What happened to ONE address in a batch.

    ⚠ A batch NEVER fails as a unit on one bad address. Four good invites plus
    one typo has to land the four and name the one — the alternative is an admin
    re-typing four addresses they already got right, and no way to tell which of
    the five was the problem.
    """

    email: str
    role: str
    status: str
    """``invited`` (mail sent, member now) · ``pending`` (existing account: waits for
    them to accept in the app, no mail) · ``already_member`` (no-op) · ``failed``
    (see ``error``)."""

    email_sent: bool = False
    error: str | None = None
    """The server's own words for this address, or null. Rendered verbatim."""


class BulkInviteResponse(BaseModel):
    results: list[InviteOutcome]


class InviteResponse(BaseModel):
    """``email_sent`` is the ONE bit the admin needs and used to be withheld.

    This response was fully uniform, so an admin could not tell "invite
    emailed" from "they already had an account, so I added them and no email
    is coming". That is the difference between waiting and picking up the
    phone, and on 2026-08-31 it cost a real invite: a teammate account was added to My
    Company, no mail was ever attempted, and nobody knew why.

    ⚠ The enumeration guard it traded away was real but narrow. Knowing an
    address is registered is only reachable by someone who is ALREADY an admin
    of the company they are inviting into, is rate-limited by
    ``invite_limiter``, and lands the invitee in that admin's team list where
    they can see them anyway. The admin not knowing whether to expect an email
    costs more than that. The exact ROLE, user id and prior-company details
    stay unexposed — only "did mail go out" is surfaced.
    """

    email: str
    role: str
    status: str = "ok"
    email_sent: bool = True
    """False when the address already had an account: no mail was attempted,
    so the admin must tell them to log in rather than wait for an invite."""
    awaiting_acceptance: bool = False
    """True when the address already had an account: they are NOT on the team
    until they sign in and accept (migration 0112). Defaults False so an older
    client that does not read it is unchanged."""


def _resolve_target_company(user: AuthenticatedUser, requested: str | None) -> str:
    """The company a mutation targets. Defaults to the caller's first
    company; an explicit ``company_id`` must be one the caller belongs to.
    Membership is enforced here; the owner/manager gate is separate."""
    if not user.company_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No company membership found for this user.",
        )
    if requested is None:
        return user.company_ids[0]
    if requested not in user.company_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not a member of that company.",
        )
    return requested


def _require_pool():  # noqa: ANN202 — asyncpg pool type is dynamic
    pool = get_pool()
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database pool not initialised.",
        )
    return pool


async def require_role(  # noqa: ANN001
    pool, user_id: str, company_id: str, minimum: str, *, detail: str | None = None
) -> str:
    """Primary role gate (ADR 0005): 403 unless the caller's role in
    ``company_id`` is at least ``minimum``. Returns the caller's normalized role.

    The stored role is normalized first, so a not-yet-migrated legacy 'owner'
    row satisfies an ``ADMIN`` minimum — the deployed backend must accept both
    during the deploy→migrate window for 0034 (see the migration header). RLS on
    ``company_users`` is the defense-in-depth backstop; this is the primary gate.
    """
    row = await pool.fetchrow(
        "SELECT role FROM public.company_users WHERE user_id = $1::uuid AND company_id = $2::uuid",
        user_id,
        company_id,
    )
    role = normalize_role(row["role"]) if row else None
    if role is None or not role_at_least(role, minimum):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=detail or f"This action requires the {minimum} role.",
        )
    return role


@router.patch("")
async def rename_company(
    body: RenameRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> RenameResponse:
    """Rename the caller's company (the wizard's 'workspace name')."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _resolve_target_company(user, body.company_id)
    pool = _require_pool()
    await require_role(
        pool, user.user_id, company_id, ADMIN, detail="Only admins can rename the company."
    )

    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Name must not be blank.")

    await pool.execute(
        "UPDATE public.companies SET name = $2 WHERE id = $1::uuid",
        company_id,
        name,
    )
    return RenameResponse(company_id=company_id, name=name)


def _invite_redirect_url(settings) -> str | None:  # noqa: ANN001 — Settings type
    """Where the invite email's link should return the invitee.

    Explicit ``api.public_app_url`` wins; otherwise the first https:// entry in
    ``cors_origins`` (the deployed web app), so the invite lands on the app and
    its set-password screen instead of Supabase's Site URL, which has drifted to
    retired domains before (gotcha 3). ``None`` (no https origin configured) →
    fall back to GoTrue's Site URL (the pre-fix behavior). Pure + testable."""
    explicit = (settings.api.public_app_url or "").strip()
    if explicit:
        return explicit.rstrip("/")
    for origin in settings.api.cors_origins:
        if isinstance(origin, str) and origin.startswith("https://"):
            return origin.rstrip("/")
    return None


async def _gotrue_invite(email: str) -> str | None:
    """Admin-invite ``email`` via GoTrue; returns the new user id, or
    None when the address already has an account (422 from GoTrue)."""
    settings = get_settings()
    url = settings.supabase.url.rstrip("/")
    headers = {
        "apikey": settings.supabase.service_key,
        "Authorization": f"Bearer {settings.supabase.service_key}",
        "Content-Type": "application/json",
    }
    # ``redirect_to`` overrides the project Site URL for THIS invite link, so the
    # invitee returns to the app (→ set-password) instead of a stale dashboard
    # URL. Passed as a query param per the GoTrue invite contract. Omitted when
    # no app URL resolves, preserving the original Site-URL behavior.
    redirect = _invite_redirect_url(settings)
    params = {"redirect_to": redirect} if redirect else None
    async with httpx.AsyncClient(timeout=10.0) as client:
        res = await client.post(
            f"{url}/auth/v1/invite", headers=headers, json={"email": email}, params=params
        )
    if res.status_code == 422:
        return None  # already registered — caller resolves the id from auth.users
    if res.status_code >= 400:
        logger.error("GoTrue invite failed (%s): %s", res.status_code, res.text[:300])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The invite email could not be sent. Try again shortly.",
        )
    body = res.json()
    user_id = body.get("id")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Unexpected response from the auth service.",
        )
    return str(user_id)


def _require_valid_role(role: str) -> None:
    """Reject anything outside the canonical set, with a 422.

    ⚠ Validate LITERALLY — do NOT run client input through ``normalize_role``.
    That helper is for reading legacy DB rows and folds an unknown value to
    ``member`` rather than rejecting it, so using it here would silently accept
    typos and the retired 'owner'/'employee' aliases as a member invite.
    """
    if role not in INVITABLE_ROLES:
        raise HTTPException(
            status_code=422,
            detail=f"Role must be one of: {', '.join(sorted(INVITABLE_ROLES))}.",
        )


def _require_invites_configured() -> None:
    """A DEPLOYMENT-level precondition, checked once per request.

    Deliberately not per-address: a missing service key is not a fact about
    somebody's email, so reporting it 25 times inside a batch would describe one
    broken deployment as 25 rejected people.
    """
    settings = get_settings()
    if not settings.supabase.url or not settings.supabase.service_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Invites are not configured on this deployment.",
        )


async def _invite_one(
    pool,  # noqa: ANN001 — asyncpg.Pool
    *,
    company_id: str,
    actor_user_id: str,
    email: str,
    role: str,
) -> tuple[str, bool]:
    """Invite ONE address. Returns ``(outcome, email_sent)``.

    ⚠ THE RULE LIVES ONCE (gotcha 25). Both ``POST /company/invite`` and
    ``POST /company/invite/bulk`` call this, so the acceptance rule, the audit entry
    and the shell-drop cannot be right on one path and wrong on the other. The two
    routes own only what genuinely differs between them: the rate limit, the role
    check, and WHEN seats are synced.

    Two paths, decided BEFORE GoTrue is asked anything:

    * **The address already has an account** → ``pending``. The invite waits in
      ``company_member_invites`` until that person signs in and accepts it
      (``accept_invite``). Nothing is written to ``company_users``, so nothing
      changes for them: not their companies, not their active one, not the books.
      Until 2026-11-11 this path inserted their membership at once, and because the
      backend and the web app make the sign-up company LAST, any account holder
      (everyone admins their own sign-up company) could make their company a
      stranger's active one by typing the stranger's email.
    * **No account** → ``invited``. GoTrue creates the account and emails the link;
      clicking it is the acceptance, so the membership is written now and the
      seconds-old sign-up shell GoTrue's insert triggered is dropped.

    ⚠ Looked up FIRST, never inferred from GoTrue's answer. GoTrue's admin invite
    answers 422 for a CONFIRMED account but 200 for one that exists unconfirmed (it
    re-sends the invite), so "200 means new" let anyone capture an account between
    sign-up and its confirmation email. A GoTrue 422 after the lookup found nothing
    (someone confirmed in between) is treated as existing too.

    ⚠ Does NOT sync seats. That is the caller's job precisely because the batch
    must do it once — see ``BulkInviteRequest``. A pending invite takes no seat:
    the seat is counted when it is accepted.

    Raises ``HTTPException`` on a per-address failure; the bulk caller catches
    it and records the address as failed rather than losing the batch.
    """
    # ⚠ Also validated by the single-invite ROUTE, before the config check, to
    # preserve the documented order (see `invite_member`). Repeating it here is
    # what makes a bad role a PER-ADDRESS failure in a batch rather than a 422
    # that loses everybody else's invites. The check is idempotent and free.
    _require_valid_role(role)

    existing = await _account_for(pool, email)
    if existing is None:
        fresh_user_id = await _gotrue_invite(email)
        # GoTrue created the account on this request: the lookup above found none.
        fresh_invite = fresh_user_id is not None
        if fresh_user_id is not None:
            await _add_fresh_invitee(
                pool,
                company_id=company_id,
                actor_user_id=actor_user_id,
                email=email,
                role=role,
                invited_user_id=fresh_user_id,
                fresh_invite=fresh_invite,
            )
            return "invited", fresh_invite
        # 422: the account appeared after the lookup. It is an existing account.
        existing = await _account_for(pool, email)
        if existing is None:
            # 422 from GoTrue but no row — eventual consistency or a
            # different 422 cause. Surface honestly rather than guess.
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Could not resolve an account for that email.",
            )

    if await members_repo.get_member_role(pool, company_id=company_id, user_id=existing):
        logger.info("Invite: company=%s role=%s outcome=already_member", company_id, role)
        return "already_member", False

    try:
        await member_invites.upsert(
            pool,
            company_id=company_id,
            user_id=existing,
            role=role,
            invited_by=actor_user_id,
        )
    except member_invites.InvitesUnavailableError:
        # Fails CLOSED. Before migration 0112 the only other way to "invite" an
        # existing account is the one this replaced: add them without asking.
        logger.warning("Invite of an existing account refused: migration 0112 not applied")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "That address already has an Occupella account, and inviting an "
                "existing account isn't switched on yet. Try again later."
            ),
        ) from None

    logger.info(
        "Invite: company=%s role=%s outcome=pending (user_id=%s)", company_id, role, existing
    )
    await write_activity(
        pool,
        ActivityEntry(
            account_id=None,
            company_id=company_id,
            source="manual",
            action_type="member_invited",
            description=f"invited {email} as {role}; waiting for them to accept",
            user_id=actor_user_id,
            raw={"email": email, "role": role, "outcome": "pending"},
        ),
    )
    return "pending", False


async def _account_for(pool, email: str) -> str | None:  # noqa: ANN001 — asyncpg.Pool
    """The id of the account registered under ``email``, or None. Global by design
    (rule 11): whether an address has an account is the question."""
    row = await pool.fetchrow("SELECT id FROM auth.users WHERE lower(email) = $1", email)
    return str(row["id"]) if row else None


async def _add_fresh_invitee(
    pool,  # noqa: ANN001 — asyncpg.Pool
    *,
    company_id: str,
    actor_user_id: str,
    email: str,
    role: str,
    invited_user_id: str,
    fresh_invite: bool,
) -> None:
    """The membership of an account GoTrue created on THIS request.

    The only membership written for somebody other than the caller without their
    click in the app, and it is safe for one reason: the account did not exist a
    moment ago (``_invite_one`` looked first), so it has nothing to lose, and the
    invite email's link is the only way into it. The tripwire in
    ``tests/unit/test_invites_need_acceptance.py`` allows this insert and the
    accept route's, and no other.
    """
    await pool.execute(
        "INSERT INTO public.company_users (user_id, company_id, role) "
        "VALUES ($1::uuid, $2::uuid, $3) "
        "ON CONFLICT (company_id, user_id) DO NOTHING",
        invited_user_id,
        company_id,
        role,
    )

    # One account, one company (founder directive 2026-09-03). The signup trigger
    # is `after insert on auth.users`, and GoTrue's admin invite CREATES that row,
    # so a brand-new invitee is handed a personal workspace too, then added to the
    # inviting company, and would land in two. That shell is seconds old and nobody
    # has ever used it, so its membership is dropped here.
    #
    # ⚠ "Auto-provisioned" is NOT the same as "throwaway": EVERY company is created
    # by the signup trigger with id = the founder's user id, Blue Ridge Properties
    # included (50 properties, 75 leases, 3 members). The emptiness conditions
    # below are a second check, never the first: they cannot tell a throwaway shell
    # from a REAL company that is merely new (and they cannot see the books, which
    # live in another database). The first check is that this account was created
    # by this request: this function is only called then, and ``fresh_invite`` is
    # bound into the statement as well, so a future caller that passes anything
    # else deletes nothing.
    dropped = await pool.execute(
        """
            DELETE FROM public.company_users cu
             WHERE cu.user_id = $1::uuid
               AND $3::boolean
               AND cu.company_id <> $2::uuid
               AND cu.company_id = cu.user_id
               AND NOT EXISTS (SELECT 1 FROM public.company_buildium_credentials b
                                WHERE b.company_id = cu.company_id)
               AND NOT EXISTS (SELECT 1 FROM public.helixis_properties p
                                WHERE p.company_id = cu.company_id)
               AND NOT EXISTS (SELECT 1 FROM public.helixis_leases l
                                WHERE l.company_id = cu.company_id)
               AND NOT EXISTS (SELECT 1 FROM public.company_users o
                                WHERE o.company_id = cu.company_id
                                  AND o.user_id <> cu.user_id)
        """,
        invited_user_id,
        company_id,
        fresh_invite,
    )
    dropped_shell = dropped != "DELETE 0"

    logger.info(
        "Invite: company=%s role=%s outcome=invited (user_id=%s)",
        company_id,
        role,
        invited_user_id,
    )
    # Review finding #2: the membership change is accountable in the Activity log
    # — who added whom, when (best-effort, never raises).
    await write_activity(
        pool,
        ActivityEntry(
            account_id=None,
            company_id=company_id,
            source="manual",
            action_type="member_invited",
            description=f"invited {email} as {role}",
            user_id=actor_user_id,
            raw={
                "email": email,
                "role": role,
                "outcome": "invited",
                "dropped_empty_shell": dropped_shell,
            },
        ),
    )


@router.post("/invite")
async def invite_member(
    body: InviteRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> InviteResponse:
    """Invite ``email`` into the caller's company with ``role``.

    Fresh address → GoTrue sends a Supabase invite email and the membership row is
    created up front (the link is the acceptance). Existing account → an invite
    that waits for them to accept it in the app (``_invite_one``): no membership,
    no email. Idempotent: re-inviting a member is a no-op, re-inviting someone
    still deciding renews their invite.

    The response says only whether mail went out and whether the invite waits
    (see ``InviteResponse``); user ids and other companies stay unexposed. Uses
    the tight ``invite_limiter`` because this endpoint can send mail to arbitrary
    addresses.
    """
    await invite_limiter.check_by_user(request, user)
    company_id = _resolve_target_company(user, body.company_id)
    pool = _require_pool()
    await require_role(
        pool, user.user_id, company_id, ADMIN, detail="Only admins can invite teammates."
    )
    # ⚠ ORDER IS LOAD-BEARING, and `scripts/role_matrix.py` depends on this
    # exact sequence. That probe sends a VALID email with an INVALID role and
    # reads 403 as "the admin floor fired" and 422 as "the floor let me past" —
    # so the role floor must run BEFORE this check, and this check must run
    # BEFORE the deployment-config one, or an unconfigured deployment answers
    # 503 where the probe expects 422 and the floor stops being tested.
    _require_valid_role(body.role.strip().lower())
    _require_invites_configured()

    outcome, fresh_invite = await _invite_one(
        pool,
        company_id=company_id,
        actor_user_id=user.user_id,
        # Already stripped + lower-cased by ``EmailAddress``; see _normalise_email.
        email=body.email,
        role=body.role.strip().lower(),
    )

    # ⚠ AFTER the membership write, and never gating it (billing/seats.py).
    # A seat is counted when a company_users row is created: here for a fresh
    # invitee, at acceptance for an existing account (a pending invite takes no
    # seat). If a Stripe call fails here the invite still stands and the
    # reconciler repairs the quantity; the reverse order would bill for somebody
    # whose row never landed, which nothing self-heals.
    if outcome == "invited":
        await sync_company_seats(pool, get_settings(), company_id, reason="invite")

    return InviteResponse(
        email=body.email,
        role=body.role.strip().lower(),
        email_sent=fresh_invite,
        awaiting_acceptance=outcome == "pending",
    )


@router.post("/invite/bulk")
async def invite_members_bulk(
    body: BulkInviteRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> BulkInviteResponse:
    """Invite several people in one request, reporting on each address.

    ⚠ ONE rate-limit check and ONE seat sync for the whole batch — those are the
    two reasons this endpoint exists rather than the client looping (see
    ``BulkInviteRequest``). Everything else per address is ``_invite_one``, so
    this route cannot drift from the single-invite path.

    ⚠ NEVER fails as a unit on one bad address. A per-address ``HTTPException``
    is caught and recorded; the batch's own status code stays 200 and the
    caller reads ``results``. The exceptions to that are deliberately
    batch-level: an unauthenticated caller, a non-admin, a rate limit, and an
    unconfigured deployment all fail the whole request, because none of them is
    a fact about anybody's email address.
    """
    await invite_limiter.check_by_user(request, user)
    company_id = _resolve_target_company(user, body.company_id)
    pool = _require_pool()
    await require_role(
        pool, user.user_id, company_id, ADMIN, detail="Only admins can invite teammates."
    )
    _require_invites_configured()

    # ⚠ Deduplicated on the ADDRESS, keeping the FIRST role given for it. The
    # same address twice in one batch is a paste artefact, not an instruction to
    # invite somebody twice — and the second call would land as
    # `already_member`, which reads to the admin like the first one failed.
    seen: set[str] = set()
    results: list[InviteOutcome] = []
    any_membership_written = False

    for entry in body.invites:
        email = entry.email  # normalised by ``EmailAddress`` on the model
        role = entry.role.strip().lower()
        if email in seen:
            continue
        seen.add(email)
        try:
            outcome, fresh_invite = await _invite_one(
                pool,
                company_id=company_id,
                actor_user_id=user.user_id,
                email=email,
                role=role,
            )
        except HTTPException as exc:
            # The server's own words for THIS address, so the admin can tell a
            # typo from an outage without reading a log.
            results.append(
                InviteOutcome(email=email, role=role, status="failed", error=str(exc.detail))
            )
            continue
        except Exception:
            # ⚠ An unexpected error on one address must not take the batch — the
            # invites already written are real, and losing the report of them is
            # how an admin re-sends and double-invites. Logged with a traceback
            # because unlike the branch above, nobody chose this message.
            logger.exception(
                "Bulk invite: unexpected failure for %s in company=%s", email, company_id
            )
            results.append(
                InviteOutcome(
                    email=email,
                    role=role,
                    status="failed",
                    error="Something went wrong inviting this address.",
                )
            )
            continue

        if outcome == "invited":
            any_membership_written = True
        results.append(
            InviteOutcome(email=email, role=role, status=outcome, email_sent=fresh_invite)
        )

    # ⚠ ONCE, and only if a membership actually landed. Syncing per address
    # would push an intermediate quantity to Stripe for each one and prorate
    # every step of a change the admin made as a single action.
    if any_membership_written:
        await sync_company_seats(pool, get_settings(), company_id, reason="invite_bulk")

    return BulkInviteResponse(results=results)


# ── Team roster (ADR 0005) ──────────────────────────────────────────


class MemberOut(BaseModel):
    """One member on the Team & Permissions screen."""

    user_id: str
    email: str | None
    display_name: str | None
    role: str
    is_primary_admin: bool
    is_self: bool
    #: Sent an invitation and has never signed in. Defaults False so an older
    #: client that does not read it is unchanged.
    invite_pending: bool = False


class PendingInviteOut(BaseModel):
    """An invite to an existing account, waiting for them to accept (migration 0112).

    Not a member: no seat, no access, no row in ``company_users``. Listed apart from
    ``members`` so nothing that reads the roster can mistake one for the other.
    """

    id: str
    email: str | None
    role: str
    invited_at: str
    expires_at: str


class MembersResponse(BaseModel):
    company_id: str
    members: list[MemberOut]
    #: Invites waiting for someone who already has an account. Defaults empty so
    #: an older client that does not read it is unchanged.
    pending_invites: list[PendingInviteOut] = []


class RoleChangeRequest(BaseModel):
    role: str = Field(description="One of: admin, manager, member.")
    company_id: str | None = None


class MemberRemovedResponse(BaseModel):
    user_id: str
    status: str = "removed"
    #: Staff SMS numbers this person had CLAIMED as theirs, now revoked.
    #:
    #: ⚠ ``None`` is not zero. Zero means they had claimed none; ``None`` means
    #: the ledger could not be read, so nothing was checked. Rendering the
    #: second as the first tells an admin the offboarding is complete when it
    #: was never looked at (gotcha 57).
    sms_numbers_revoked: int | None = None
    #: Live staff numbers nobody has claimed — the ones no departure can act
    #: on, because the ledger does not know whose they are. Reported rather
    #: than guessed at: a gap the product does not show is one nobody closes.
    sms_numbers_unattributed: int | None = None


class MyRoleResponse(BaseModel):
    company_id: str
    role: str
    is_primary_admin: bool


@router.get("/me")
async def my_company_role(
    request: Request,
    company_id: str | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
) -> MyRoleResponse:
    """The caller's own role in the company (any member may call). The frontend
    uses this to gate Settings-screen visibility before it can fetch the roster,
    which is itself manager+ (ADR 0005)."""
    await connectors_limiter.check_by_user(request, user)
    target = _resolve_target_company(user, company_id)
    pool = _require_pool()
    # MEMBER minimum → any member passes; a non-member 403s. Returns the
    # normalized role (legacy 'owner' reads as admin).
    role = await require_role(pool, user.user_id, target, MEMBER)

    is_primary = False
    if role == ADMIN:
        roster = await members_repo.list_members(pool, company_id=target)
        is_primary = any(m.user_id == user.user_id and m.is_primary_admin for m in roster)

    return MyRoleResponse(company_id=target, role=role, is_primary_admin=is_primary)


@router.get("/members")
async def list_company_members(
    request: Request,
    company_id: str | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
) -> MembersResponse:
    """The team roster for the caller's company. Manager+ (members can't see
    the team-management screen — visibility gating, ADR 0005)."""
    await connectors_limiter.check_by_user(request, user)
    target = _resolve_target_company(user, company_id)
    pool = _require_pool()
    await require_role(
        pool, user.user_id, target, MANAGER, detail="Only managers or admins can view the team."
    )

    roster = await members_repo.list_members(pool, company_id=target)
    waiting = await member_invites.for_company(pool, company_id=target)
    return MembersResponse(
        company_id=target,
        pending_invites=[
            PendingInviteOut(
                id=i.id,
                email=i.email,
                role=normalize_role(i.role),
                invited_at=i.created_at.isoformat(),
                expires_at=i.expires_at.isoformat(),
            )
            for i in waiting
        ],
        members=[
            MemberOut(
                user_id=m.user_id,
                email=m.email,
                display_name=m.display_name,
                role=m.role,
                is_primary_admin=m.is_primary_admin,
                is_self=m.user_id == user.user_id,
                invite_pending=m.invite_pending,
            )
            for m in roster
        ],
    )


@router.post("/members/{target_user_id}/role")
async def change_member_role(
    body: RoleChangeRequest,
    request: Request,
    target_user_id: str = Path(...),
    user: AuthenticatedUser = Depends(get_current_user),
) -> MemberOut:
    """Change a member's role. Admin only. Guards against demoting the last
    admin so a company can never be left with zero admins."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _resolve_target_company(user, body.company_id)
    pool = _require_pool()
    await require_role(
        pool, user.user_id, company_id, ADMIN, detail="Only admins can change roles."
    )

    new_role = body.role.strip().lower()
    if new_role not in INVITABLE_ROLES:
        raise HTTPException(
            status_code=422,
            detail=f"Role must be one of: {', '.join(sorted(INVITABLE_ROLES))}.",
        )

    current = await members_repo.get_member_role(
        pool, company_id=company_id, user_id=target_user_id
    )
    if current is None:
        raise HTTPException(status_code=404, detail="That person is not a member of this company.")

    # Last-admin guard: demoting the only remaining admin would orphan the
    # company (no one could manage it again). Refuse.
    if current == ADMIN and new_role != ADMIN:
        if await members_repo.count_admins(pool, company_id=company_id) <= 1:
            raise HTTPException(
                status_code=409,
                detail="This is the company's last admin — promote someone else first.",
            )

    if new_role != current:
        await members_repo.set_member_role(
            pool, company_id=company_id, user_id=target_user_id, role=new_role
        )
        await write_activity(
            pool,
            ActivityEntry(
                account_id=None,
                company_id=company_id,
                source="manual",
                action_type="member_role_changed",
                description=f"role changed to {new_role}",
                user_id=user.user_id,
                raw={"target_user_id": target_user_id, "from": current, "to": new_role},
            ),
        )

    is_primary = False
    if new_role == ADMIN:
        roster = await members_repo.list_members(pool, company_id=company_id)
        is_primary = any(m.user_id == target_user_id and m.is_primary_admin for m in roster)

    return MemberOut(
        user_id=target_user_id,
        email=None,
        display_name=None,
        role=new_role,
        is_primary_admin=is_primary,
        is_self=target_user_id == user.user_id,
    )


@router.delete("/members/{target_user_id}")
async def remove_company_member(
    request: Request,
    target_user_id: str = Path(...),
    company_id: str | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
) -> MemberRemovedResponse:
    """Remove a member from the company. Admin only. Cannot remove the last
    admin (same guard as a demote)."""
    await connectors_limiter.check_by_user(request, user)
    target = _resolve_target_company(user, company_id)
    pool = _require_pool()
    await require_role(pool, user.user_id, target, ADMIN, detail="Only admins can remove members.")

    current = await members_repo.get_member_role(pool, company_id=target, user_id=target_user_id)
    if current is None:
        raise HTTPException(status_code=404, detail="That person is not a member of this company.")

    if current == ADMIN and await members_repo.count_admins(pool, company_id=target) <= 1:
        raise HTTPException(
            status_code=409,
            detail="This is the company's last admin — promote someone else first.",
        )

    await members_repo.remove_member(pool, company_id=target, user_id=target_user_id)
    # Somebody who has left must stop receiving the manager digest — it names
    # the top card, which is portfolio content. Nothing did this before 0096,
    # because nothing on a consent row said whose number it was.
    revoked = await revoke_for_departing_member(pool, company_id=target, user_id=target_user_id)
    unattributed = await unattributed_manager_numbers(pool, company_id=target)
    await write_activity(
        pool,
        ActivityEntry(
            account_id=None,
            company_id=target,
            source="manual",
            action_type="member_removed",
            description=f"removed member ({current})",
            user_id=user.user_id,
            raw={
                "target_user_id": target_user_id,
                "role": current,
                "sms_numbers_revoked": revoked,
                "sms_numbers_unattributed": unattributed,
            },
        ),
    )
    # Removing somebody must CREDIT them back, which is the half a per-seat
    # model gets wrong most often — nobody complains about being under-billed,
    # so an unsynced removal can sit for months.
    await sync_company_seats(pool, get_settings(), target, reason="member_removed")

    return MemberRemovedResponse(
        user_id=target_user_id,
        sms_numbers_revoked=revoked,
        sms_numbers_unattributed=unattributed,
    )


# ── Invites waiting for an existing account (migration 0112) ─────────


class MyInviteOut(BaseModel):
    """An invite waiting for the caller: who asked them to join what, as what."""

    id: str
    company_id: str
    company_name: str
    role: str
    invited_by_email: str | None
    expires_at: str


class MyInvitesResponse(BaseModel):
    invites: list[MyInviteOut]


class InviteAcceptedResponse(BaseModel):
    company_id: str
    company_name: str
    role: str


class InviteClosedResponse(BaseModel):
    id: str
    status: str


_NO_SUCH_INVITE = "That invite is no longer open. Ask the admin who sent it for a new one."


@router.get("/invites")
async def my_invites(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> MyInvitesResponse:
    """The invites waiting for the caller. Any signed-in user: they need no
    membership to see who wants them, which is the point."""
    await connectors_limiter.check_by_user(request, user)
    pool = _require_pool()
    waiting = await member_invites.for_invitee(pool, user_id=user.user_id)
    return MyInvitesResponse(
        invites=[
            MyInviteOut(
                id=i.id,
                company_id=i.company_id,
                company_name=i.company_name,
                role=normalize_role(i.role),
                invited_by_email=i.invited_by_email,
                expires_at=i.expires_at.isoformat(),
            )
            for i in waiting
        ]
    )


@router.post("/invites/{invite_id}/accept")
async def accept_invite(
    request: Request,
    invite_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
) -> InviteAcceptedResponse:
    """The invitee joins the company that invited them.

    The only route that writes an existing account's membership, and it writes the
    CALLER's own, only from an invite addressed to them (``member_invites.accept``).
    Refused (409, the invite kept) when the caller already belongs to a company
    other than their own sign-up one: one account, one company (founder directive
    2026-09-03), so accepting never leaves somebody in two workspaces with a coin
    flip deciding which one they see.
    """
    await connectors_limiter.check_by_user(request, user)
    pool = _require_pool()
    try:
        accepted = await member_invites.accept(pool, invite_id=str(invite_id), user_id=user.user_id)
    except member_invites.AcceptRefusedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"You already belong to {exc.company_name or 'another workspace'}. "
                "An account belongs to one workspace: leave that one first "
                "(Settings, Team & Permissions), then accept."
            ),
        ) from None
    if accepted is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_SUCH_INVITE)

    await write_activity(
        pool,
        ActivityEntry(
            account_id=None,
            company_id=accepted.company_id,
            source="manual",
            action_type="member_invite_accepted",
            description=f"accepted the invite as {accepted.role}",
            user_id=user.user_id,
            raw={"invite_id": str(invite_id), "role": accepted.role},
        ),
    )
    # The seat is counted now that the membership exists (billing/seats.py).
    await sync_company_seats(pool, get_settings(), accepted.company_id, reason="invite_accepted")
    return InviteAcceptedResponse(
        company_id=accepted.company_id,
        company_name=accepted.company_name,
        role=normalize_role(accepted.role),
    )


@router.post("/invites/{invite_id}/decline")
async def decline_invite(
    request: Request,
    invite_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
) -> InviteClosedResponse:
    """The invitee turns an invite down. Only their own; nothing else changes."""
    await connectors_limiter.check_by_user(request, user)
    pool = _require_pool()
    company_id = await member_invites.decline(pool, invite_id=str(invite_id), user_id=user.user_id)
    if company_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_SUCH_INVITE)
    await write_activity(
        pool,
        ActivityEntry(
            account_id=None,
            company_id=company_id,
            source="manual",
            action_type="member_invite_declined",
            description="an invitee declined the invite",
            user_id=user.user_id,
            raw={"invite_id": str(invite_id)},
        ),
    )
    return InviteClosedResponse(id=str(invite_id), status="declined")


@router.delete("/invites/{invite_id}")
async def withdraw_invite(
    request: Request,
    invite_id: UUID,
    company_id: str | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
) -> InviteClosedResponse:
    """An admin takes back an invite their company sent and nobody has answered."""
    await connectors_limiter.check_by_user(request, user)
    target = _resolve_target_company(user, company_id)
    pool = _require_pool()
    await require_role(
        pool, user.user_id, target, ADMIN, detail="Only admins can withdraw an invite."
    )
    invitee = await member_invites.withdraw(pool, invite_id=str(invite_id), company_id=target)
    if invitee is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_SUCH_INVITE)
    await write_activity(
        pool,
        ActivityEntry(
            account_id=None,
            company_id=target,
            source="manual",
            action_type="member_invite_withdrawn",
            description="withdrew an invite",
            user_id=user.user_id,
            raw={"invite_id": str(invite_id), "target_user_id": invitee},
        ),
    )
    return InviteClosedResponse(id=str(invite_id), status="withdrawn")


# ── Company knowledge profile (ADR 0006) ────────────────────────────


class CompanyKnowledgeResponse(BaseModel):
    """The standing company profile (Settings → AI & Memory). ``content_md`` is
    the auto-learned daily profile — Helixis learns the company (daily distiller
    + uploaded docs / Drive); nobody hand-types context into it (founder
    directive, #67). This surface is READ-ONLY."""

    company_id: str
    content_md: str
    generated_on: str | None
    updated_at: str | None


@router.get("/knowledge")
async def get_company_knowledge(
    request: Request,
    company_id: str | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
) -> CompanyKnowledgeResponse:
    """The company's standing knowledge profile — read, any member. ``content_md``
    is empty until the daily job has produced something (or the migration is
    applied); the UI shows a 'still learning' state."""
    await connectors_limiter.check_by_user(request, user)
    target = _resolve_target_company(user, company_id)
    pool = _require_pool()

    row = await company_knowledge_repo.load(pool, company_id=target)
    if row is None:
        return CompanyKnowledgeResponse(
            company_id=target, content_md="", generated_on=None, updated_at=None
        )
    return CompanyKnowledgeResponse(
        company_id=target,
        content_md=row.content_md,
        generated_on=row.generated_on.isoformat() if row.generated_on else None,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


__all__ = ["router"]


# ── Leaving, exporting, and deleting (items 20 / 219 / 349 / 356 / 425) ──


class LeftCompanyResponse(BaseModel):
    company_id: str


@router.post("/leave", response_model=LeftCompanyResponse)
async def leave_company(
    request: Request,
    company_id: str | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
) -> LeftCompanyResponse:
    """Remove YOURSELF from this workspace.

    Until this existed, leaving required asking an admin — so somebody who had
    changed jobs stayed on the members list, kept a seat they were being
    billed for, and kept access to a portfolio they had no business seeing.
    "Ask someone else to revoke your access" is not an access control.

    ⚠ Reuses the LAST-ADMIN guard, and that is the whole reason this is not
    just a delete. An admin who leaves an orphaned workspace strands it: no
    one can invite, promote, or cancel the subscription, and only we can fix
    it. The refusal names the fix rather than just saying no.
    """
    await connectors_limiter.check_by_user(request, user)
    target = _resolve_target_company(user, company_id)
    pool = _require_pool()

    current = await members_repo.get_member_role(pool, company_id=target, user_id=user.user_id)
    if current is None:
        raise HTTPException(status_code=404, detail="You are not a member of this workspace.")

    if current == ADMIN and await members_repo.count_admins(pool, company_id=target) <= 1:
        raise HTTPException(
            status_code=409,
            detail=(
                "You're the last admin here. Promote someone else first, or "
                "delete the workspace if you're finished with it."
            ),
        )

    await members_repo.remove_member(pool, company_id=target, user_id=user.user_id)
    # Same revocation as an admin-initiated removal. ⚠ BOTH paths, or the one
    # that gets forgotten is silently exempt — leaving is the MORE likely of
    # the two to be used by somebody who has already changed jobs, which is
    # exactly the person who should stop receiving the digest.
    revoked = await revoke_for_departing_member(pool, company_id=target, user_id=user.user_id)
    await write_activity(
        pool,
        ActivityEntry(
            account_id=None,
            company_id=target,
            source="manual",
            action_type="member_left",
            description=f"left the workspace ({current})",
            user_id=user.user_id,
            raw={"role": current, "sms_numbers_revoked": revoked},
        ),
    )
    # Credit the seat back — the half a per-seat model gets wrong most often,
    # because nobody complains about being under-billed.
    await sync_company_seats(pool, get_settings(), target, reason="member_left")
    return LeftCompanyResponse(company_id=target)


@router.get("/export")
async def export_company_data(
    request: Request,
    company_id: str | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    """Every exportable table for this workspace, as a zip of CSVs.

    Admin-only: this is the whole portfolio in one file, which is a strictly
    stronger disclosure than any single screen in the app.

    Property managers live in spreadsheets, so this answers a day-one request
    on its own — and it is also the step that has to exist before deletion can
    be offered honestly. A destructive action whose only alternative is losing
    everything is one people are right to refuse.
    """
    await connectors_limiter.check_by_user(request, user)
    target = _resolve_target_company(user, company_id)
    pool = _require_pool()
    await require_role(
        pool, user.user_id, target, ADMIN, detail="Only admins can export company data."
    )

    row = await pool.fetchrow("SELECT name FROM public.companies WHERE id = $1::uuid", target)
    name = str((row or {}).get("name") or "workspace")

    payload = await build_export(pool, target, name)
    await write_activity(
        pool,
        ActivityEntry(
            account_id=None,
            company_id=target,
            source="manual",
            action_type="company_exported",
            description="exported all company data",
            user_id=user.user_id,
            raw={"bytes": len(payload)},
        ),
    )
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{export_filename(name)}"',
            # Somebody's whole portfolio — never let a proxy hold a copy.
            "Cache-Control": "no-store",
        },
    )


class DeleteCompanyRequest(BaseModel):
    confirm_name: str = Field(min_length=1, max_length=200)
    """The workspace's exact name, typed by the caller.

    ⚠ A typed confirmation rather than an "Are you sure?" — this deletes a
    portfolio and cannot be undone, and a dialog people dismiss by reflex is
    not consent for that.
    """


class DeletedCompanyResponse(BaseModel):
    company_id: str
    deleted_rows: dict[str, int]

    #: Objects removed from Supabase Storage (uploaded documents, browser
    #: screenshots, CSV exports). Before this existed the database cascaded and
    #: every file stayed — see `storage_purge`.
    storage_objects_deleted: int = 0

    #: True only when every bucket was enumerated AND emptied. ⚠ False is not a
    #: failed deletion: the workspace IS gone either way. It means one leg of
    #: the erasure is unfinished and `storage_note` says which — silence here
    #: would be the confident false claim this codebase keeps relearning,
    #: pointed at a legal obligation.
    storage_complete: bool = True

    #: Empty when complete; otherwise names the buckets and the prefix an
    #: operator has to clear by hand.
    storage_note: str = ""

    #: Agent memories erased. Memory is not foreign-keyed to companies, so the
    #: cascade never reached it and every memory outlived the workspace.
    memory_erased: int = 0

    #: ⚠ False when the memory backend was unavailable or the erase failed —
    #: never read as "nothing to erase". The workspace is gone either way.
    memory_complete: bool = True


async def _purge_company_memory(company_id: str, requested_by: str) -> tuple[int, bool]:
    """Erase the workspace's agent memory: ``(erased, complete)``. Never raises.

    Memory rows are not foreign-keyed to ``companies``, so the cascade that
    removes everything else never reached them; only the operator script
    (``scripts/purge_company_data.py``) did. ⚠ An unavailable backend or a
    failed erase answers ``complete=False``, not zero memories, for the same
    reason the Storage sweep reports what it left.
    """
    manager = get_memory_manager()
    if manager is None or not manager.available:
        return 0, False
    try:
        erased = await manager.delete_company_memory(
            company_id, requested_by=requested_by, raise_on_error=True
        )
    except (Exception, SystemExit):  # noqa: BLE001 — reported, never blocks the delete
        logger.warning("memory erase failed for deleted company %s", company_id, exc_info=True)
        return 0, False
    return erased, True


@router.delete("")
async def delete_company(
    body: DeleteCompanyRequest,
    request: Request,
    company_id: str | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
) -> DeletedCompanyResponse:
    """Delete this workspace and everything in it. Admin only. Irreversible.

    Self-service deletion is a GDPR/CCPA obligation and, separately, the thing
    that makes "your data is yours" true rather than a slogan. Disconnecting
    Buildium already purges the mirror under ToS §18; this is the wider act —
    the workspace, its members, its settings and its billing row.

    ⚠ The order is deliberate and each step is defended:
      1. **Verify the typed name** before anything is touched.
      2. **Purge the Buildium mirror explicitly**, rather than relying on the
         cascade. §18 requires API-derived data removed when the licence ends,
         and `purge_buildium_mirror` is the audited definition of that set —
         a cascade is whatever the schema happens to say today.
      3. **Purge Supabase Storage**, because nothing else can. The cascade
         reaches 46 tables and not one uploaded file; the objects are keyed
         ``{company_id}/…`` in three buckets and outlive every row that
         recorded them. ⚠ This runs BEFORE the row delete only so the log
         carries the company name alongside it — the sweep is by prefix, so
         it would work either way. ⚠ An incomplete sweep does NOT abort the
         deletion: refusing to delete a workspace because Storage hiccuped
         turns a transient error into a customer who cannot exercise a right,
         and the objects stay findable under the same prefix, so proceeding
         is the recoverable direction. The response says what was left.
      4. **Delete the company row**, which cascades the rest.

    ⚠ The Stripe subscription is NOT cancelled here, and that is a decision
    rather than an omission. Cancelling on our side without a confirmed
    Stripe response would leave a customer deleted and still billed — the
    worst possible pairing. The response tells the caller to cancel in
    Settings → Billing first, and the UI enforces that ordering.
    """
    await connectors_limiter.check_by_user(request, user)
    target = _resolve_target_company(user, company_id)
    pool = _require_pool()
    await require_role(
        pool, user.user_id, target, ADMIN, detail="Only admins can delete the workspace."
    )

    row = await pool.fetchrow("SELECT name FROM public.companies WHERE id = $1::uuid", target)
    if row is None:
        raise HTTPException(status_code=404, detail="That workspace no longer exists.")
    name = str(row["name"] or "")

    # ⚠ Compared case-insensitively on stripped text, not byte-for-byte. The
    # point of typing the name is to prove deliberate intent, and failing
    # somebody over a trailing space turns a safety check into an obstacle
    # they will route around by pasting.
    if body.confirm_name.strip().casefold() != name.strip().casefold():
        raise HTTPException(
            status_code=400,
            detail=f'To delete this workspace, type its name exactly: "{name}".',
        )

    purged = await purge_buildium_mirror(pool, target)
    storage = await purge_company_storage(company_id=target, settings=get_settings())
    memory_erased, memory_complete = await _purge_company_memory(target, user.user_id)
    await pool.execute("DELETE FROM public.companies WHERE id = $1::uuid", target)

    logger.warning(
        "company deleted: company=%s by user=%s (purged %s mirror rows; %s; memory %s)",
        target,
        user.user_id,
        sum(purged.values()),
        storage.summary(),
        f"{memory_erased} erased"
        if memory_complete
        else "NOT erased: run scripts/purge_company_memory.py for this company",
    )
    # ⚠ No activity row: the company it would belong to no longer exists, and
    # writing one would either fail on the foreign key or resurrect the
    # company_id it was meant to erase. The application log is the record.
    return DeletedCompanyResponse(
        company_id=target,
        deleted_rows=purged,
        storage_objects_deleted=storage.deleted_total,
        storage_complete=storage.complete,
        storage_note="" if storage.complete else storage.summary(),
        memory_erased=memory_erased,
        memory_complete=memory_complete,
    )
