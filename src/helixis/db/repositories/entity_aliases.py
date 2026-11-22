"""User-confirmed entity aliases (migration 0057) — "Mike the plumber" → vendor 4821.

The research investigation (§1.8) found free-text entity matching had no
place to remember a user's own names for people: every "Mike the plumber"
re-ran the same fuzzy disambiguation. This table stores the confirmed
mapping once; ``lookup_alias`` is checked FIRST in contact lookup (exact,
company-scoped, case-insensitive), before any fuzzy matching.

Deploy-window safe (gotcha 8): reads return None and writes report failure
without raising when the 0057 table isn't applied yet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

logger = logging.getLogger(__name__)

ALIAS_ENTITY_TYPES = ("tenant", "owner", "vendor", "property", "unit")


@dataclass
class AliasRow:
    alias: str
    entity_type: str
    entity_id: int
    label: str | None


async def lookup_alias(pool: asyncpg.Pool, *, company_id: str, alias: str) -> AliasRow | None:
    """Exact (case-insensitive) alias match for the company, or None."""
    needle = (alias or "").strip()
    if not needle:
        return None
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT alias, entity_type, entity_id, label
                  FROM helixis_entity_aliases
                 WHERE company_id = $1::uuid AND lower(alias) = lower($2)
                """,
                company_id,
                needle,
            )
        if row is None:
            return None
        # Row parsing stays INSIDE the fail-open guard: a malformed row (or a
        # test fake answering a different query's shape) degrades to no-alias
        # rather than crashing the lookup.
        return AliasRow(
            alias=row["alias"],
            entity_type=row["entity_type"],
            entity_id=row["entity_id"],
            label=row["label"],
        )
    except Exception:  # noqa: BLE001 — aliases are an enhancement, never load-bearing
        logger.debug("alias lookup failed (non-fatal)", exc_info=True)
        return None


async def upsert_alias(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    alias: str,
    entity_type: str,
    entity_id: int,
    label: str | None = None,
    created_by: str | None = None,
) -> bool:
    """Remember (or re-point) an alias. Returns False (never raises) on
    validation or storage failure — the caller phrases an honest reply."""
    needle = (alias or "").strip()
    if not needle or entity_type not in ALIAS_ENTITY_TYPES or entity_id <= 0:
        return False
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO helixis_entity_aliases
                    (company_id, alias, entity_type, entity_id, label, created_by)
                VALUES ($1::uuid, $2, $3, $4, $5, $6)
                ON CONFLICT (company_id, lower(alias))
                DO UPDATE SET entity_type = $3, entity_id = $4, label = $5,
                              created_by = $6, created_at = now()
                """,
                company_id,
                needle,
                entity_type,
                entity_id,
                label,
                created_by,
            )
        return True
    except Exception:  # noqa: BLE001
        logger.debug("alias upsert failed (non-fatal)", exc_info=True)
        return False
