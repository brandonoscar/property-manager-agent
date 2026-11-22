#!/usr/bin/env python3
"""Do Rentvine's owner-to-property links come out right? Read-only, founder-run.

The sync fills ``helixis_owners.property_ids`` by joining each property's
``portfolioID`` to ``GET /portfolios`` (``sync.owner_property_join``). The
property side was measured live (every sandbox property carries a top-level
``portfolioID``); the PORTFOLIO side was built from the spec alone, so its row
shape is NOT VERIFIED. This settles it, in three parts:

    1. the shape:   the first raw ``/portfolios`` element's KEY NAMES (no
                    values), and whether ``contacts[].contactID`` is there
    2. the join:    the same function the sync runs, over two complete reads,
                    printed per property as owner names so you can hold it
                    against Rentvine's own Owner tab
    3. the mirror:  with ``--company-id`` and the DB variable, whether what the
                    sync STORED equals the join (needs a sync after this
                    branch, e.g. Phase 8 step 3)

Sends GETs only. Prints sandbox names to this console and writes no file:
nothing read from Rentvine is committed (terms rules 17-19).

    $env:RENTVINE_ACCOUNT_CODE = "<subdomain>"     # never an argument
    $s = Read-Host "RENTVINE_API_KEY" -AsSecureString
    $env:RENTVINE_API_KEY      = [Net.NetworkCredential]::new("", $s).Password
    $s = Read-Host "RENTVINE_API_SECRET" -AsSecureString
    $env:RENTVINE_API_SECRET   = [Net.NetworkCredential]::new("", $s).Password
    python scripts/rentvine_owner_links_check.py             # free, sends nothing
    python scripts/rentvine_owner_links_check.py --execute   # parts 1 and 2
    $s = Read-Host "the helixis-test pooler URL" -AsSecureString
    $env:HELIXIS_SUPABASE__DB_URL = [Net.NetworkCredential]::new("", $s).Password
    python scripts/rentvine_owner_links_check.py --execute --company-id $C   # all 3

Exit 0 pass · 1 a check FAILED (the line says which) · 2 could not run.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:  # operator tooling: runnable from a clone
    sys.path.insert(0, str(REPO / "src"))

CRED_VARS = ("RENTVINE_ACCOUNT_CODE", "RENTVINE_API_KEY", "RENTVINE_API_SECRET")
DB_VAR = "HELIXIS_SUPABASE__DB_URL"
SHOWN_PROPERTIES = 5


def _settings_from_env() -> Any:
    from helixis.settings import RentvineSettings

    return RentvineSettings(
        enabled=True,
        account_code=os.environ.get("RENTVINE_ACCOUNT_CODE", "").strip(),
        api_key=os.environ.get("RENTVINE_API_KEY", "").strip(),
        api_secret=os.environ.get("RENTVINE_API_SECRET", "").strip(),
    )


def shape_report(raw_first: Any) -> tuple[list[str], bool]:
    """Lines describing the first raw ``/portfolios`` element, and whether the
    shape is the one the mapper reads. Key names only, never values."""
    from helixis.rentvine._row_envelopes import ROW_ENVELOPE

    envelope = ROW_ENVELOPE["GET /portfolios"]
    if not isinstance(raw_first, dict):
        return [f"  first element is {type(raw_first).__name__}, not an object"], False
    lines = [f"  raw element keys: {sorted(raw_first)}"]
    row = raw_first.get(envelope) if envelope else raw_first
    if envelope and not isinstance(row, dict):
        lines.append(f"  FAIL: no {envelope!r} wrapper; the generated envelope is wrong")
        return lines, False
    assert isinstance(row, dict)
    lines.append(f"  row keys: {sorted(row)}")
    ok = "portfolioID" in row
    contacts = row.get("contacts")
    if not isinstance(contacts, list):
        lines.append(f"  contacts: {type(contacts).__name__} (expected a list)")
        ok = False
    elif contacts and isinstance(contacts[0], dict):
        lines.append(f"  contacts[0] keys: {sorted(contacts[0])}")
        ok = ok and "contactID" in contacts[0]
    else:
        lines.append("  contacts: [] on this portfolio (look at another one in Rentvine)")
    if "portfolioID" not in row:
        lines.append("  FAIL: no portfolioID on the row")
    return lines, ok


async def _mirror_links(company_id: str) -> dict[int, list[int]]:
    import asyncpg

    conn = await asyncpg.connect(os.environ[DB_VAR])
    try:
        rows = await conn.fetch(
            "SELECT buildium_id, property_ids FROM helixis_owners "
            "WHERE company_id = $1::uuid AND pms = 'rentvine'",
            company_id,
        )
    finally:
        await conn.close()
    return {int(r["buildium_id"]): sorted(r["property_ids"] or []) for r in rows}


async def run(company_id: str | None) -> int:
    from helixis.rentvine import mapper
    from helixis.rentvine.client import RentvineAPIError, RentvineClient
    from helixis.rentvine.sync import PORTFOLIOS_PATH, owner_property_join

    failed = False
    async with RentvineClient(_settings_from_env()) as client:
        try:
            raw = await client.get(PORTFOLIOS_PATH, query={"page": 1, "pageSize": 1})
        except RentvineAPIError as exc:
            print(f"COULD NOT RUN: {exc}")
            return 2
        print("1. /portfolios row shape")
        first = raw[0] if isinstance(raw, list) and raw else None
        if first is None:
            print("  the account has no portfolios; nothing to check")
            return 2
        lines, shape_ok = shape_report(first)
        print("\n".join(lines))
        print(f"  {'PASS' if shape_ok else 'FAIL'}: shape")
        failed |= not shape_ok
        try:
            props = await client.iter_all("/properties")
            ports = await client.iter_all(PORTFOLIOS_PATH)
            owners = await client.iter_all("/owners")
        except RentvineAPIError as exc:
            # The client refuses a row it cannot unwrap, so a wrong envelope
            # lands here AFTER the shape lines above have named the keys.
            print(f"  {'FAIL' if failed else 'COULD NOT RUN'}: {exc}")
            return 1 if failed else 2

    print("2. the join (the function the sync runs)")
    for label, got in (("properties", props), ("portfolios", ports), ("owners", owners)):
        print(
            f"  {label}: {len(got.rows)} row(s), end confirmed by Rentvine: "
            f"{got.stop_was_documented}"
        )
        if not got.stop_was_documented:
            print(f"  FAIL: {label} not confirmed complete; the sync would not write links")
            failed = True
    portfolio_of: dict[int, int | None] = {}
    names: dict[int, str] = {}
    for row in props.rows:
        pid = mapper.map_property(row).get("buildium_id")
        if pid is not None:
            portfolio_of[int(pid)] = mapper.property_portfolio_id(row)
            names[int(pid)] = str(row.get("name") or pid)
    owner_names: dict[int, str] = {}
    for row in owners.rows:
        mapped = mapper.map_owner(row)
        if mapped.get("buildium_id") is not None:
            owner_names[int(mapped["buildium_id"])] = str(
                mapped.get("company_name") or mapped.get("last_name") or mapped["buildium_id"]
            )
    join = owner_property_join(portfolio_of, ports.rows)
    if join.problem:
        print(f"  FAIL: {join.problem}")
        return 1
    owned_by: dict[int, list[str]] = {}
    for owner, prop_ids in join.owns.items():
        for p in prop_ids:
            owned_by.setdefault(p, []).append(owner_names.get(owner, f"contact {owner}"))
    print(f"  owners linked: {len(join.owns)} · properties with no portfolio: {join.unlinked}")
    unknown = sorted(set(join.owns) - set(owner_names))
    if unknown:
        print(f"  FAIL: {len(unknown)} portfolio contact(s) are not in GET /owners")
        failed = True
    for p in sorted(portfolio_of)[:SHOWN_PROPERTIES]:
        print(f"  {names[p]}: {', '.join(sorted(owned_by.get(p, []))) or '(no owner)'}")
    print(
        f"  CHECK BY HAND: open those {min(SHOWN_PROPERTIES, len(portfolio_of))} "
        "properties in Rentvine and compare the owners"
    )

    if company_id:
        print("3. the mirror")
        if not os.environ.get(DB_VAR):
            print(f"  COULD NOT RUN: {DB_VAR} is not set")
            return 2
        stored = await _mirror_links(company_id)
        if not stored:
            print("  FAIL: no Rentvine owners stored for that company (sync it first)")
            return 1
        want = {o: sorted(join.owns.get(o, [])) for o in stored}
        diff = sorted(o for o in stored if stored[o] != want[o])
        if diff:
            print(
                f"  FAIL: {len(diff)} of {len(stored)} owner(s) differ from the join "
                "(was it synced after this branch?)"
            )
            failed = True
        else:
            print(f"  PASS: all {len(stored)} stored owner(s) match the join")
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
