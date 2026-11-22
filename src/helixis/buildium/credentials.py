"""Per-company Buildium credential loading + decryption.

Single source of truth for turning a company's encrypted Buildium row in
``company_buildium_credentials`` back into a usable :class:`BuildiumSettings`.
Both the connector REST routes (``api/routes/buildium.py``) and the agent
tools (``agents/buildium_tools.py``) load credentials through this helper
so they can't drift apart.
"""

from __future__ import annotations

import logging
from typing import Any

from helixis.db.engine import get_pool
from helixis.observability.sentry_setup import capture_swallowed
from helixis.security import CredentialCipher
from helixis.settings import BuildiumSettings, Settings, get_settings

logger = logging.getLogger(__name__)

#: Buildium's two API hosts. The connect route stores one of these, chosen by
#: the card's environment toggle (``api/routes/buildium._resolve_base_url``),
#: and nothing else may ever be a Buildium client's host.
SANDBOX_BASE_URL = "https://apisandbox.buildium.com"
PRODUCTION_BASE_URL = "https://api.buildium.com"
BUILDIUM_BASE_URLS = frozenset({SANDBOX_BASE_URL, PRODUCTION_BASE_URL})


class BuildiumHostRefusedError(ValueError):
    """A credentials row names a host that is not Buildium's.

    ⚠ The row's ``base_url`` becomes the host the client sends the company's
    client id and secret to. Until migration 0110 any company member could
    rewrite the row through PostgREST, and the API's admin check never saw it
    (gotcha 212). Refused here, before anything is decrypted, so a row edited
    some other way still sends nothing. Saving the keys again on the Buildium
    card writes one of :data:`BUILDIUM_BASE_URLS` back.
    """


async def fetch_buildium_row(company_id: str) -> dict[str, Any] | None:
    """Return the raw (still-encrypted) credentials row for a company, or None."""
    pool = get_pool()
    row = await pool.fetchrow(
        """
        SELECT
            company_id, encrypted_client_id, encrypted_client_secret,
            client_id_last4, encrypted_webhook_secret, webhook_secret_last4,
            account_id, base_url, last_tested_at
        FROM public.company_buildium_credentials
        WHERE company_id = $1
        """,
        company_id,
    )
    return dict(row) if row else None


async def fetch_buildium_row_by_account_id(account_id: int) -> dict[str, Any] | None:
    """Resolve the credentials row for the given Buildium AccountId.

    Webhook payloads carry ``AccountId`` in the envelope. We use that to
    pick which company's stored secret to verify against. Returns None
    when no company has registered this AccountId yet — in that case
    the webhook handler logs and returns 200 (not 401, which would
    burn Buildium's retry budget).
    """
    pool = get_pool()
    row = await pool.fetchrow(
        """
        SELECT
            company_id, encrypted_client_id, encrypted_client_secret,
            client_id_last4, encrypted_webhook_secret, webhook_secret_last4,
            account_id, base_url, last_tested_at
        FROM public.company_buildium_credentials
        WHERE account_id = $1
        LIMIT 1
        """,
        account_id,
    )
    return dict(row) if row else None


async def load_webhook_secret_by_account_id(account_id: int) -> tuple[str, str] | None:
    """Return ``(company_id, plaintext_webhook_secret)`` for an AccountId.

    Returns None if the company isn't found or hasn't pasted in their
    Buildium signing secret yet.
    """
    row = await fetch_buildium_row_by_account_id(account_id)
    if row is None or not row.get("encrypted_webhook_secret"):
        return None
    cipher = CredentialCipher(get_settings())
    try:
        plaintext = cipher.decrypt(row["encrypted_webhook_secret"])
    except Exception as exc:  # noqa: BLE001
        # A decrypt failure here is ALARMING (data-key rotation without
        # re-encrypt — gotcha 11 — or row corruption), NOT the benign
        # unknown-account case the caller otherwise treats a None as. Both
        # return None and drop the webhook with a 200, so without an explicit
        # Sentry signal a company's entire webhook feed goes silently dark.
        # Fire an alert so the drop is VISIBLE (2026-07 release audit).
        logger.exception(
            "Failed to decrypt webhook secret for account_id %s",
            account_id,
        )
        capture_swallowed(exc, op="buildium.webhook_secret.decrypt")
        return None
    return str(row["company_id"]), plaintext


def decrypt_buildium_settings_from(
    row: dict[str, Any],
    app_settings: Settings,
) -> BuildiumSettings:
    """Pure variant: decrypt using an explicit Settings instance.

    Used by the routes layer (which the test suite patches via its own
    ``get_settings`` import) and by :func:`decrypt_buildium_settings`.
    """
    base_url = row["base_url"]
    if base_url not in BUILDIUM_BASE_URLS:
        raise BuildiumHostRefusedError(
            "The saved Buildium address is not one of Buildium's own, so nothing was "
            "sent. Save the Buildium keys again on the Connectors page to fix it."
        )
    cipher = CredentialCipher(app_settings)
    base = app_settings.buildium
    overrides: dict[str, Any] = {
        "client_id": cipher.decrypt(row["encrypted_client_id"]),
        "client_secret": cipher.decrypt(row["encrypted_client_secret"]),
        "base_url": base_url,
    }
    if row.get("account_id") is not None:
        overrides["account_id"] = row["account_id"]
    result: BuildiumSettings = base.model_copy(update=overrides)
    return result


def decrypt_buildium_settings(row: dict[str, Any]) -> BuildiumSettings:
    """Build a BuildiumSettings from an encrypted row using global settings.

    Reuses the global :class:`BuildiumSettings` shape (concurrency, freshness,
    timeouts) and overrides only the fields we persist per-company. The
    ``account_id`` column is honored when the row carries one (pre-0005
    rows still have it) so the global default is preserved otherwise; the
    BuildiumClient itself doesn't use account_id during auth — it only
    matters to webhook signature verification, where webhooks carry their
    own account_id in the payload.
    """
    return decrypt_buildium_settings_from(row, get_settings())


async def load_buildium_settings_for_company(
    company_id: str,
) -> BuildiumSettings | None:
    """Load + decrypt the company's Buildium credentials in one call.

    Returns None when the company has not connected Buildium yet. Callers
    should treat that as a user-facing "not configured" condition rather
    than an error.
    """
    if not company_id:
        return None
    row = await fetch_buildium_row(company_id)
    if row is None:
        return None
    try:
        return decrypt_buildium_settings_from(row, get_settings())
    except BuildiumHostRefusedError as exc:
        # Not a decrypt failure: the row names a host that is not Buildium's,
        # which only a direct edit can do. Loud, because it is either that or a
        # bug in the connect route.
        logger.error("Buildium credentials for company %s name a foreign host", company_id)
        capture_swallowed(exc, op="buildium.credentials.host_refused")
        return None
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to decrypt Buildium credentials for company %s",
            company_id,
        )
        return None
