#!/usr/bin/env python3
"""Populate ``company_buildium_credentials.account_id`` for existing rows.

Migration 0005 made ``account_id`` nullable, and the connector form was
simplified to drop the field. The ``/api/v1/buildium/test`` endpoint
now auto-fetches the id from Buildium's ``/administration/account``
and writes it back, but rows saved before that change still have
``account_id = NULL`` and so any inbound webhook for those companies
gets dropped by ``fetch_buildium_row_by_account_id``.

This script does the same one-shot fetch for every NULL row:

  1. SELECT every row in company_buildium_credentials WHERE account_id IS NULL.
  2. For each row, decrypt the saved client_id/client_secret.
  3. Call GET /administration/account and read ``Id``.
  4. UPDATE the row with the returned id.

Idempotent: re-running is a no-op once every row has an id. Safe to run
in a deploy hook.

Required env (loaded the same way the app does):
  HELIXIS_SUPABASE__DB_URL   postgresql://… (direct DB access)
  HELIXIS_DATA_KEY           Fernet key used to encrypt the row's secrets

Usage:
  python3 scripts/backfill_buildium_account_id.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

import asyncpg

from helixis.buildium.client import BuildiumClient
from helixis.buildium.credentials import decrypt_buildium_settings_from
from helixis.buildium.errors import BuildiumApiError
from helixis.settings import get_settings

logger = logging.getLogger("backfill_buildium_account_id")


async def _backfill_one(
    conn: asyncpg.Connection,
    row: dict[str, Any],
) -> tuple[str, int | None, str]:
    """Return ``(company_id, account_id_or_none, status)`` for one row.

    ``status`` is one of ``ok``, ``decrypt_error``, ``api_error``,
    ``shape_error``. Errors are logged but never raised — one bad row
    must not block the rest.
    """
    company_id = str(row["company_id"])

    try:
        bsettings = decrypt_buildium_settings_from(dict(row), get_settings())
    except Exception:  # noqa: BLE001 — keep going on the rest of the rows
        logger.exception("decrypt failed for company=%s; skipping", company_id)
        return company_id, None, "decrypt_error"

    try:
        async with BuildiumClient(bsettings) as client:
            account_info = await client.request("GET", "/administration/account")
    except BuildiumApiError as exc:
        logger.warning("buildium api error for company=%s: %s", company_id, exc)
        return company_id, None, "api_error"
    except Exception as exc:  # noqa: BLE001
        logger.warning("network error for company=%s: %s", company_id, exc)
        return company_id, None, "api_error"

    if not isinstance(account_info, dict) or "Id" not in account_info:
        logger.warning(
            "unexpected /administration/account shape for company=%s: %r",
            company_id,
            account_info,
        )
        return company_id, None, "shape_error"

    try:
        account_id = int(account_info["Id"])
    except (TypeError, ValueError):
        logger.warning("non-integer Id for company=%s: %r", company_id, account_info.get("Id"))
        return company_id, None, "shape_error"

    await conn.execute(
        """
        UPDATE public.company_buildium_credentials
           SET account_id = $2
         WHERE company_id = $1
        """,
        row["company_id"],
        account_id,
    )
    return company_id, account_id, "ok"


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(levelname)s: %(message)s")
    settings = get_settings()
    db_url = settings.supabase.db_url
    if not db_url:
        sys.stderr.write("backfill_buildium_account_id: HELIXIS_SUPABASE__DB_URL is not set.\n")
        return 2

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT company_id, encrypted_client_id, encrypted_client_secret,
                       client_id_last4, encrypted_webhook_secret, webhook_secret_last4,
                       account_id, base_url, last_tested_at
                  FROM public.company_buildium_credentials
                 WHERE account_id IS NULL
                """
            )
            if not rows:
                logger.info("no rows with NULL account_id — nothing to backfill")
                return 0

            logger.info("backfilling %d row(s)", len(rows))
            results: list[tuple[str, int | None, str]] = []
            for row in rows:
                results.append(await _backfill_one(conn, dict(row)))

            ok = [r for r in results if r[2] == "ok"]
            errors = [r for r in results if r[2] != "ok"]
            for company_id, account_id, status in results:
                logger.info("company=%s account_id=%s status=%s", company_id, account_id, status)
            logger.info("done: ok=%d error=%d", len(ok), len(errors))
            return 0 if not errors else 1
    finally:
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
