#!/usr/bin/env python3
"""Purge dev/synthetic test rows from PM-facing surfaces (#7 F5).

Dev probing left rows like ``Write-probe …`` and ``CRM Sync Test …`` in the
LIVE helixis-test DB; they leak into a real property manager's Inbox, Team
Activity, and CRM (the live E2E run flagged them). This deletes them from the
LOCAL Postgres tables that back those surfaces:

  * ``buildium_events``      — Inbox / work-order events (marker matched in the
                               ``payload`` JSON).
  * ``helixis_activity_log`` — Team Activity (marker matched in ``description``).
  * ``crm_leads``            — CRM pipeline (marker matched in ``name``;
                               ``crm_lead_messages`` cascade-delete with the lead).

Dry-run by DEFAULT — prints per-table counts + a sample and changes nothing.
Pass ``--yes-really-delete`` to actually delete. ``--company-id`` scopes to one
company; ``--pattern`` adds a marker substring (repeatable). Needs
``HELIXIS_SUPABASE__DB_URL``.

NOT touched: rows that live only in Buildium itself — the live work-order list
queries Buildium's API, so delete those in the Buildium sandbox, not here — and
any surface not backed by the three tables above.

Exit 0 on success (including a clean dry-run); exit 2 if the DB URL is missing.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from dataclasses import dataclass

# Substrings that identify a dev/synthetic row. Matched case-insensitively as
# ``%substring%``. Conservative + specific so a genuine PM row can't match.
DEFAULT_PATTERNS = ["Write-probe", "CRM Sync Test"]

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_.]*(::text)?$", re.IGNORECASE)


@dataclass(frozen=True)
class Target:
    """One table to sweep + the expression the marker is matched against."""

    table: str
    match_expr: str  # a column or a cast expression, e.g. "payload::text"
    label: str  # human description for the report


TARGETS: tuple[Target, ...] = (
    Target("buildium_events", "payload::text", "Inbox / work-order events"),
    Target("helixis_activity_log", "description", "Team Activity"),
    Target("crm_leads", "name", "CRM leads"),
)


def like_patterns(patterns: list[str]) -> list[str]:
    """Turn marker substrings into SQL ILIKE patterns (``%substring%``)."""
    return [f"%{p}%" for p in patterns]


def _validate(expr: str) -> str:
    """Guard the hardcoded table/column identifiers before interpolation."""
    if not _IDENT_RE.match(expr):
        raise ValueError(f"unsafe identifier: {expr!r}")
    return expr


def build_where(match_expr: str, *, has_company: bool) -> str:
    """WHERE clause: marker match (``$1`` = pattern array), optional company
    scope (``$2`` = company_id). Pure — no DB."""
    clause = f"{_validate(match_expr)} ILIKE ANY($1::text[])"
    if has_company:
        clause += " AND company_id = $2::uuid"
    return clause


def build_count_sql(table: str, match_expr: str, *, has_company: bool) -> str:
    return f"SELECT count(*) FROM public.{_validate(table)} WHERE {build_where(match_expr, has_company=has_company)}"  # noqa: E501


def build_sample_sql(table: str, match_expr: str, *, has_company: bool) -> str:
    return (
        f"SELECT {_validate(match_expr)} AS marker FROM public.{_validate(table)} "
        f"WHERE {build_where(match_expr, has_company=has_company)} LIMIT 3"
    )


def build_delete_sql(table: str, match_expr: str, *, has_company: bool) -> str:
    return f"DELETE FROM public.{_validate(table)} WHERE {build_where(match_expr, has_company=has_company)}"  # noqa: E501


def _args_for(patterns: list[str], company_id: str | None) -> list[object]:
    args: list[object] = [like_patterns(patterns)]
    if company_id:
        args.append(company_id)
    return args


async def run(patterns: list[str], company_id: str | None, *, delete: bool) -> int:
    import asyncpg

    db_url = os.environ.get("HELIXIS_SUPABASE__DB_URL", "")
    if not db_url:
        print("HELIXIS_SUPABASE__DB_URL not set — cannot connect.")
        return 2

    has_company = bool(company_id)
    scope = f" (company {company_id})" if company_id else " (ALL companies)"
    mode = "DELETE" if delete else "DRY-RUN"
    print(f"[{mode}] synthetic-row sweep{scope}; markers: {', '.join(patterns)}\n")

    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    total = 0
    try:
        for t in TARGETS:
            args = _args_for(patterns, company_id)
            count = await conn.fetchval(
                build_count_sql(t.table, t.match_expr, has_company=has_company), *args
            )
            total += count or 0
            print(f"  {t.table} — {t.label}: {count} matching row(s)")
            if count:
                samples = await conn.fetch(
                    build_sample_sql(t.table, t.match_expr, has_company=has_company), *args
                )
                for s in samples:
                    marker = str(s["marker"])
                    print(f"      · {marker[:100]}")
                if delete:
                    result = await conn.execute(
                        build_delete_sql(t.table, t.match_expr, has_company=has_company), *args
                    )
                    print(f"      → deleted ({result})")
    finally:
        await conn.close()

    if delete:
        print(f"\nDone — removed {total} synthetic row(s).")
    else:
        print(
            f"\nDRY-RUN — {total} row(s) would be deleted. "
            "Re-run with --yes-really-delete to remove them."
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--yes-really-delete",
        action="store_true",
        dest="delete",
        help="actually delete (default is a dry-run that changes nothing)",
    )
    parser.add_argument("--company-id", default=None, help="scope the sweep to one company UUID")
    parser.add_argument(
        "--pattern",
        action="append",
        default=None,
        dest="patterns",
        help="additional marker substring to match (repeatable); adds to the defaults",
    )
    args = parser.parse_args()
    patterns = DEFAULT_PATTERNS + list(args.patterns or [])
    return asyncio.run(run(patterns, args.company_id, delete=args.delete))


if __name__ == "__main__":
    raise SystemExit(main())
