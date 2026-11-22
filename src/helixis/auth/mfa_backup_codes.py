"""Generating, hashing and spending two-factor backup codes.

⚠ **What a code actually does.** Supabase owns the MFA protocol and will not
accept a backup code in place of a TOTP code — `mfa.verify` challenges an
enrolled factor and nothing else. So a code here is not a second way in; it is
the way to REMOVE the factor. Spend one, 2FA switches off, and the password
alone works again. The threat model is unchanged (an attacker still needs the
password AND a code) and it matches what "backup codes" means to a person: the
way back when the phone is gone.

⚠ **The entropy claim is load-bearing.** :data:`CODE_BITS` bits of
server-generated randomness is why a fast hash is sufficient — there is no
dictionary to run and no meaningful offline search. Shorten the codes, or let
a human choose one, and that reasoning expires along with the hash choice.
Migration 0094 says the same thing from the other side.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from typing import Any

logger = logging.getLogger(__name__)

#: How many codes a generation issues. Ten is the common shape and it matters
#: less than it looks: the point is having enough that losing the paper once
#: is not fatal, not having enough to lose repeatedly.
CODE_COUNT = 10

#: Crockford-ish base32 without I/L/O/U — the characters people mis-transcribe
#: from paper, and the one place a wrong character costs somebody their account
#: recovery. `U` is dropped as well because it is the pair that most often
#: turns a code into an accidental word.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: Characters per code. 20 × log2(32) = 100 bits.
_CODE_LENGTH = 20
CODE_BITS = _CODE_LENGTH * 5

#: Rendered in groups of five with dashes — `A1B2C-3D4E5-…`. People read these
#: off paper and type them into a phone, and an unbroken 20-character string is
#: where transcription errors come from.
_GROUP = 5


def _format(raw: str) -> str:
    return "-".join(raw[i : i + _GROUP] for i in range(0, len(raw), _GROUP))


def normalise(code: str) -> str:
    """What the user typed, reduced to what was generated.

    ⚠ Dashes, spaces and case are all stripped. Somebody reading a code off
    paper types it however it looks to them, and refusing a correct code over
    a lowercase letter turns account recovery into a support ticket. This is
    the one place where being permissive costs nothing: the entropy is in the
    characters, not in their presentation.
    """
    return "".join(ch for ch in code.upper() if ch in _ALPHABET)


def generate_codes(count: int = CODE_COUNT) -> list[str]:
    """`count` fresh codes, formatted for reading aloud.

    ``secrets``, never ``random`` — the module difference is the whole
    security of this feature, and `random` is seeded predictably enough that
    codes generated near each other in time would be guessable.
    """
    return [
        _format("".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LENGTH)))
        for _ in range(count)
    ]


def hash_code(code: str, salt: str) -> str:
    """Hex SHA-256 of salt || normalised code. See the module note on why fast."""
    return hashlib.sha256(f"{salt}{normalise(code)}".encode()).hexdigest()


def new_salt() -> str:
    return secrets.token_hex(16)


async def replace_codes(conn: Any, user_id: str) -> list[str]:
    """Issue a fresh set, discarding whatever was there.

    ⚠ Deletes the OLD rows, including unspent ones. Generating a second set
    while the first still works would mean a code printed a year ago and
    forgotten in a drawer is still a live 2FA bypass — "regenerate" has to
    mean the old sheet is dead, or the button is a lie.
    """
    codes = generate_codes()
    await conn.execute(
        "delete from public.helixis_mfa_backup_codes where user_id = $1::uuid",
        user_id,
    )
    for code in codes:
        salt = new_salt()
        await conn.execute(
            "insert into public.helixis_mfa_backup_codes (user_id, code_hash, salt) "
            "values ($1::uuid, $2, $3)",
            user_id,
            hash_code(code, salt),
            salt,
        )
    logger.info("issued %d backup codes for user=%s", len(codes), user_id)
    return codes


async def remaining_count(conn: Any, user_id: str) -> int:
    """How many are still spendable."""
    value = await conn.fetchval(
        "select count(*) from public.helixis_mfa_backup_codes "
        "where user_id = $1::uuid and used_at is null",
        user_id,
    )
    return int(value or 0)


async def spend_code(conn: Any, user_id: str, code: str) -> bool:
    """Verify and consume one code. True only if it was valid AND unspent.

    ⚠ Compared with :func:`hmac.compare_digest` against every candidate, and
    the loop does NOT stop early on a match. Returning as soon as a hash
    matches makes the function's runtime depend on WHERE the code sits in the
    list — a timing signal that leaks how close a guess was. The set is ten
    rows; comparing all of them costs nothing.

    ⚠ Marking used is scoped by `used_at is null` in the UPDATE itself, so two
    simultaneous requests carrying the same code cannot both succeed. Checking
    in Python and updating afterwards is the check-then-act race this repo has
    already been bitten by (gotcha 25); here it would spend one code twice and
    disable 2FA on a request that should have been refused.
    """
    rows = await conn.fetch(
        "select id, code_hash, salt from public.helixis_mfa_backup_codes "
        "where user_id = $1::uuid and used_at is null",
        user_id,
    )
    matched: str | None = None
    for row in rows:
        if hmac.compare_digest(row["code_hash"], hash_code(code, row["salt"])):
            matched = str(row["id"])
    if matched is None:
        return False

    claimed = await conn.fetchval(
        "update public.helixis_mfa_backup_codes set used_at = now() "
        "where id = $1::uuid and used_at is null returning id",
        matched,
    )
    return claimed is not None


async def disable_all_factors(conn: Any, user_id: str) -> int:
    """Remove every MFA factor for this user. Returns how many went.

    ⚠ This is what spending a code BUYS, and it is why the route that calls it
    must verify the code first and refuse otherwise. Deleting from
    `auth.mfa_factors` is how GoTrue itself unenrols — the row is what makes a
    session need aal2 — so this genuinely switches 2FA off rather than hiding
    it from a list.
    """
    deleted = await conn.fetch(
        "delete from auth.mfa_factors where user_id = $1::uuid returning id",
        user_id,
    )
    logger.warning("2FA disabled via backup code for user=%s (%d factors)", user_id, len(deleted))
    return len(deleted)


__all__ = [
    "CODE_BITS",
    "CODE_COUNT",
    "disable_all_factors",
    "generate_codes",
    "hash_code",
    "new_salt",
    "normalise",
    "remaining_count",
    "replace_codes",
    "spend_code",
]
