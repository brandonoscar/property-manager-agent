#!/usr/bin/env python3
"""Right-to-erasure: purge ALL data for a company (GDPR / CCPA / SOC2).

The audit (2026-07-12, Q30) found only agent *memory* had a working erasure
path (``purge_company_memory.py``) — the relational tenant data (the 13-table
Buildium mirror, activity log, chat, CRM, idempotency, queue, …) had no
company-wide delete and no proof of completeness. This is the umbrella eraser:
relational data + memory + a verified proof that nothing tenant-scoped remains.

Design (never hardcodes the table list — it asks the schema, so it can't drift
as tables are added):

  1. DISCOVER every ``public`` table that has a ``company_id`` column.
  2. COUNT rows per table for the company (the dry-run report).
  3. DELETE ``public.companies`` for that id inside a transaction → the
     ``on delete cascade`` FKs remove every FK'd tenant table atomically.
  4. RESIDUAL SWEEP any discovered table that still has rows (a table whose
     ``company_id`` is not FK-cascaded — e.g. a text key without the FK) gets an
     explicit ``DELETE … WHERE company_id = $1``.
  5. MEMORY purge via ``MemoryManager.delete_company_memory`` (mem0's runtime
     tables aren't FK'd to companies).
  6. VERIFY — re-count every discovered table + memory; ALL must be 0, or the
     run fails WITHOUT certifying erasure.
  7. PROOF — emit a structured erasure record (company, operator, per-table
     counts, verified) to stdout; the in-DB audit rows are themselves erased, so
     the operator archives this record as the retained evidence.

    python scripts/purge_company_data.py <company_id>                     # dry-run report
    python scripts/purge_company_data.py <company_id> --yes-really-delete # DESTRUCTIVE
        [--requested-by <admin_auth_users_uuid>]

Needs HELIXIS_SUPABASE__DB_URL (service-role DSN, bypasses RLS) and — for the
memory leg — HELIXIS_LLM__VOYAGE_API_KEY. This container can't reach Supabase
(egress); run it from a machine/CI that can. The pure SQL/plan/proof logic is
unit-tested so a regression is caught without a live DB.

⚠ That key was HELIXIS_LLM__GOOGLE_API_KEY until 2026-09-03 and had been wrong
since 2026-07-22, when the embedder moved to Voyage (gotcha 18). An operator
following it would have exported a key to an account that no longer exists,
and the memory leg degrades SILENTLY — `MemoryManager` swallows and returns
empty by design (gotcha 17), so the run would have printed a verified proof
with memory untouched. A stale name in an erasure runbook is not a typo; it is
a false certificate.

✅ STORAGE IS NO LONGER MANUAL (2026-09-11). It was the first item on the list
below, and the shape of the gap is why it mattered: the script printed
"✓ Verified" while every uploaded lease, browser screenshot and CSV export sat
in three company-id-prefixed buckets, and the only place that said so was this
docstring. `storage_purge.purge_company_storage` now runs as its own leg, the
record carries `storage_objects_erased` / `storage_verified` / `storage_note`,
and a bucket that could not even be ENUMERATED fails the certification instead
of reading as empty — "there were no files" and "I could not look" are not the
same answer.

⚠ WHAT THIS SCRIPT STILL DOES NOT DO: the Stripe subscription, the Composio
connection, the Twilio number, and `auth.users` — the customer can still SIGN
IN after a "verified" erasure and the bootstrap self-heal hands them a fresh
empty company. Those four legs are manual. Follow
`docs/runbooks/account-deletion.md`, which also records why Stripe has to be
cancelled BEFORE the delete (the row holds the only link to their Stripe
customer).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import UTC, datetime
from typing import Any

# Table + column names that reach SQL string interpolation come from
# information_schema (trusted), but we still validate the shape as defense in
# depth — a name that isn't a plain identifier is refused, never interpolated.
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

# The cascade root: deleting this id fans out via `on delete cascade` FKs.
_ROOT_TABLE = "companies"

DISCOVER_SQL = (
    "SELECT table_name FROM information_schema.columns "
    "WHERE table_schema = 'public' AND column_name = 'company_id' "
    "ORDER BY table_name"
)


def _safe_ident(name: str) -> str:
    """Return ``name`` if it's a plain lowercase identifier, else raise. Guards
    the count/delete SQL builders against anything that isn't a bare table name."""
    if not _IDENT_RE.match(name):
        raise ValueError(f"unsafe identifier from schema introspection: {name!r}")
    return name


