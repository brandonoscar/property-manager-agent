"""Which property-management system is a company connected to? One home.

ADR 0010 Decision (a) makes ``pms`` a COMPANY-level fact rather than a row-level
one, and the ``assert_one_pms_per_company`` trigger is what makes that true: a
company may hold one system's credentials row, and the database refuses a
second. So the question has exactly one answer and can be asked in one
statement. The systems are listed in ``pms.registry``; this query names each
non-Buildium credentials table, and ``test_pms_registry`` pins the two
together.

⚠ **THE TRIGGER IS WHY THIS IS A FUNCTION AND NOT A GUESS.** Without a1 a
company could hold both, this would have to return a set, and every caller
would have to decide what to do with two answers. Finding R3-7 records what
that would cost downstream: with both systems' record ids in one identity
column under ``UNIQUE (company_id, buildium_id)``, two records sharing an id
**silently overwrite each other**. The trigger is load-bearing on the identity
design, not a tidy-up.

⚠ **This query is deliberately per-company, unlike ``company_population``'s** —
it answers "which PMS is THIS company on", so it carries ``company_id`` (gotcha
47's rule; the neighbouring module carries the opposite note for the opposite
reason).
"""

from __future__ import annotations

import logging
import re
from typing import Any

import asyncpg

from helixis.pms.registry import PMS, PmsSpec

logger = logging.getLogger(__name__)

#: What a company with no credentials row at all is treated as.
#:
#: ⚠ Buildium, NOT "unknown" and NOT a refusal. A company mid-onboarding, or one
#: that disconnected, has no connection and still uses the product — and every
#: surface that reads this answer (today: the analyst's column filter) behaved
#: as Buildium before Rentvine existed. Returning "unknown" would make every
#: caller invent its own fallback, which is how one fact ends up with three
#: answers (gotchas 25/73).
DEFAULT_PMS = "buildium"

_SQL = """
SELECT CASE
           WHEN EXISTS (
               SELECT 1 FROM company_rentvine_credentials WHERE company_id = $1::uuid
           ) THEN 'rentvine'
           WHEN EXISTS (
               SELECT 1 FROM company_rentmanager_credentials WHERE company_id = $1::uuid
           ) THEN 'rentmanager'
           WHEN EXISTS (
               SELECT 1 FROM company_propertyware_credentials WHERE company_id = $1::uuid
           ) THEN 'propertyware'
           WHEN EXISTS (
               SELECT 1 FROM company_doorloop_credentials WHERE company_id = $1::uuid
           ) THEN 'doorloop'
           ELSE 'buildium'
       END AS pms
"""

#: The query before the DoorLoop credentials table existed (migration 0109).
#: ⚠ Used only while that table is MISSING, for the reason the next query
#: gives: while a credentials table does not exist, no company can be on its
#: system, so this answer is exact. Without this step, code that reached a
#: database before 0109 was applied would fall back past Rent Manager and
#: Propertyware too.
_SQL_BEFORE_DOORLOOP = """
SELECT CASE
           WHEN EXISTS (
               SELECT 1 FROM company_rentvine_credentials WHERE company_id = $1::uuid
           ) THEN 'rentvine'
           WHEN EXISTS (
               SELECT 1 FROM company_rentmanager_credentials WHERE company_id = $1::uuid
           ) THEN 'rentmanager'
           WHEN EXISTS (
               SELECT 1 FROM company_propertyware_credentials WHERE company_id = $1::uuid
           ) THEN 'propertyware'
           ELSE 'buildium'
       END AS pms
"""

#: The query before the Rent Manager and Propertyware credentials tables
#: existed. ⚠ Used only while those tables are MISSING, i.e. this code reached
#: a database the migration that creates them has not been applied to. That
#: fallback is exact, not approximate: while a credentials table does not
#: exist, no company can be on its system. Without it, deploying the code first
#: would make every lookup fail: ``company_pms`` would read a Rentvine company
#: as Buildium, and ``company_pms_strict`` would make the detector cycle skip
#: every company (CLAUDE.md rule 7: a committed migration is not an applied one).
_SQL_BEFORE_THE_NEW_TABLES = """
SELECT CASE
           WHEN EXISTS (
               SELECT 1 FROM company_rentvine_credentials WHERE company_id = $1::uuid
           ) THEN 'rentvine'
           ELSE 'buildium'
       END AS pms
"""


#: Newest first: each query names one fewer migration's credentials tables.
_QUERIES: tuple[str, ...] = (_SQL, _SQL_BEFORE_DOORLOOP, _SQL_BEFORE_THE_NEW_TABLES)


