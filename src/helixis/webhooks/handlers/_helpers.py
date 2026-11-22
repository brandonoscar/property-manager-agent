"""Shared helpers for v2 webhook handlers (the dispatch.py path).

Handlers receive ``(company_id, envelope_dict)`` and need a Buildium
API client + the company's account_id. This module lazily builds a
:class:`BuildiumApi` per call, decrypting the company's stored
credentials via :mod:`helixis.buildium.credentials`.

Every handler follows the same shape:

    1. Pull the entity id out of the envelope.
    2. Resolve a BuildiumApi for the company.
    3. Fetch the full entity (404 → "deleted before we got here").
    4. Upsert the local mirror / write an activity entry.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from helixis.buildium.api import BuildiumApi
from helixis.buildium.client import BuildiumClient
from helixis.buildium.credentials import load_buildium_settings_for_company

logger = logging.getLogger(__name__)


def envelope_int(envelope: dict[str, Any], key: str) -> int | None:
    """Read ``envelope[key]`` as int, or None if absent / not numeric."""
    value = envelope.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("Envelope field %s is not numeric: %r", key, value)
        return None


def envelope_account_id(envelope: dict[str, Any]) -> int:
    """Read ``AccountId`` off the envelope, or 0 if missing.

    The router has already verified the account exists, so 0 here means
    a degenerate edge case (manual re-dispatch with a malformed row);
    handlers should still handle 0 gracefully — most just pass it as
    the activity-log ``account_id`` for record-keeping.
    """
    return envelope_int(envelope, "AccountId") or 0


@asynccontextmanager
async def open_api_for_company(company_id: str):  # type: ignore[no-untyped-def]
    """Yield an open ``BuildiumApi`` for the company, or None if not configured.

    Keeps the underlying httpx client open for the duration of the
    handler call so a single handler can make multiple API calls
    without paying the connection setup cost each time.
    """
    settings = await load_buildium_settings_for_company(company_id)
    if settings is None:
        logger.warning(
            "No Buildium credentials for company %s — cannot fetch entity",
            company_id,
        )
        yield None
        return
    async with BuildiumClient(settings) as client:
        yield BuildiumApi(client)