def count_sql(table: str) -> str:
    """``SELECT count(*) … WHERE company_id = $1`` for a discovered table."""
    return f"SELECT count(*) FROM public.{_safe_ident(table)} WHERE company_id = $1"


def delete_sql(table: str) -> str:
    """``DELETE … WHERE company_id = $1`` for a residual-sweep table."""
    return f"DELETE FROM public.{_safe_ident(table)} WHERE company_id = $1"


def delete_root_sql() -> str:
    """Delete the company row — cascades all FK'd tenant tables."""
    return f"DELETE FROM public.{_ROOT_TABLE} WHERE id = $1"


def residual_tables(counts: dict[str, int]) -> list[str]:
    """Tables that STILL have rows after the cascade — need an explicit delete."""
    return sorted(t for t, n in counts.items() if n > 0)


def erasure_record(
    company_id: str,
    *,
    requested_by: str,
    counts_before: dict[str, int],
    memory_erased: int,
    verified: bool,
    at: datetime,
    storage_objects_erased: int = 0,
    storage_verified: bool = False,
    storage_note: str = "",
) -> dict[str, Any]:
    """The retained proof of erasure (the in-DB audit rows are themselves
    deleted, so this is the evidence the operator archives).

    ⚠ **Each leg states its own verdict and they are NOT collapsed into one
    flag.** `verified_zero_remaining` has always meant the relational leg and
    keeps meaning exactly that — quietly widening it to include Storage would
    change what every archived record before today asserted. `storage_verified`
    is its own field, and it is three-valued in effect: false covers both "some
    objects survived" and "the bucket could not be read", with `storage_note`
    carrying which. An audit fact a reader has to decode is one they decode
    wrongly (gotcha 94).

    ⚠ `storage_verified` defaults to **False**, not True. A caller that forgets
    to pass it must produce a record that under-claims, never one that
    certifies a sweep nobody ran.
    """
    return {
        "event": "company_data_erased",
        "company_id": company_id,
        "requested_by": requested_by or "operator",
        "erased_at": at.astimezone(UTC).isoformat(),
        "relational_rows_erased": sum(counts_before.values()),
        "per_table_rows_erased": {t: n for t, n in sorted(counts_before.items()) if n > 0},
        "memory_rows_erased": memory_erased,
        "verified_zero_remaining": verified,
        "storage_objects_erased": storage_objects_erased,
        "storage_verified": storage_verified,
        "storage_note": storage_note,
    }


# ── live DB legs (owner-run; not exercised in unit tests) ────────────


