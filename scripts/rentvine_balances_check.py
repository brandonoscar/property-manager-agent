#!/usr/bin/env python3
"""Do Rentvine's rent balances come out right? Read-only, founder-run.

The sync reads each lease's ``balances`` (and ``unpaidCharges``) off the lease
export it already pages through, and replaces ``helixis_lease_balances`` only
when every lease's balance came through (``sync._replace_balances``). Built
from the spec alone, so NOT VERIFIED until this has run. Three parts:

    1. the shape:   the first raw export row's KEY NAMES (no values): a
                    ``balances`` object beside the lease, and what
                    ``unpaidCharges`` is (a list, one object, missing)
    2. the totals:  the same functions the sync runs, over one complete read:
                    how many leases owe, the total, whether aging could be
                    stated, and up to five leases to hold against Rentvine's
                    own tenant ledger screens
    3. the mirror:  with ``--company-id`` and the DB variable, whether what the
                    sync STORED matches (needs a sync after this branch)

Sends GETs only. Prints sandbox ids and amounts to this console and writes no
file: nothing read from Rentvine is committed (terms rules 17-19).

    $env:RENTVINE_ACCOUNT_CODE = "<subdomain>"     # never an argument
    $s = Read-Host "RENTVINE_API_KEY" -AsSecureString
    $env:RENTVINE_API_KEY      = [Net.NetworkCredential]::new("", $s).Password
    $s = Read-Host "RENTVINE_API_SECRET" -AsSecureString
    $env:RENTVINE_API_SECRET   = [Net.NetworkCredential]::new("", $s).Password
    python scripts/rentvine_balances_check.py             # free, sends nothing
    python scripts/rentvine_balances_check.py --execute   # parts 1 and 2
    $s = Read-Host "the helixis-test pooler URL" -AsSecureString
    $env:HELIXIS_SUPABASE__DB_URL = [Net.NetworkCredential]::new("", $s).Password
    python scripts/rentvine_balances_check.py --execute --company-id $C   # all 3

Exit 0 pass · 1 a check FAILED (the line says which) · 2 could not run.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:  # operator tooling: runnable from a clone
    sys.path.insert(0, str(REPO / "src"))

CRED_VARS = ("RENTVINE_ACCOUNT_CODE", "RENTVINE_API_KEY", "RENTVINE_API_SECRET")
DB_VAR = "HELIXIS_SUPABASE__DB_URL"
SHOWN_LEASES = 5


def _settings_from_env() -> Any:
    from helixis.settings import RentvineSettings

    return RentvineSettings(
        enabled=True,
        account_code=os.environ.get("RENTVINE_ACCOUNT_CODE", "").strip(),
        api_key=os.environ.get("RENTVINE_API_KEY", "").strip(),
        api_secret=os.environ.get("RENTVINE_API_SECRET", "").strip(),
    )


def shape_report(raw_first: Any) -> tuple[list[str], bool]:
    """Lines describing the first raw export row, and whether its balances are
    the shape the sync reads. Key names only, never values."""
    if not isinstance(raw_first, dict):
        return [f"  first row is {type(raw_first).__name__}, not an object"], False
    lines = [f"  row keys: {sorted(raw_first)}"]
    bal = raw_first.get("balances")
    if not isinstance(bal, dict):
        kind = "missing" if "balances" not in raw_first else type(bal).__name__
        lines.append(f"  FAIL: 'balances' beside the lease is {kind}, not an object")
        return lines, False
    lines.append(f"  balances keys: {sorted(bal)}")
    ok = "unpaidTotalAmount" in bal
    if not ok:
        lines.append("  FAIL: balances has no unpaidTotalAmount")
    charges = raw_first.get("unpaidCharges")
    if isinstance(charges, list):
        first = charges[0] if charges and isinstance(charges[0], dict) else {}
        lines.append(f"  unpaidCharges: a list of {len(charges)}; first keys {sorted(first)}")
    else:
        kind = "missing" if "unpaidCharges" not in raw_first else type(charges).__name__
        lines.append(f"  unpaidCharges: {kind} (aging will be unknown; not a failure)")
    return lines, ok


async def _mirror_balances(company_id: str) -> tuple[int, Decimal | None]:
    import asyncpg

    conn = await asyncpg.connect(os.environ[DB_VAR])
    try:
        row = await conn.fetchrow(
            "SELECT count(*) AS n, sum(total_balance) AS owed FROM helixis_lease_balances "
            "WHERE company_id = $1::uuid AND pms = 'rentvine'",
            company_id,
        )
    finally:
        await conn.close()
    return int(row["n"]), row["owed"]


async def run(company_id: str | None) -> int:
    from helixis.rentvine import mapper
    from helixis.rentvine.client import RentvineAPIError, RentvineClient
    from helixis.rentvine.sync import BALANCE_SIBLINGS, LEASES_EXPORT_PATH

    failed = False
    async with RentvineClient(_settings_from_env()) as client:
        try:
            raw = await client.get(LEASES_EXPORT_PATH, query={"page": 1, "pageSize": 1})
        except RentvineAPIError as exc:
            print(f"COULD NOT RUN: {exc}")
            return 2
        print("1. lease export row shape")
        first = raw[0] if isinstance(raw, list) and raw else None
        if first is None:
            print("  the account has no leases; nothing to check")
            return 2
        lines, shape_ok = shape_report(first)
        print("\n".join(lines))
        print(f"  {'PASS' if shape_ok else 'FAIL'}: shape")
        failed |= not shape_ok
        try:
            export = await client.iter_all(LEASES_EXPORT_PATH, optional_companions=BALANCE_SIBLINGS)
        except RentvineAPIError as exc:
            print(f"  {'FAIL' if failed else 'COULD NOT RUN'}: {exc}")
            return 1 if failed else 2

    print("2. the totals (the functions the sync runs)")
    print(f"  leases: {len(export.rows)}, end confirmed by Rentvine: {export.stop_was_documented}")
    if not export.stop_was_documented:
        print("  FAIL: not confirmed complete; the sync would not replace the snapshot")
        failed = True
    picked = [mapper.lease_balance_known(row, date.today()) for row in export.rows]
    unknown = sum(1 for p in picked if p is mapper.BALANCE_UNKNOWN)
    owing = [p for p in picked if isinstance(p, dict)]
    aged = sum(1 for p in owing if p["balance_0_30"] is not None)
    owed = sum((p["total_balance"] for p in owing), Decimal(0))
    print(f"  leases that owe: {len(owing)} · total owed: ${owed:,.2f}")
    print(f"  aging could be stated for {aged} of {len(owing)} (the rest show no aging)")
    if unknown:
        print(f"  FAIL: {unknown} lease(s) came without a readable balance; the sync would")
        print("  refuse the whole snapshot and report balances stale")
        failed = True
    for p in sorted(owing, key=lambda p: p["total_balance"], reverse=True)[:SHOWN_LEASES]:
        print(f"  lease {p['lease_id']}: ${p['total_balance']:,.2f}")
    print("  CHECK BY HAND: open those leases' ledgers in Rentvine; the balance due")
    print("  should match. Note whether a tenant's credit shows as 0 or as a negative")

    if company_id:
        print("3. the mirror")
        if not os.environ.get(DB_VAR):
            print(f"  COULD NOT RUN: {DB_VAR} is not set")
            return 2
        n, stored = await _mirror_balances(company_id)
        if n != len(owing) or (stored or Decimal(0)) != owed:
            print(
                f"  FAIL: the mirror holds {n} lease(s) owing ${stored or 0:,.2f}; "
                f"Rentvine says {len(owing)} owing ${owed:,.2f} (synced after this branch?)"
            )
            failed = True
        else:
            print(f"  PASS: {n} lease(s), ${owed:,.2f} owed, same as Rentvine")
    print(f"VERDICT: {'FAILED' if failed else 'PASS'}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    ap.add_argument("--execute", action="store_true", help="send the GETs")
    ap.add_argument("--company-id", help="also compare the stored mirror (needs the DB var)")
    args = ap.parse_args(argv)
    missing = [v for v in CRED_VARS if not os.environ.get(v, "").strip()]
    for v in (*CRED_VARS, DB_VAR):
        print(f"  {v}: {'set' if os.environ.get(v, '').strip() else 'unset'}")
    if not args.execute:
        print("Dry run: nothing sent. Add --execute.")
        return 0
    if missing:
        print(f"COULD NOT RUN: set {', '.join(missing)} in this shell first")
        return 2
    return asyncio.run(run(args.company_id))


if __name__ == "__main__":
    sys.exit(main())
