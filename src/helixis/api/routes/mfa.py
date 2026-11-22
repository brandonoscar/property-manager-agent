"""Two-factor backup codes: issue, count, and spend.

Supabase owns the TOTP protocol; it does not issue recovery codes and will
not accept one in place of a TOTP code (`mfa.verify` challenges an enrolled
factor and nothing else). So these codes are not a second way IN — they are
the way to take the factor OFF. Spend one and 2FA switches off, and the
password alone works again. See `helixis.auth.mfa_backup_codes` and migration
0094, which state the same thing from the storage side.

⚠ **WHAT PROTECTS REDEMPTION, stated plainly because the obvious reading is
wrong.** These routes require a valid session, and at the two-factor gate the
caller HAS one: `signInWithPassword` succeeds at aal1 even with a factor
enrolled (measured — that is also why `TwoFactorGate` has to exist as a
client-side gate at all). So a redemption carries two independent proofs: a
token that could only be obtained with the password, and a code that could
only come from the sheet. That is genuinely two factors, and it is the reason
this endpoint does not — and must not — ask for the password again.

⚠ **THE RESIDUAL, NOT PAPERED OVER (gotcha 84).** A session that is ALREADY
past the second factor can call `POST` to mint codes and then spend one, and
so remove the factor without re-entering the password. Our backend cannot
close that: Supabase owns passwords, so no server-side proof of one is
available here, and the freshness signals that look like proof are not —
supabase-js refreshes access tokens on a timer, so a recent `iat` says the
session refreshed, not that a human typed anything. The client re-verifies
the password before calling `POST`, which is the same guard `changePassword`,
`changeEmail` and `disableTwoFactor` already ship with, and it is a UX guard
rather than enforcement. The exposure requires an unlocked, fully
authenticated machine — where the attacker can already read every lease and
every resident's details — so what it actually grants is PERSISTENCE. That is
worth closing with a real re-authentication step (Supabase's `aal`/`amr`
claims are the right signal) and is deliberately not closed here rather than
closed with a check this container cannot verify.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.auth.mfa_backup_codes import (
    CODE_COUNT,
    disable_all_factors,
    normalise,
    remaining_count,
    replace_codes,
    spend_code,
)
from helixis.db.engine import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/mfa", tags=["auth"])


def _require_pool():  # noqa: ANN202 - asyncpg Pool, not imported for typing
    pool = get_pool()
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database pool not initialised.",
        )
    return pool


class BackupCodeStatus(BaseModel):
    """How many codes are left. NEVER the codes themselves."""

    remaining: int
    #: How many a fresh generation issues, so the UI can say "3 of 10 left"
    #: without hardcoding a number that would drift from the generator.
    issued_per_batch: int = CODE_COUNT


class BackupCodes(BaseModel):
    """The one and only time the plaintext codes exist outside a person's
    hands. They are hashed at rest and cannot be recovered — regenerating is
    the only way back, and that invalidates the old sheet."""

    codes: list[str]


class RedeemRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class RedeemResult(BaseModel):
    #: How many factors were removed. Zero is a real outcome — a code spent
    #: against an account whose factor was already gone — and is reported
    #: rather than treated as failure, because the code IS spent either way.
    factors_removed: int


@router.get("/backup-codes", response_model=BackupCodeStatus)
async def backup_code_status(
    user: AuthenticatedUser = Depends(get_current_user),
) -> BackupCodeStatus:
    """How many unspent codes this user has.

    Exists so Settings can say "2 of 10 left" — the number is the whole point
    of keeping spent rows rather than deleting them. Somebody down to their
    last code needs to be told before they are down to none, because at zero
    with a lost phone the only route back is a support ticket.
    """
    pool = _require_pool()
    try:
        async with pool.acquire() as conn:
            remaining = await remaining_count(conn, user.user_id)
    except Exception:
        logger.exception("Backup-code count failed for user=%s", user.user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Couldn't read your backup codes — see server logs.",
        )
    return BackupCodeStatus(remaining=remaining)


@router.post("/backup-codes", response_model=BackupCodes)
async def generate_backup_codes(
    user: AuthenticatedUser = Depends(get_current_user),
) -> BackupCodes:
    """Issue a fresh set, discarding whatever was there.

    ⚠ Regenerating KILLS the old sheet, including unspent codes. That is the
    point rather than a side effect: a code printed a year ago and forgotten
    in a drawer would otherwise stay a live way to strip 2FA off the account
    forever. A "regenerate" that leaves the old ones working is a lie.

    ⚠ The response body is the only time these strings exist in a form
    anybody can read. There is no second GET that returns them — see the
    module note on why the client must make the user save them here.
    """
    pool = _require_pool()
    try:
        async with pool.acquire() as conn:
            codes = await replace_codes(conn, user.user_id)
    except Exception:
        logger.exception("Backup-code generation failed for user=%s", user.user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Couldn't create backup codes — see server logs.",
        )
    return BackupCodes(codes=codes)


@router.post("/backup-codes/redeem", response_model=RedeemResult)
async def redeem_backup_code(
    body: RedeemRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> RedeemResult:
    """Spend one code and turn 2FA off. The lost-phone path.

    ⚠ ORDER IS LOAD-BEARING: spend first, then remove the factors. Spending
    is the atomic claim (`used_at is null` lives in the UPDATE itself), so a
    crash between the two leaves a spent code and a still-enrolled factor —
    the user retries with another code and gets in. The other order leaves
    2FA off with the code still live, which is a permanent bypass sitting on
    a piece of paper.

    ⚠ A wrong code is a 400 with the SAME message whether the user has ten
    codes or none. "You have no backup codes" told to an unauthenticated-ish
    caller is a hint about which accounts are worth attacking; the honest
    version of that information belongs in Settings, behind a session that
    has already cleared 2FA.
    """
    pool = _require_pool()

    # Reject an empty-after-normalisation code before touching the database.
    # `normalise` strips everything outside the alphabet, so "----" and " "
    # arrive here as "" — which would otherwise hash to a real value and be
    # compared against every row for nothing.
    if not normalise(body.code):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That backup code isn't valid. Check it and try again.",
        )

    try:
        async with pool.acquire() as conn:
            spent = await spend_code(conn, user.user_id, body.code)
            if not spent:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="That backup code isn't valid. Check it and try again.",
                )
            removed = await disable_all_factors(conn, user.user_id)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Backup-code redemption failed for user=%s", user.user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Couldn't use that backup code — see server logs.",
        )

    # WARNING, not info: somebody just removed the second factor from an
    # account using a piece of paper. If that was not the account owner, this
    # line is the only trace of it.
    logger.warning(
        "Backup code redeemed for user=%s — %d factor(s) removed",
        user.user_id,
        removed,
    )
    return RedeemResult(factors_removed=removed)


__all__ = ["router"]