async def _counts(conn: Any, tables: list[str], company_id: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in tables:
        out[t] = int(await conn.fetchval(count_sql(t), company_id) or 0)
    return out


async def _discover(conn: Any) -> list[str]:
    rows = await conn.fetch(DISCOVER_SQL)
    # companies itself keys on `id`, not company_id — excluded by the query; the
    # discovered set is the FK'd/child tables we count + verify.
    return [r["table_name"] for r in rows]


async def _purge_memory(company_id: str, requested_by: str) -> int:
    """Erase mem0 memory (not FK'd to companies). Best-effort: a memory backend
    that's unreachable must not block relational erasure — but it IS reported."""
    try:
        from helixis.memory.manager import MemoryManager
        from helixis.settings import Settings

        mgr = MemoryManager(Settings())
        await mgr.initialize()
        if not mgr.available:
            print("  ⚠ memory backend unavailable — memory NOT erased (rerun the memory purge).")
            return 0
        return int(await mgr.delete_company_memory(company_id, requested_by=requested_by))
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ memory erase failed ({type(exc).__name__}: {exc}) — rerun the memory purge.")
        return 0


async def _purge_storage(company_id: str) -> Any:
    """Erase the company's Supabase Storage objects.

    ⚠ Unlike the memory leg above, an exception here does NOT degrade to a
    reported zero. `purge_company_storage` never raises and returns a report
    whose `complete` is false when a bucket could not even be enumerated, so
    an unreachable Storage API produces a refusal to certify rather than a
    clean-looking sweep. If this import itself fails the run must still not
    certify — hence a synthetic incomplete report rather than a swallow.
    """
    from helixis.settings import Settings
    from helixis.storage_purge import (  # noqa: PLC0415
        BucketPurge,
        StoragePurgeReport,
        purge_company_storage,
    )

    try:
        return await purge_company_storage(company_id=company_id, settings=Settings())
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ storage erase could not run ({type(exc).__name__}: {exc}).")
        return StoragePurgeReport(
            company_id=company_id,
            buckets=[BucketPurge(bucket="(all)", enumerated=False, error=str(exc))],
        )


async def run(company_id: str, *, execute: bool, requested_by: str) -> int:
    import asyncpg

    dsn = os.environ.get("HELIXIS_SUPABASE__DB_URL", "")
    if not dsn:
        print("HELIXIS_SUPABASE__DB_URL is not set.", file=sys.stderr)
        return 2

    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        tables = await _discover(conn)
        before = await _counts(conn, tables, company_id)
        total = sum(before.values())
        print(
            f"Company {company_id}: {total} relational row(s) across {len(tables)} tenant tables."
        )
        for t in residual_tables(before):
            print(f"  {t}: {before[t]}")

        if not execute:
            print("\nDRY RUN — nothing deleted. Re-run with --yes-really-delete to erase.")
            return 0

        async with conn.transaction():
            await conn.execute(delete_root_sql(), company_id)
            # Residual sweep: anything the cascade didn't reach.
            after = await _counts(conn, tables, company_id)
            for t in residual_tables(after):
                await conn.execute(delete_sql(t), company_id)

        memory_erased = await _purge_memory(company_id, requested_by)
        storage = await _purge_storage(company_id)

        # Verify — the proof. Re-count from scratch; all must be zero.
        final = await _counts(conn, tables, company_id)
        remaining = residual_tables(final)
        verified = not remaining
    finally:
        await conn.close()

    record = erasure_record(
        company_id,
        requested_by=requested_by,
        counts_before=before,
        memory_erased=memory_erased,
        verified=verified,
        at=datetime.now(UTC),
        storage_objects_erased=storage.deleted_total,
        storage_verified=storage.complete,
        storage_note="" if storage.complete else storage.summary(),
    )
    print("\nERASURE RECORD (archive this — the in-DB audit rows are gone):")
    print(json.dumps(record, indent=2))
    if not verified:
        print(f"\n✗ NOT fully erased — rows remain in: {', '.join(remaining)}", file=sys.stderr)
        return 1
    # ⚠ The relational leg passing is no longer enough to print a certificate.
    # Until this existed the script printed "✓ Verified" with three buckets of
    # the customer's PDFs untouched, and its own docstring was the only place
    # that said so.
    if not storage.complete:
        print(f"\n✗ NOT fully erased — {storage.summary()}", file=sys.stderr)
        return 1
    print(
        f"\n✓ Verified: 0 rows remain for {company_id} "
        f"(+{memory_erased} memories, +{storage.deleted_total} storage objects erased)."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Erase ALL data for a company (right-to-erasure).")
    ap.add_argument("company_id", help="public.companies(id) of the tenant to erase")
    ap.add_argument(
        "--requested-by", default="", help="admin auth.users(id) UUID recorded on the proof"
    )
    ap.add_argument(
        "--yes-really-delete",
        action="store_true",
        help="confirm the DESTRUCTIVE full erasure (default is a dry-run report)",
    )
    args = ap.parse_args(argv)
    return asyncio.run(
        run(args.company_id, execute=args.yes_really_delete, requested_by=args.requested_by)
    )


if __name__ == "__main__":
    raise SystemExit(main())
