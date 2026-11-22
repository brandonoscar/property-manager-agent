#!/usr/bin/env python3
"""Do Rentvine's bills come out right? Read-only, founder-run.

The sync builds ``helixis_bills`` from ``GET /accounting/payables/search``, one
row per bill LINE, grouped by ``mapper.group_payables`` and totalled by
``mapper.map_bill``. Built from the spec alone, so NOT VERIFIED until this has
run. It answers, in three parts:

    1. the shape:   the first raw row's KEY NAMES (no values): a ``bill`` and a
                    ``transaction`` side by side, and an ``amount`` that reads
                    as a decimal
    2. the totals:  the same two functions the sync runs, over one complete
                    read: how many bills, how many unpaid, the unpaid total,
                    and up to five unpaid bills to hold against Rentvine's own
                    Bills screen
    3. the mirror:  with ``--company-id`` and the DB variable, whether what the
                    sync STORED matches (needs a sync after this branch)

Sends GETs only. Prints sandbox names and amounts to this console and writes
no file: nothing read from Rentvine is committed (terms rules 17-19).

    $env:RENTVINE_ACCOUNT_CODE = "<subdomain>"     # never an argument
    $s = Read-Host "RENTVINE_API_KEY" -AsSecureString
    $env:RENTVINE_API_KEY      = [Net.NetworkCredential]::new("", $s).Password
    $s = Read-Host "RENTVINE_API_SECRET" -AsSecureString
    $env:RENTVINE_API_SECRET   = [Net.NetworkCredential]::new("", $s).Password
    python scripts/rentvine_bills_check.py             # free, sends nothing
    python scripts/rentvine_bills_check.py --execute   # parts 1 and 2
    $s = Read-Host "the helixis-test pooler URL" -AsSecureString
    $env:HELIXIS_SUPABASE__DB_URL = [Net.NetworkCredential]::new("", $s).Password
    python scripts/rentvine_bills_check.py --execute --company-id $C   # all 3

Exit 0 pass · 1 a check FAILED (the line says which) · 2 could not run.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:  # operator tooling: runnable from a clone
    sys.path.insert(0, str(REPO / "src"))

CRED_VARS = ("RENTVINE_ACCOUNT_CODE", "RENTVINE_API_KEY", "RENTVINE_API_SECRET")
DB_VAR = "HELIXIS_SUPABASE__DB_URL"
SHOWN_BILLS = 5


def _settings_from_env() -> Any:
    from helixis.settings import RentvineSettings

    return RentvineSettings(
        enabled=True,
        account_code=os.environ.get("RENTVINE_ACCOUNT_CODE", "").strip(),
        api_key=os.environ.get("RENTVINE_API_KEY", "").strip(),
        api_secret=os.environ.get("RENTVINE_API_SECRET", "").strip(),
    )


def shape_report(raw_first: Any) -> tuple[list[str], bool]:
    """Lines describing the first raw payables row, and whether it is the shape
    the sync reads. Key names only, never values."""
    from helixis.rentvine.mapper import _money

    if not isinstance(raw_first, dict):
        return [f"  first row is {type(raw_first).__name__}, not an object"], False
    lines = [f"  row keys: {sorted(raw_first)}"]
    ok = True
    bill, txn = raw_first.get("bill"), raw_first.get("transaction")
    if not isinstance(bill, dict):
        lines.append("  FAIL: no 'bill' object on the row")
        return lines, False
    lines.append(f"  bill keys: {sorted(bill)}")
    if not isinstance(txn, dict):
        lines.append("  FAIL: no 'transaction' object beside the bill")
        return lines, False
    lines.append(f"  transaction keys: {sorted(txn)}")
    for key in ("billID", "payeeContactID"):
        if key not in bill:
            lines.append(f"  FAIL: bill has no {key}")
            ok = False
    for key in ("amount", "amountPaid"):
        if key not in txn:
            lines.append(f"  FAIL: transaction has no {key}")
            ok = False
        elif _money(txn[key]) is None:
            lines.append(f"  FAIL: transaction.{key} does not read as a decimal")
            ok = False
    return lines, ok


async def _mirror_bills(company_id: str) -> tuple[int, Decimal | None]:
    import asyncpg

    conn = await asyncpg.connect(os.environ[DB_VAR])
    try:
        row = await conn.fetchrow(
            "SELECT count(*) AS n, sum(total_amount) FILTER (WHERE is_paid IS FALSE) AS owed "
            "FROM helixis_bills WHERE company_id = $1::uuid AND pms = 'rentvine'",
            company_id,
        )
    finally:
        await conn.close()
    return int(row["n"]), row["owed"]


async def run(company_id: str | None) -> int:
    from helixis.rentvine import mapper
    from helixis.rentvine.client import RentvineAPIError, RentvineClient
    from helixis.rentvine.sync import PAYABLES_PATH

    failed = False
    async with RentvineClient(_settings_from_env()) as client:
        try:
            raw = await client.get(PAYABLES_PATH, query={"page": 1, "pageSize": 1})
        except RentvineAPIError as exc:
            print(f"COULD NOT RUN: {exc}")
            return 2
        print("1. payables row shape")
        first = raw[0] if isinstance(raw, list) and raw else None
        if first is None:
            print("  the account has no bills; enter one in Rentvine and run again")
            return 2
        lines, shape_ok = shape_report(first)
        print("\n".join(lines))
        print(f"  {'PASS' if shape_ok else 'FAIL'}: shape")
        failed |= not shape_ok
        try:
            payables = await client.iter_all(
                PAYABLES_PATH,
                companion="transaction",
                nullable_companions=("ledger", "property"),
            )
            vendors = await client.iter_all("/vendors")
        except RentvineAPIError as exc:
            print(f"  {'FAIL' if failed else 'COULD NOT RUN'}: {exc}")
            return 1 if failed else 2

    print("2. the totals (the functions the sync runs)")
    print(
        f"  payables: {len(payables.rows)} line(s), end confirmed by Rentvine: "
        f"{payables.stop_was_documented}"
    )
    if not payables.stop_was_documented:
        print("  FAIL: not confirmed complete; the sync would not prune stale bills")
        failed = True
    records, voided = mapper.group_payables(payables.rows)
    bills = [mapper.map_bill(r) for r in records]
    names = {}
    for row in vendors.rows:
        v = mapper.map_vendor(row)
        if v.get("buildium_id") is not None:
            names[int(v["buildium_id"])] = str(v.get("name") or v["buildium_id"])
    unpaid = [b for b in bills if b["is_paid"] is False]
    unknown = [b for b in bills if b["is_paid"] is None or b["total_amount"] is None]
    children = sum(1 for r in records if r.get("parentBillID") is not None)
    owed = sum((b["total_amount"] for b in unpaid if b["total_amount"] is not None), Decimal(0))
    print(f"  bills: {len(bills)} · voided, left out: {voided} · unpaid: {len(unpaid)}")
    print(f"  owed on unpaid bills: ${owed:,.2f}")
    print(f"  markup/discount child bills (NOT VERIFIED what they mean): {children}")
    if unknown:
        print(f"  FAIL: {len(unknown)} bill(s) have an amount or paid flag that did not read")
        failed = True
    for b in sorted(unpaid, key=lambda b: (b["due_date"] is None, b["due_date"]))[:SHOWN_BILLS]:
        who = names.get(b["vendor_id"], f"payee {b['vendor_id']}")
        print(f"  {who}: ${b['total_amount']:,.2f}, due {b['due_date']}")
    print("  CHECK BY HAND: Rentvine > Accounting > Bills, unpaid only; the count, the")
    print("  total and the bills above should match")

    if company_id:
        print("3. the mirror")
        if not os.environ.get(DB_VAR):
            print(f"  COULD NOT RUN: {DB_VAR} is not set")
            return 2
        n, stored_owed = await _mirror_bills(company_id)
        if n != len(bills) or (stored_owed or Decimal(0)) != owed:
            print(
                f"  FAIL: the mirror holds {n} bill(s) owing ${stored_owed or 0:,.2f}; "
                f"Rentvine says {len(bills)} owing ${owed:,.2f} (synced after this branch?)"
            )
            failed = True
        else:
            print(f"  PASS: {n} bill(s), ${owed:,.2f} owed, same as Rentvine")
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
