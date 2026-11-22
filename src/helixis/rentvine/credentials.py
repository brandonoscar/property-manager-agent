"""Per-company Rentvine credential loading + decryption. ADR 0010 Decision (e).

Single source of truth for turning a company's encrypted row in
``company_rentvine_credentials`` back into a usable :class:`RentvineSettings`,
mirroring ``buildium/credentials.py`` so the two connectors cannot drift.

⚠ **NO ENV FALLBACK, and that is the load-bearing difference from a naive
port.** ``load_rentvine_settings_for_company`` returns ``None`` when a company
has no row; it never quietly falls back to ``settings.rentvine.account_code``.
CLAUDE.md gotcha 70 is the standing record of what the fallback costs: the
Buildium write-harness fell back to env credentials pointing at a different
account and cheerfully verified the wrong books. The env fields exist for local
development and the read-only probe; a product path that reaches them is a bug.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Any

import asyncpg

from helixis.db.engine import get_pool
from helixis.rentvine.transport import base_url as rentvine_base_url
from helixis.security import CredentialCipher
from helixis.settings import RentvineSettings, Settings, get_settings

logger = logging.getLogger(__name__)

#: Every column the loader needs, named once. A ``SELECT *`` here would carry
#: whatever a later migration adds into every caller's dict.
#:
#: ⚠ ``encrypted_webhook_secret`` (migration 0102) is HERE and is deliberately
#: NOT decrypted by :func:`decrypt_rentvine_settings_from`. The API credentials
#: are what a client needs to read the account; the signing key is what ONE
#: route needs to authenticate an inbound request, and widening a settings
#: object every caller holds would put a webhook secret in the memory of every
#: sync, tool and probe that opens a client.
_COLUMNS = (
    "company_id, account_code, encrypted_api_key, api_key_last4, "
    "encrypted_api_secret, encrypted_webhook_secret, base_url, last_tested_at"
)

#: What a saved login is made of, as stored (migration 0100).
#: :func:`version_of` fingerprints exactly these.
#:
#: ⚠ NOT ``encrypted_webhook_secret`` (0102): saving or removing a signing key
#: is not a reconnect, and a sync stopped by one would lose a pass for nothing.
#: Nor ``last_tested_at`` (``/test`` stamps it, then starts a sync) or
#: ``updated_at`` (every UPDATE moves it).
_LOGIN_COLUMNS = ("account_code", "encrypted_api_key", "encrypted_api_secret")


async def fetch_rentvine_row(company_id: str) -> dict[str, Any] | None:
    """Return the raw (still-encrypted) credentials row for a company, or None."""
    pool = get_pool()
    row = await pool.fetchrow(
        f"""
        SELECT {_COLUMNS}
        FROM public.company_rentvine_credentials
        WHERE company_id = $1
        """,  # noqa: S608 — _COLUMNS is a module constant, never caller input
        company_id,
    )
    return dict(row) if row else None


async def fetch_rentvine_row_by_account_code(account_code: str) -> dict[str, Any] | None:
    """Resolve the credentials row for a Rentvine account subdomain.

    Rentvine webhook deliveries carry no company context — the account code in
    the URL is the only linkage — so this is the Phase 5 resolution path.

    ⚠ There is no uniqueness constraint on ``account_code`` today because the
    subdomain belongs to the customer's Rentvine tenancy and two Occupella
    companies should never share one. This deliberately does NOT ``LIMIT 1``
    over an ambiguous match: if two rows ever carry the same account code,
    routing a delivery to either is a cross-tenant leak, so the caller is told
    it is ambiguous instead. Same posture as ``comms_ingest`` declining a phone
    number that resolves to two companies.
    """
    if not account_code:
        return None
    pool = get_pool()
    rows = await pool.fetch(
        f"""
        SELECT {_COLUMNS}
        FROM public.company_rentvine_credentials
        WHERE account_code = $1
        """,  # noqa: S608 — _COLUMNS is a module constant, never caller input
        account_code,
    )
    if not rows:
        return None
    if len(rows) > 1:
        logger.error(
            "rentvine account_code %r resolves to %d companies — refusing to "
            "guess which one a delivery belongs to",
            account_code,
            len(rows),
        )
        return None
    return dict(rows[0])


def decrypt_rentvine_settings_from(
    row: dict[str, Any],
    app_settings: Settings,
) -> RentvineSettings:
    """Pure variant: decrypt using an explicit Settings instance.

    Reuses the global :class:`RentvineSettings` shape (concurrency, rate,
    timeout) and overrides only the fields persisted per company.
    """
    cipher = CredentialCipher(app_settings)
    account_code = str(row["account_code"])
    overrides: dict[str, Any] = {
        "account_code": account_code,
        "api_key": cipher.decrypt(row["encrypted_api_key"]),
        "api_secret": cipher.decrypt(row["encrypted_api_secret"]),
    }
    result: RentvineSettings = app_settings.rentvine.model_copy(update=overrides)
    return result


def decrypt_rentvine_settings(row: dict[str, Any]) -> RentvineSettings:
    """Build a RentvineSettings from an encrypted row using global settings."""
    return decrypt_rentvine_settings_from(row, get_settings())


def decrypt_webhook_secret(row: dict[str, Any], app_settings: Settings | None = None) -> str | None:
    """The account's webhook signing key, or ``None`` when there isn't one.

    ⚠ **Three states, and collapsing them is the bug to avoid.** No column
    value means the customer has not wired any webhooks — ordinary, and the
    product works because polling is primary. A value that will not decrypt
    means gotcha 11's shape (the data key rotated without a re-encrypt) and is
    logged loudly. Both return ``None``, because the receiver's answer is the
    same — refuse — and the log is what tells them apart for a human.
    """
    blob = row.get("encrypted_webhook_secret")
    if not blob:
        return None
    try:
        return CredentialCipher(app_settings or get_settings()).decrypt(blob)
    except Exception:  # noqa: BLE001 — a decrypt failure must not 500 the receiver
        logger.exception(
            "Failed to decrypt the Rentvine webhook secret for company %s — every "
            "delivery will be refused until this is resolved",
            row.get("company_id"),
        )
        return None


def resolved_base_url(settings: RentvineSettings) -> str:
    """The account's API root.

    ⚠ Derived from ``account_code`` through ``transport.base_url`` rather than
    read from the stored ``base_url`` column, so the template lives in exactly
    one place (gotcha 25). The column exists for parity with Buildium's row and
    as an escape hatch if Rentvine ever serves an account from somewhere else;
    nothing reads it today, and this docstring is the reason why.
    """
    return rentvine_base_url(settings.account_code)


async def load_rentvine_settings_for_company(
    company_id: str,
) -> RentvineSettings | None:
    """Load + decrypt a company's Rentvine credentials in one call.

    Returns ``None`` when the company has not connected Rentvine — which
    callers must treat as "this company is not a Rentvine customer", not as an
    error, and **never** as a reason to fall back to env credentials.
    """
    if not company_id:
        return None
    row = await fetch_rentvine_row(company_id)
    if row is None:
        return None
    try:
        return decrypt_rentvine_settings_from(row, get_settings())
    except Exception:  # noqa: BLE001 — a decrypt failure must not crash a turn
        # Alarming rather than benign: this is gotcha 11's shape (the data key
        # rotated without a re-encrypt) or row corruption. Both return None,
        # which is indistinguishable from "not connected" at the call site, so
        # log loudly here or the company's connector goes silently dark.
        logger.exception("Failed to decrypt Rentvine credentials for company %s", company_id)
        return None


def version_of(row: Mapping[str, Any]) -> str:
    """A short fingerprint of one saved login: its stored columns
    (:data:`_LOGIN_COLUMNS`), ciphertext only, so it names no value.

    Each save encrypts afresh, so saving again is a new version even with the
    same values, which is what a reconnect means. Read through
    :func:`saved_version` by ``sync.sync_company``, which stops a pass whose
    login was removed or replaced while it ran.
    """
    material = json.dumps([row[column] for column in _LOGIN_COLUMNS], default=str)
    return hashlib.sha256(material.encode()).hexdigest()[:16]


async def saved_version(pool: asyncpg.Pool, company_id: str) -> str | None:
    """The :func:`version_of` of the login saved for ``company_id`` now, or
    ``None`` when none is saved. One primary-key read of the login columns
    only: the webhook key and the rest of the row are never fetched."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {', '.join(_LOGIN_COLUMNS)} "  # noqa: S608 — module constants, never caller input
            "FROM public.company_rentvine_credentials WHERE company_id = $1",
            company_id,
        )
    return version_of(row) if row is not None else None


__all__ = [
    "decrypt_rentvine_settings",
    "decrypt_webhook_secret",
    "decrypt_rentvine_settings_from",
    "fetch_rentvine_row",
    "fetch_rentvine_row_by_account_code",
    "load_rentvine_settings_for_company",
    "resolved_base_url",
    "saved_version",
    "version_of",
]
