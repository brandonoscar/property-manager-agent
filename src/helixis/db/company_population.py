"""Which companies does a background sweep visit? One home for the answer.

Five cycles each derived their own company population from a different query
and nobody could see that they disagreed:

* ``reminders/sweep.py``      — ``company_buildium_credentials WHERE account_id IS NOT NULL``
* ``detectors/runner.py``     — ``helixis_leases UNION helixis_bills``
* ``reconciliation/analyst.py`` — the same union, restated
* ``mirror_sync`` / ``company_knowledge`` — their own again

The disagreement is not theoretical. Live 2026-08-29: **A test
workspace** has mirrored books but **no credentials row**, so it
is in the detector population and NOT the reminder one — while holding three
open reminder cards, one of them from a `turnover` producer that is not in
``PRODUCERS`` at all. ``clear_unknown_producers`` exists precisely so that
retiring a producer cannot strand its cards, and it is per-company, so it can
never reach that company. The guarantee is false at exactly the boundary that
matters.

That boundary is also the **Buildium ToS §18 purge path** — `helixis_reminders`
IS in ``PURGE_TABLES``, so those rows reached a company with no credentials by
a route that bypassed the purge.

⚠ **Every query here is deliberately GLOBAL** (gotcha 47's required note): a
company population is the one thing that cannot be company-scoped, since its
whole job is to decide which companies exist for a given cycle. They read
`company_id` and nothing else.

⚠ **What a company with a mirror but no credentials MEANS is a data-model
question, and this module does not answer it.** It could be a disconnect whose
purge did not complete, or a seeded fixture, or a company mid-onboarding. The
populations are named for what they SELECT, not for what they imply, so the
next reader picks one on purpose instead of copying whichever query was
nearest.
"""

from __future__ import annotations

import asyncpg

#: Companies with live Buildium credentials — the population that can actually
#: CALL Buildium. Producing a card for anyone outside it would fail on the
#: first API call, so this stays the right population for card production.
_WITH_CREDENTIALS = """
SELECT DISTINCT company_id
  FROM public.company_buildium_credentials
 WHERE account_id IS NOT NULL
"""

#: Companies with live Rentvine credentials — the population the nightly
#: Rentvine mirror refresh visits.
#:
#: ⚠ **No ``IS NOT NULL`` filter, and the asymmetry with the Buildium query
#: above is deliberate rather than an omission.** Buildium's needs one because
#: ``account_id`` is backfilled by ``POST /buildium/test`` AFTER the row is
#: written, so a row can exist that cannot yet be keyed for webhook routing.
#: Rentvine's routing key is ``account_code``, which migration 0100 declares
#: ``not null`` on the same INSERT — so a row that exists is already usable and
#: a filter here would only be a copy of a constraint the database enforces.
#:
#: ⚠ **Disjoint from the Buildium population BY CONSTRUCTION**, not by
#: convention: 0100's ``assert_one_pms_per_company`` trigger refuses to let a
#: company hold both. A caller that unions them is describing a state the
#: database will not represent.
_WITH_RENTVINE_CREDENTIALS = """
SELECT DISTINCT company_id
  FROM public.company_rentvine_credentials
"""

#: Companies with mirrored books. Wider than the above: a mirror outlives a
#: credentials row (see the module docstring). This is the detector/analyst
#: population, restated here so the two stop being separate copies.
_WITH_MIRRORED_BOOKS = """
SELECT DISTINCT company_id FROM helixis_leases
UNION
SELECT DISTINCT company_id FROM helixis_bills
"""

#: Companies that HAVE reminder cards, whatever their credential state. The
#: population a CLEANUP pass needs — you cannot reconcile rows belonging to a
#: company you never visit.
_WITH_REMINDER_ROWS = """
SELECT DISTINCT company_id
  FROM helixis_reminders
 WHERE status <> 'cleared'
"""


async def _ids(pool: asyncpg.Pool, sql: str) -> list[str]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)
    return [str(r["company_id"]) for r in rows]


async def with_buildium_credentials(pool: asyncpg.Pool) -> list[str]:
    """Companies that can call Buildium. Use for work that HITS the API."""
    return await _ids(pool, _WITH_CREDENTIALS)


async def with_rentvine_credentials(pool: asyncpg.Pool) -> list[str]:
    """Companies that can call Rentvine. Use for work that HITS that API."""
    return await _ids(pool, _WITH_RENTVINE_CREDENTIALS)


async def with_pms_credentials(pool: asyncpg.Pool, key: str) -> list[str]:
    """Companies that can call one of the systems in ``pms.registry`` (the
    connectors' worker cycle, ``pms.sync_cycle``).

    ⚠ Deliberately global, like :func:`with_rentvine_credentials`: it IS the
    population a background cycle visits. The table name comes from the
    registry, never from a caller's string.
    """
    from helixis.pms.registry import spec

    found = spec(key)
    if found is None:
        raise ValueError(f"{key!r} is not a system in pms.registry")
    return await _ids(
        pool,
        f"SELECT DISTINCT company_id FROM public.{found.credentials_table}",  # noqa: S608
    )


async def with_mirrored_books(pool: asyncpg.Pool) -> list[str]:
    """Companies with mirrored leases or bills. Use for pure-SQL sweeps."""
    return await _ids(pool, _WITH_MIRRORED_BOOKS)


async def with_reminder_rows(pool: asyncpg.Pool) -> list[str]:
    """Companies holding at least one uncleared reminder card."""
    return await _ids(pool, _WITH_REMINDER_ROWS)


async def for_reminder_cleanup(pool: asyncpg.Pool) -> list[str]:
    """Credentialed companies PLUS any company still holding cards.

    ⚠ The union is the whole point. Cleanup must reach a company that has cards
    and no credentials — that is the exact state one test workspace is in, and the
    reason a retired producer's cards sat open for over a month under a
    guarantee that said they could not.

    Deliberately NOT used for card PRODUCTION: a company with no credentials
    cannot answer a Buildium call, so producing there would trade a stranded
    card for a failing sweep.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"{_WITH_CREDENTIALS} UNION {_WITH_REMINDER_ROWS}")
    return [str(r["company_id"]) for r in rows]


__all__ = [
    "for_reminder_cleanup",
    "with_buildium_credentials",
    "with_mirrored_books",
    "with_pms_credentials",
    "with_reminder_rows",
    "with_rentvine_credentials",
]
