"""Load and decrypt ONE company's Rent Manager credentials.

The row lives in ``company_rentmanager_credentials`` (migration 0107):
``corp_id`` and ``location_id`` in plaintext (they are in every request), the
username, password and optional partner token as Fernet ciphertext, decrypted
with the same ``CredentialCipher`` Rentvine's loader uses.

⚠ NO ENV FALLBACK (gotcha 70). A company without a row is not a Rent Manager
customer; nothing here, or anywhere in this package, reads credentials from
settings or the environment. A harness that fell back to env credentials once
verified the wrong account's books.

⚠ NEVER LOGGED, NEVER IN A REPR (terms rule 8). :class:`RentManagerCredentials`
leaves its three secrets out of its repr, and every error raised here names
the problem without the value.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from helixis import settings as app_settings_module
from helixis.pms.auth import header_value, host_label
from helixis.security import CredentialCipher
from helixis.settings import Settings

logger = logging.getLogger(__name__)

TABLE = "company_rentmanager_credentials"

#: The columns the loader reads, named once (never ``SELECT *``: a later
#: column must not ride into every caller's hands).
_COLUMNS = (
    "company_id, corp_id, location_id, encrypted_username, encrypted_password, "
    "encrypted_partner_token"
)

#: What a login is made of, as stored. :func:`version_of` fingerprints them.
_LOGIN_COLUMNS = (
    "corp_id",
    "location_id",
    "encrypted_username",
    "encrypted_password",
    "encrypted_partner_token",
)


class CredentialsUnusableError(Exception):
    """A row exists and cannot be used: it does not decrypt, or a stored value
    fails its guard. The message never carries a credential."""


@dataclass(frozen=True)
class RentManagerCredentials:
    """One company's decrypted Rent Manager credentials."""

    company_id: str
    #: The subdomain, already through the host-label guard.
    corp_id: str
    #: The one location this workspace reads (design R2).
    location_id: int
    username: str = field(repr=False)
    password: str = field(repr=False)
    #: Only if Rent Manager's partner program issued one (design R1).
    partner_token: str | None = field(default=None, repr=False)
    #: Which saved credential set these are (:func:`version_of`). The session
    #: token is cached under it, so a re-save is a fresh login in every process.
    version: str = ""

    def secrets(self) -> tuple[str, ...]:
        """Every value to mask wherever an error is shown or logged."""
        return tuple(s for s in (self.username, self.password, self.partner_token) if s)


async def fetch_row(pool: Any, company_id: str) -> dict[str, Any] | None:
    """The still-encrypted row for ONE company, or ``None``."""
    row = await pool.fetchrow(
        f"SELECT {_COLUMNS} FROM public.{TABLE} WHERE company_id = $1::uuid",  # noqa: S608 — module constants
        company_id,
    )
    return dict(row) if row else None


def version_of(row: Mapping[str, Any]) -> str:
    """A fingerprint of one saved credential set: every column the login is
    made of, as stored. Each save encrypts afresh, so saving again is a new
    version even with the same values, which is what a reconnect means.

    ⚠ Why it exists: the session token (and a second 401's "broken" mark) is
    cached per process (``pms.auth``). The route that saves new credentials
    runs in the web process and cannot reach the worker's cache, so a key
    without the version would keep the worker on the old login's token, or
    refusing as broken, until it restarted. Ciphertext only: nothing secret
    is hashed, and the fingerprint names no value.
    """
    material = json.dumps([row.get(column) for column in _LOGIN_COLUMNS], default=str)
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _location(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CredentialsUnusableError("the stored Rent Manager location is not a location id")
    return int(value)


def decrypt_row(
    row: dict[str, Any], app_settings: Settings, *, company_id: str
) -> RentManagerCredentials:
    """Pure: the decrypted credentials for ``company_id`` (the company the row
    was read for), or :class:`CredentialsUnusableError`."""
    try:
        corp_id = host_label(str(row.get("corp_id") or ""), what="the Rent Manager corp id")
    except ValueError as exc:
        raise CredentialsUnusableError(str(exc)) from None
    location_id = _location(row.get("location_id"))
    try:
        cipher = CredentialCipher(app_settings)
        username = cipher.decrypt(row["encrypted_username"])
        password = cipher.decrypt(row["encrypted_password"])
        stored_partner = row.get("encrypted_partner_token")
        partner = cipher.decrypt(stored_partner) if stored_partner else None
    except Exception:  # noqa: BLE001 — the reason, never the value
        # `from None`: the cipher's own exception may quote what it was given.
        raise CredentialsUnusableError(
            "the stored Rent Manager credentials do not decrypt (was the data key rotated?)"
        ) from None
    if not username or not password:
        raise CredentialsUnusableError("the stored Rent Manager username or password is empty")
    if partner is not None:
        # It is sent as a header (client.request_headers): no line breaks.
        try:
            header_value(partner, what="the partner token")
        except ValueError:
            raise CredentialsUnusableError(
                "the stored Rent Manager partner token is not usable"
            ) from None
    return RentManagerCredentials(
        company_id=company_id,
        corp_id=corp_id,
        location_id=location_id,
        username=username,
        password=password,
        partner_token=partner,
        version=version_of(row),
    )


async def load_credentials(
    pool: Any, company_id: str, *, app_settings: Settings | None = None
) -> RentManagerCredentials | None:
    """This company's credentials, ``None`` when it has no row, or
    :class:`CredentialsUnusableError` when the row cannot be used (logged, without
    the values: gotcha 11's shape, the data key rotated without a re-encrypt,
    would otherwise go silently dark)."""
    if not company_id:
        return None
    row = await fetch_row(pool, company_id)
    if row is None:
        return None
    try:
        return decrypt_row(
            row, app_settings or app_settings_module.get_settings(), company_id=company_id
        )
    except CredentialsUnusableError as exc:
        logger.error("rentmanager credentials for company %s are unusable: %s", company_id, exc)
        raise


__all__ = [
    "TABLE",
    "CredentialsUnusableError",
    "RentManagerCredentials",
    "decrypt_row",
    "fetch_row",
    "load_credentials",
    "version_of",
]