async def _fetch(conn: Any, company_id: str) -> Any:
    """The answer row, stepping back to an older query only while a newer
    migration's credentials table is absent. Any other error is raised."""
    for sql in _QUERIES[:-1]:
        try:
            return await conn.fetchrow(sql, company_id)
        except asyncpg.exceptions.UndefinedTableError as exc:
            logger.warning(
                "company_pms: %s; answering from the query before that table existed "
                "(apply the migration)",
                str(exc)[:200],
            )
    return await conn.fetchrow(_QUERIES[-1], company_id)


async def company_pms(pool: Any, company_id: str) -> str:
    """The ``pms.registry`` key of the system one company is on.

    ⚠ **Fails OPEN to :data:`DEFAULT_PMS`, and that direction is a choice.** The
    one caller today is the analyst's hidden-column filter, where being wrong
    costs a column of noise in one direction and a column of missing schema in
    the other — and a missing column is invisible to the reader while an extra
    one is merely unhelpful. Raising would take the analyst down for a Buildium
    customer over a transient catalog read, which is a real regression traded
    for a cosmetic one. A caller whose failure mode is not that shape must not
    reuse this without re-reading the sentence above (gotchas 55/91's
    fail-closed/fail-open split).
    """
    if not company_id:
        return DEFAULT_PMS
    try:
        async with pool.acquire() as conn:
            row = await _fetch(conn, company_id)
    except Exception as exc:  # noqa: BLE001 — degrade, never crash a chat turn
        logger.warning("company_pms failed for company=%s: %s", company_id, exc)
        return DEFAULT_PMS
    if row is None:
        return DEFAULT_PMS
    value = row["pms"] if not isinstance(row, dict) else row.get("pms")
    # Only a registry key is an answer. Anything else falls back like a failed
    # read, so a caller comparing against the registry never sees junk.
    return str(value) if value in _KNOWN else DEFAULT_PMS


async def company_pms_strict(pool: Any, company_id: str) -> str:
    """Same answer as :func:`company_pms`, but a failed read RAISES.

    For callers where a wrong "buildium" is the harmful direction. The
    background detector cycle is the first: treating a Rentvine company as
    Buildium runs every money rule over tables Rentvine never fills, and each
    one reports every active lease as unpaid or never charged. Skipping one
    company for one cycle costs nothing; a false claim about a tenant's rent
    reaches the Inbox.

    A company with no credentials row at all is still :data:`DEFAULT_PMS`.
    That is a fact about the company (it is not on Rentvine), not a failed
    read, and it is the population the detectors have always served.
    """
    async with pool.acquire() as conn:
        row = await _fetch(conn, company_id)
    if row is None:
        return DEFAULT_PMS
    value = row["pms"] if not isinstance(row, dict) else row.get("pms")
    # A value the SQL cannot produce is a failed read, not an answer.
    if value not in _KNOWN:
        raise ValueError(f"company_pms_strict: unrecognised pms {value!r} for {company_id}")
    return str(value)


_KNOWN = frozenset(PMS)

#: The one-PMS trigger's refusal names the system the company is already on:
#: "company <id> is already connected to <name>; one PMS per company …"
#: (`assert_one_pms_per_company`, migration 0107).
_ALREADY_CONNECTED = re.compile(r"already connected to ([^;]+);")


def connected_to(refusal: BaseException | str) -> PmsSpec | None:
    """The system the one-PMS trigger's refusal names, or None.

    The connect routes read it back so their 409 names the real system without
    a second query. ⚠ Only a name from ``pms.registry`` is accepted, so the text
    of some other error can never reach a customer as a system's name.
    """
    match = _ALREADY_CONNECTED.search(str(refusal))
    if match is None:
        return None
    name = match.group(1).strip()
    return next((found for found in PMS.values() if found.name == name), None)


def one_pms_refusal(connecting: str, refusal: BaseException | str) -> str:
    """The 409 text for a connect the one-PMS trigger refused.

    ``connecting`` is the registry key of the system being connected. When the
    refusal does not name a known system, the text says "another
    property-management system" rather than guessing one.
    """
    this = PMS[connecting].name
    other = connected_to(refusal)
    if other is None:
        return (
            "This workspace is already connected to another property-management "
            "system. Occupella supports one per workspace. Disconnect it first, "
            f"then connect {this}."
        )
    return (
        f"This workspace is already connected to {other.name}. Occupella "
        "supports one property-management system per workspace — "
        f"disconnect {other.name} first, then connect {this}."
    )


__all__ = [
    "DEFAULT_PMS",
    "company_pms",
    "company_pms_strict",
    "connected_to",
    "one_pms_refusal",
]
