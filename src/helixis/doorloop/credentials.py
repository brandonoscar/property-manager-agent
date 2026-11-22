"""One company's DoorLoop credentials, loaded and decrypted.

Reads ``company_doorloop_credentials`` (migration 0109) and decrypts the API
key with ``CredentialCipher``, as Propertyware's ``credentials.py`` does.

⚠ **NO ENV FALLBACK** (gotcha 70). :func:`load_credentials` returns ``None``
when the company has no row, and nothing here ever reaches for a deployment
setting instead.

⚠ **Never logged, never in a repr.** The key is excluded from
:class:`DoorLoopCredentials`'s ``repr``; every log line here names the company
and nothing else.

⚠ **The key is header-guarded after it is decrypted.** It is sent as a bearer
token on every request, so a stored CR or LF would be header injection.
:func:`helixis.pms.auth.header_value` refuses it here, and the client's auth
refuses it again.

The row's ``base_url`` and ``last_tested_at`` are not read here: the host is a
constant in ``client.py`` (a stored value must never redirect a request that
carries the key), and the test stamp is the connect route's.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from helixis.pms.auth import header_value
from helixis.security import CredentialCipher
from helixis.settings import Settings

logger = logging.getLogger(__name__)

#: The credentials table (migration 0109; ``pms.registry`` names it too).
TABLE = "company_doorloop_credentials"

#: Every column the loader reads, named once.
_COLUMNS = "company_id, encrypted_api_key"


class DoorLoopCredentialsError(Exception):
    """This company HAS a credentials row and it cannot be used: the key does
    not decrypt (gotcha 11's shape, the data key rotated without a re-encrypt),
    or it is not safe on a header line. Never carries the key."""


@dataclass(frozen=True)
class DoorLoopCredentials:
    """What the client needs for one company. The key never prints."""

    company_id: str
    api_key: str = field(repr=False)


def _settings() -> Settings:
    # Read at call time, like ``pms.registry``'s switches.
    from helixis.settings import get_settings

    return get_settings()


async def fetch_row(pool: Any, company_id: str) -> dict[str, Any] | None:
    """The raw (still encrypted) row for one company, or ``None``."""
    row = await pool.fetchrow(
        f"SELECT {_COLUMNS} FROM public.{TABLE} WHERE company_id = $1::uuid",  # noqa: S608 — module constants
        company_id,
    )
    return dict(row) if row else None


def decrypt_row(row: dict[str, Any], app_settings: Settings) -> DoorLoopCredentials:
    """The usable credentials from one row, or :class:`DoorLoopCredentialsError`.

    Decrypted and header-checked in one expression, so no frame that can raise
    here holds the decrypted key in a named local.
    """
    company_id = str(row.get("company_id") or "")
    try:
        cipher = CredentialCipher(app_settings)
        return DoorLoopCredentials(
            company_id=company_id,
            api_key=header_value(
                cipher.decrypt(str(row["encrypted_api_key"])), what="the DoorLoop API key"
            ),
        )
    except Exception:  # noqa: BLE001 — re-raised below without the cause's text
        # ``from None``: neither a decrypt failure nor ``header_value``'s
        # refusal carries the key today, and dropping the cause keeps that
        # true whatever they become.
        raise DoorLoopCredentialsError(
            f"company {company_id}: the DoorLoop API key could not be decrypted or is not "
            "usable on a request header"
        ) from None


async def load_credentials(
    pool: Any, company_id: str, *, settings: Settings | None = None
) -> DoorLoopCredentials | None:
    """Load and decrypt one company's credentials.

    ``None`` means this company is not on DoorLoop, which callers treat as "not
    connected", never as a reason to look anywhere else. A row that exists and
    cannot be used raises :class:`DoorLoopCredentialsError` and is logged
    loudly: returning ``None`` for it would make the connector go silently dark
    for a company that is connected.
    """
    if not company_id:
        return None
    row = await fetch_row(pool, company_id)
    if row is None:
        return None
    try:
        return decrypt_row(row, settings or _settings())
    except DoorLoopCredentialsError:
        logger.error("doorloop credentials for company %s are unusable", company_id)
        raise


__all__ = [
    "TABLE",
    "DoorLoopCredentials",
    "DoorLoopCredentialsError",
    "decrypt_row",
    "fetch_row",
    "load_credentials",
]
