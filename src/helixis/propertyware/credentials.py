"""One company's Propertyware credentials, loaded and decrypted.

Reads ``company_propertyware_credentials`` (migration 0107) and decrypts the
client id and secret with ``CredentialCipher``, the same helper Rentvine's
``credentials.py`` uses.

⚠ **NO ENV FALLBACK** (gotcha 70). :func:`load_credentials` returns ``None``
when the company has no row, and nothing here ever reaches for a deployment
setting instead: an env fallback once pointed the Buildium write harness at a
different account, and it verified the wrong books.

⚠ **Never logged, never in a repr.** The client id and secret are excluded
from :class:`PropertywareCredentials`'s ``repr``, so a settings object in a
log line, an exception or an error report shows neither. Every log line here
names the company and nothing else.

⚠ **The system id is header-guarded before it can become a header.** It is
plaintext (it identifies the account, like Rentvine's account code), and it is
sent as ``x-propertyware-system-id`` on every request, so a stored CR or LF
would be header injection. :func:`helixis.pms.auth.header_value` refuses it
before anything is decrypted.

The row's ``base_url`` and ``last_tested_at`` are not read here: the host is
a constant in ``client.py`` (a stored value must never redirect a request
that carries the secret), and the test stamp is the connect route's. Its
``updated_at`` is (:func:`load_connection`): the sync reads everything whole
when the connection changed after its last full read.

``capabilities`` holds one thing (migration 0107, design "built correctly"):
the writes Propertyware refused with a 403, and when (:func:`refused_at`,
:func:`remember_refused`; the rule itself is ``pms.refusals``, shared with
Rent Manager). The write tools read it before showing a card, so an attempt
within :data:`REFUSAL_REMEMBERED` of a refusal explains instead of trying;
saving the keys again clears it (``api/routes/propertyware.py``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from helixis.pms import refusals
from helixis.pms.auth import header_value
from helixis.pms.refusals import REFUSAL_REMEMBERED, REFUSED_KEY, still_refused
from helixis.security import CredentialCipher
from helixis.settings import Settings

logger = logging.getLogger(__name__)

#: The credentials table (migration 0107; ``pms.registry`` names it too).
TABLE = "company_propertyware_credentials"

#: Every column the loader reads, named once.
_COLUMNS = "company_id, system_id, encrypted_client_id, encrypted_client_secret, updated_at"


class PropertywareCredentialsError(Exception):
    """This company HAS a credentials row and it cannot be used: the system id
    is not header-safe, or the secrets do not decrypt (gotcha 11's shape, the
    data key rotated without a re-encrypt). Never carries a secret."""


@dataclass(frozen=True)
class PropertywareCredentials:
    """What the client needs for one company. The secrets never print."""

    company_id: str
    #: The organization id, sent as ``x-propertyware-system-id``. Not secret.
    system_id: str
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)


@dataclass(frozen=True)
class PropertywareConnection:
    """One company's usable credentials, and when its row last changed."""

    credentials: PropertywareCredentials
    #: The row's ``updated_at``, moved by the database on every change to it:
    #: keys saved, a passed test stamped, a refused write remembered. ``None``
    #: when it is not a time with a zone, which the sync reads as "may have
    #: changed".
    changed_at: datetime | None


def _settings() -> Settings:
    # Read at call time, like ``pms.registry``'s switches, so a redeploy or a
    # test is never cached away.
    from helixis.settings import get_settings

    return get_settings()


async def fetch_row(pool: Any, company_id: str) -> dict[str, Any] | None:
    """The raw (still encrypted) row for one company, or ``None``."""
    row = await pool.fetchrow(
        f"SELECT {_COLUMNS} FROM public.{TABLE} WHERE company_id = $1::uuid",  # noqa: S608 — module constants
        company_id,
    )
    return dict(row) if row else None


def decrypt_row(row: dict[str, Any], app_settings: Settings) -> PropertywareCredentials:
    """The usable credentials from one row, or :class:`PropertywareCredentialsError`.

    The system id is checked BEFORE anything is decrypted, and the two secrets
    are decrypted straight into the result, so no frame that can raise here
    ever holds a decrypted secret in a named local.
    """
    company_id = str(row.get("company_id") or "")
    try:
        system_id = header_value(str(row.get("system_id") or ""), what="the Propertyware system id")
    except ValueError as exc:
        raise PropertywareCredentialsError(
            f"company {company_id}: the stored Propertyware system id is empty or contains "
            "a control character; reconnect Propertyware with the organization id"
        ) from exc
    try:
        cipher = CredentialCipher(app_settings)
        return PropertywareCredentials(
            company_id=company_id,
            system_id=system_id,
            client_id=cipher.decrypt(str(row["encrypted_client_id"])),
            client_secret=cipher.decrypt(str(row["encrypted_client_secret"])),
        )
    except Exception:  # noqa: BLE001 — re-raised below without the cause's text
        # ``from None``: the cause (a missing data key, a bad token) carries no
        # secret today, and dropping it keeps that true whatever it becomes.
        raise PropertywareCredentialsError(
            f"company {company_id}: the Propertyware credentials could not be decrypted"
        ) from None


async def load_connection(
    pool: Any, company_id: str, *, settings: Settings | None = None
) -> PropertywareConnection | None:
    """Load and decrypt one company's credentials, with when the row last
    changed, from ONE read of the row.

    ``None`` means this company is not on Propertyware, which callers treat as
    "not connected", never as a reason to look anywhere else. A row that
    exists and cannot be used raises :class:`PropertywareCredentialsError`
    and is logged loudly: returning ``None`` for it would make the connector
    go silently dark for a company that is connected.
    """
    if not company_id:
        return None
    row = await fetch_row(pool, company_id)
    if row is None:
        return None
    try:
        usable = decrypt_row(row, settings or _settings())
    except PropertywareCredentialsError:
        logger.error("propertyware credentials for company %s are unusable", company_id)
        raise
    changed = row.get("updated_at")
    return PropertywareConnection(
        credentials=usable,
        changed_at=changed if isinstance(changed, datetime) and changed.tzinfo else None,
    )


async def load_credentials(
    pool: Any, company_id: str, *, settings: Settings | None = None
) -> PropertywareCredentials | None:
    """Load and decrypt one company's credentials (:func:`load_connection`
    without the change time)."""
    connection = await load_connection(pool, company_id, settings=settings)
    return connection.credentials if connection is not None else None


async def refused_at(pool: Any, company_id: str, operation: str) -> datetime | None:
    """When Propertyware last refused ``operation`` (``"POST /workorders"``)
    for this company with a 403, or ``None`` (:func:`helixis.pms.refusals.refused_at`)."""
    return await refusals.refused_at(pool, TABLE, company_id, operation)


async def remember_refused(pool: Any, company_id: str, operation: str) -> None:
    """Record that Propertyware refused ``operation`` for this company (a 403)."""
    await refusals.remember_refused(pool, TABLE, company_id, operation)


__all__ = [
    "REFUSAL_REMEMBERED",
    "REFUSED_KEY",
    "TABLE",
    "PropertywareConnection",
    "PropertywareCredentials",
    "PropertywareCredentialsError",
    "decrypt_row",
    "fetch_row",
    "load_connection",
    "load_credentials",
    "refused_at",
    "remember_refused",
    "still_refused",
]
