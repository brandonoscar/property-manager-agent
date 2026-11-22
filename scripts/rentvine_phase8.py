"""Phase 8's sandbox run: sync a real Rentvine account and reconcile it.

WHY THIS EXISTS
    ``docs/rentvine/PROGRESS.md`` makes Phase 8 the one gate every remaining
    phase waits behind, and names its pass criterion: *"the mirror's row count
    must equal the same count read back from the API, and a re-run must be
    idempotent"*. Both halves of the judgement were already written —
    ``scripts/rentvine_reconcile.py`` is the pure comparison and
    ``helixis.rentvine.sync.sync_company`` is the cycle — and **nothing
    connected them.** Measured 2026-09-22:

      * ``grep -rn sync_company src/ scripts/`` finds its own ``__all__``, one
        docstring, and no caller. The nightly cycle had never been reachable
        from a command. Only ``refresh_one`` is wired, from the webhook router,
        which refuses every delivery until the signature scheme is known.
      * ``reconcile()`` takes two dicts of counts and **nothing anywhere
        produces them.** Its own docstring says *"Phase 8 wires the two readers
        and calls reconcile"* — that wiring is this file.

    So the two gotchas that both end *"the first sandbox sync settles it"*
    (148, the mapped date; 149, ``confirmed_complete``) named an event that had
    no way to happen.

WHAT IT TOUCHES
    Rentvine: **reads only.** ``RentvineClient``'s read methods hardcode the
    verb and this harness never calls ``post`` — the one write method on the
    client — so no run can change the customer's account. That is a property of
    the client rather than a promise made here (CLAUDE.md gotcha 70: a 2xx is
    not the write, and the mirror image is that a harness claiming read-only
    should be read-only by construction).

    Our own mirror: ``--execute`` WRITES. That is what a sync is. The default
    run does not: it reads both sides and reports the comparison, which on a
    never-synced company correctly reports NOT RECONCILED rather than pretending
    the question was answered.

CREDENTIALS
    From the environment, never an argument, and the SAME three names the
    read-only probe uses — one vocabulary, so an operator who has run the probe
    has already done this step (gotcha 73: ``"approved"`` typed into three
    modules is how a fourth drifts).

        $env:RENTVINE_ACCOUNT_CODE = "<subdomain>"
        $s = Read-Host "RENTVINE_API_KEY" -AsSecureString
        $env:RENTVINE_API_KEY      = [Net.NetworkCredential]::new("", $s).Password
        $s = Read-Host "RENTVINE_API_SECRET" -AsSecureString
        $env:RENTVINE_API_SECRET   = [Net.NetworkCredential]::new("", $s).Password
        $s = Read-Host "the helixis-test pooler URL" -AsSecureString
        $env:HELIXIS_SUPABASE__DB_URL = [Net.NetworkCredential]::new("", $s).Password

    ``--execute`` additionally needs ``HELIXIS_RENTVINE__ENABLED=true`` **in
    that shell**. The connector ships dark and ``sync_company`` refuses while
    it is off; this harness names the variable rather than forging a Settings
    with the flag flipped, because a validation run that exercises a path the
    deployment refuses can only ever report on behaviour no customer reaches.

    ⚠ No ``HELIXIS_DATA_KEY`` and no per-company credentials row is needed:
    ``sync_company`` accepts an injected client, so the harness builds one from
    these three and the Fernet path is never reached. That is deliberate —
    gotcha 11 forbids handing that key around, and Phase 8 does not need it.

EXIT CODES, and the middle one is the point
    0  reconciled — every family's API count equals its mirror count, and a
       second sync inserted nothing.
    1  NOT reconciled — a real finding. The per-family lines name which.
    2  could not run — missing credentials, auth failure, no database. **Never
       a pass.** A harness that did not run is not one that validated anything
       (the LED-013 skip-floor, and §4's A2P harness records what happens when
       "every POST was accepted" and "the registration is valid" print the
       same).

⚠ THE API COUNTS ARE READ INDEPENDENTLY OF THE SYNC, AND THAT COSTS A SECOND
  FETCH ON PURPOSE. ``SyncResult.rows_by_table`` is sitting right there and
  using it would make this measure what the sync DID rather than what is true —
  the failure ``rentvine_reconcile``'s own header is about: *"a reconciliation
  written by whoever wrote the sync tends to measure what the sync does ... the
  check passes because it shares the bug."*

⚠ A FAILED READ COUNTS AS ``None``, NEVER ``0``. That is the whole subject of
  the reconciler (0 == 0 must not read as RECONCILED), and it is the one place
  this file could quietly destroy the guarantee it exists to exercise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "src") not in sys.path:  # operator tooling: runnable from a clone
    sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import rentvine_reconcile as rec  # noqa: E402

DEFAULT_OUT = "docs/rentvine/phase8-report.json"

#: Mirror table -> the API path whose rows it holds. Derived from the sync's own
#: constants rather than retyped, so a source added there cannot be silently
#: missing from the reconciliation (gotcha 28's both-sides rule).
CRED_VARS = ("RENTVINE_ACCOUNT_CODE", "RENTVINE_API_KEY", "RENTVINE_API_SECRET")
DB_VAR = "HELIXIS_SUPABASE__DB_URL"


def _families() -> dict[str, str]:
    """Mirror table -> API path, for every family the sync writes.

    Read from ``sync.SOURCES`` plus its two special cases rather than
    hand-listed: a seventh family added to the sync and forgotten here would
    make this harness report RECONCILED on a mirror it never compared.
    """
    from helixis.rentvine import sync

    families = {table: path for table, path in sync.SOURCES}
    families["helixis_leases"] = sync.LEASES_EXPORT_PATH
    # The links come from the SAME export rows (each lease's active tenants).
    families["helixis_lease_tenants"] = sync.LEASES_EXPORT_PATH
    families["helixis_units"] = sync.UNITS_PATH
    # One payables row per bill LINE; the family is the bills they fold into.
    families["helixis_bills"] = sync.PAYABLES_PATH
    families["helixis_bill_lines"] = sync.PAYABLES_PATH
    # Balances ride on the lease export rows (sync._replace_balances).
    families["helixis_lease_balances"] = sync.LEASES_EXPORT_PATH
    families["helixis_applicants"] = sync.APPLICATIONS_PATH
    # Each lease's history comes from getLease (sync._read_rents).
    families["helixis_lease_transactions"] = sync.LEASE_PATH
    return families


async def _count_lease_transactions(
    client: Any, lease_rows: list[dict[str, Any]]
) -> tuple[int | None, dict[str, Any]]:
    """Every lease's mirrored history, counted from ``getLease`` the way the
    sync reads it, and what the rows looked like.

    Also settles the two ``@unverified`` questions in
    ``mapper.lease_transactions`` without recording a single value: how many
    items came wrapped ``{transaction}`` against bare, and how many amounts of
    each type were negative. Counts only, never amounts (terms rule 19).
    """
    from helixis.rentvine import mapper, sync

    ids = [mapper.map_lease(row)["buildium_id"] for row in lease_rows]
    ids = [i for i in ids if i is not None]
    if len(ids) > sync.LEASE_RENT_READS_PER_SYNC:
        return None, {
            "read": False,
            "error": f"{len(ids)} leases is more than one pass reads "
            f"({sync.LEASE_RENT_READS_PER_SYNC}); not counted here",
        }
    total = 0
    shape = {"wrapped": 0, "bare": 0}
    negative: dict[str, int] = {}
    for lease_id in ids:
        try:
            body = await client.get(
                sync.LEASE_PATH,
                path_params={"leaseID": lease_id},
                query={"includes": "tenants,transactions"},
            )
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            return None, {"read": False, "error": f"lease {lease_id}: {exc}"}
        items = body.get("transactions") if isinstance(body, dict) else None
        rows = mapper.lease_transactions(items, lease_id)
        if rows is None:
            return None, {"read": False, "error": f"lease {lease_id}: no transactions list"}
        total += len(rows)
        for item in items or []:
            if not isinstance(item, dict):
                continue
            wrapped = isinstance(item.get("transaction"), dict)
            shape["wrapped" if wrapped else "bare"] += 1
            txn = item["transaction"] if wrapped else item
            amount = mapper._money(txn.get("amount"))
            if amount is not None and amount < 0:
                kind = str(txn.get("transactionTypeID"))
                negative[kind] = negative.get(kind, 0) + 1
    return total, {
        "read": True,
        "stop_was_documented": True,
        "item_shape": shape,
        "negative_amounts_by_type": negative,
    }


async def _api_counts(client: Any) -> tuple[dict[str, int | None], dict[str, Any]]:
    """Count each family from the API, independently of the sync.

    Returns the counts and a provenance block. A family whose read raises is
    ``None`` — not zero, and not absent.
    """
    from helixis.rentvine import sync
    from helixis.rentvine.client import RentvineTruncatedError

    counts: dict[str, int | None] = {}
    provenance: dict[str, Any] = {}

    property_ids: list[int] = []

    for table, path in sync.SOURCES:
        try:
            collected = await client.iter_all(path)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            counts[table] = None
            provenance[table] = {"read": False, "error": str(exc)}
            continue
        counts[table] = len(collected.rows)
        provenance[table] = {
            "read": True,
            "stop_was_documented": collected.stop_was_documented,
        }
        if table == "helixis_properties":
            for row in collected.rows:
                pid = row.get("propertyID") or row.get("id")
                if isinstance(pid, int):
                    property_ids.append(pid)
                elif isinstance(pid, str) and pid.isdigit():
                    property_ids.append(int(pid))

    # Leases AND their links from the paged export (the sync's own source,
    # sync.LEASES_EXPORT_PATH). ⚠ COUNTED ONLY WHEN THE SERVER NAMED THE END:
    # a guessed stop is UNREAD, never a number — a short count is what made
    # `reconcile` call real leases "extra" on 2026-09-24.
    lease_families = (
        "helixis_leases",
        "helixis_lease_tenants",
        "helixis_lease_balances",
        "helixis_lease_transactions",
    )
    try:
        exported = await client.iter_all(
            sync.LEASES_EXPORT_PATH,
            page_size=sync.LEASES_EXPORT_PAGE_SIZE,
            optional_companions=sync.BALANCE_SIBLINGS,
        )
    except Exception as exc:  # noqa: BLE001
        for table in lease_families:
            counts[table] = None
            provenance[table] = {"read": False, "error": str(exc)}
    else:
        if exported.stop_was_documented:
            from helixis.rentvine import mapper

            counts["helixis_leases"] = len(exported.rows)
            counts["helixis_lease_tenants"] = sum(
                len(mapper.map_lease_tenant_links(row)) for row in exported.rows
            )
            for table in ("helixis_leases", "helixis_lease_tenants"):
                provenance[table] = {"read": True, "stop_was_documented": True}
            # Leases that OWE (the snapshot holds only those). One unreadable
            # balance makes the count unknown, as it makes the sync refuse.
            picked = [mapper.lease_balance_known(row, date.today()) for row in exported.rows]
            if any(p is mapper.BALANCE_UNKNOWN for p in picked):
                counts["helixis_lease_balances"] = None
                provenance["helixis_lease_balances"] = {
                    "read": False,
                    "error": "a lease came without a readable balance",
                }
            else:
                counts["helixis_lease_balances"] = sum(1 for p in picked if isinstance(p, dict))
                provenance["helixis_lease_balances"] = {"read": True, "stop_was_documented": True}
            (
                counts["helixis_lease_transactions"],
                provenance["helixis_lease_transactions"],
            ) = await _count_lease_transactions(client, exported.rows)
        else:
            for table in lease_families:
                counts[table] = None
                provenance[table] = {
                    "read": False,
                    "error": "the lease export sent no pagination headers, so its end "
                    "is a guess and its count is not a total",
                }

    # Applications: one mirror row each (sync._sync_applicants).
    try:
        apps = await client.iter_all(sync.APPLICATIONS_PATH)
    except Exception as exc:  # noqa: BLE001
        counts["helixis_applicants"] = None
        provenance["helixis_applicants"] = {"read": False, "error": str(exc)}
    else:
        counts["helixis_applicants"] = len(apps.rows) if apps.stop_was_documented else None
        provenance["helixis_applicants"] = (
            {"read": True, "stop_was_documented": True}
            if apps.stop_was_documented
            else {"read": False, "error": "no pagination headers: the count is not a total"}
        )

    # Bills from the payables search, folded by the sync's own grouping (a
    # count of LINES would never equal the mirror's bills). ⚠ COUNTED ONLY
    # WHEN THE SERVER NAMED THE END, for the leases' reason above.
    try:
        payables = await client.iter_all(
            sync.PAYABLES_PATH,
            companion="transaction",
            nullable_companions=("ledger", "property"),
            optional_companions=("unit",),
        )
    except Exception as exc:  # noqa: BLE001
        for table in ("helixis_bills", "helixis_bill_lines"):
            counts[table] = None
            provenance[table] = {"read": False, "error": str(exc)}
    else:
        if payables.stop_was_documented:
            from helixis.rentvine import mapper

            bills, voided = mapper.group_payables(payables.rows)
            counts["helixis_bills"] = len(bills)
            counts["helixis_bill_lines"] = sum(len(mapper.bill_lines(b)) for b in bills)
            provenance["helixis_bill_lines"] = {"read": True, "stop_was_documented": True}
            provenance["helixis_bills"] = {
                "read": True,
                "stop_was_documented": True,
                "lines": len(payables.rows),
                "voided_left_out": voided,
            }
        else:
            counts["helixis_bills"] = None
            counts["helixis_bill_lines"] = None
            provenance["helixis_bill_lines"] = {"read": False, "error": "end not confirmed"}
            provenance["helixis_bills"] = {
                "read": False,
                "error": "the payables search sent no pagination headers, so its end "
                "is a guess and its count is not a total",
            }

    # ⚠ Units are per-property (the sync's deliberate N+1 — the bulk export
    # drops propertyID). If the property fetch failed there is no work-list, so
    # the count is UNREAD rather than zero: "we could not ask" and "there are
    # none" are the distinction this whole file turns on.
    if counts.get("helixis_properties") is None:
        counts["helixis_units"] = None
        provenance["helixis_units"] = {
            "read": False,
            "error": "the property fetch failed, so there was no work-list to count units from",
        }
    else:
        total = 0
        failed: str | None = None
        short: list[dict[str, Any]] = []
        for pid in property_ids:
            try:
                rows = await client.get_rows(sync.UNITS_PATH, path_params={"propertyID": pid})
            except RentvineTruncatedError as exc:
                # ⚠ THE FALSE PASS (2026-09-24): a 120-unit property sent 20, the
                # sync read the same 20, and both sides said 57. The sync now
                # completes such a property from the units export, so the count
                # here is the server's OWN total from this property's headers,
                # never the 20 that arrived. With no total the family is UNREAD.
                short.append({"property": pid, **_truncated(exc)})
                if exc.server_total is None:
                    continue
                total += exc.server_total
                continue
            except Exception as exc:  # noqa: BLE001
                failed = f"property {pid}: {exc}"
                break
            total += len(rows)
        if failed is None and any(p["server_total"] is None for p in short):
            counts["helixis_units"] = None
            provenance["helixis_units"] = {"read": False, "truncated": short}
        elif failed is None and short:
            counts["helixis_units"] = total
            provenance["helixis_units"] = {
                "read": True,
                "properties_walked": len(property_ids),
                "truncated": short,
                "counted_from": "the server's pagination-total-items for each short property",
            }
        elif failed is not None:
            # ⚠ **THIS DELIBERATELY DIFFERS FROM `sync._sync_units`, WHICH
            # RECORDS AND CONTINUES (gotcha 160).** There, a partial table is a
            # real outcome to report — the rows are in the mirror and `failed`
            # says the rest is stale. Here a partial count is a NUMBER, and a
            # smaller one makes `reconcile` report EXTRA ("the mirror holds more
            # than the API returned"), whose own wording sends an operator after
            # a cross-tenant scoping bug that does not exist. Unread is the only
            # honest value for a walk that did not finish.
            counts["helixis_units"] = None
            provenance["helixis_units"] = {"read": False, "error": failed}
        else:
            counts["helixis_units"] = total
            provenance["helixis_units"] = {
                "read": True,
                "properties_walked": len(property_ids),
                "stop_was_documented": None,
            }

    return counts, provenance


def _truncated(exc: Any) -> dict[str, Any]:
    """Provenance for a read the server's own headers say came back short."""
    return {
        "read": False,
        "truncated": True,
        "sent": len(exc.rows),
        "server_total": exc.server_total,
        "error": exc.detail,
    }


def _truncation_notes(provenance: dict[str, Any]) -> list[str]:
    """One line per family Rentvine sent short, for the operator."""
    notes: list[str] = []
    for table, prov in sorted(provenance.items()):
        if prov.get("truncated") is True:
            notes.append(f"{table}: Rentvine sent {prov['sent']} of {prov['server_total']}")
        elif isinstance(prov.get("truncated"), list):
            for p in prov["truncated"]:
                notes.append(
                    f"{table}: property {p['property']} sent {p['sent']} of {p['server_total']}"
                )
    return notes


async def _mirror_counts(pool: Any, company_id: str) -> dict[str, int | None]:
    """Count each family in our mirror, scoped to the company AND the PMS.

    The SQL comes from ``rentvine_reconcile.mirror_count_sql`` rather than being
    written here, because both of its predicates are load-bearing and one of
    them (``company_id``) fails in the direction that makes the numbers look
    CLOSER — gotcha 47's cross-tenant read, hiding as a pass.
    """
    counts: dict[str, int | None] = {}
    for table in _families():
        try:
            counts[table] = await pool.fetchval(rec.mirror_count_sql(table), company_id, "rentvine")
        except Exception:  # noqa: BLE001 - an unreadable table is UNREAD, not empty
            counts[table] = None
    return counts


def _total(counts: dict[str, int | None]) -> int | None:
    """Sum the mirror, or ``None`` if any family could not be read.

    An unreadable family makes the TOTAL unreadable too: idempotency compares
    two totals, and quietly dropping a family from one side of that subtraction
    is how a re-run that inserted rows reports as clean.
    """
    if any(v is None for v in counts.values()):
        return None
    return sum(v for v in counts.values() if v is not None)


def _settings_from_env() -> Any:
    from helixis.settings import RentvineSettings

    return RentvineSettings(
        enabled=True,
        account_code=os.environ.get("RENTVINE_ACCOUNT_CODE", "").strip(),
        api_key=os.environ.get("RENTVINE_API_KEY", "").strip(),
        api_secret=os.environ.get("RENTVINE_API_SECRET", "").strip(),
    )


def _write(path: str, report: dict[str, Any]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    # encoding is not optional: this report carries ⚠ and cp1252 would raise on
    # the LAST line of a run that had already spent every API call (gotcha 49).
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")


async def _run(args: argparse.Namespace) -> int:
    import asyncpg

    from helixis.rentvine.client import RentvineClient
    from helixis.rentvine.sync import RentvineSyncDisabled, sync_company

    report: dict[str, Any] = {
        "company_id": args.company_id,
        "executed": bool(args.execute),
        "harness": "scripts/rentvine_phase8.py",
        "rentvine_access": "read-only",
    }

    settings = _settings_from_env()
    try:
        client = RentvineClient(settings)
    except ValueError as exc:
        print(f"VERDICT: COULD NOT RUN — {exc}")
        return 2

    pool = await asyncpg.create_pool(os.environ[DB_VAR], min_size=1, max_size=4)
    if pool is None:
        print("VERDICT: COULD NOT RUN — no database pool")
        return 2

    try:
        print("Reading the API, independently of the sync...")
        api, provenance = await _api_counts(client)
        report["api_counts"] = api
        report["api_provenance"] = provenance

        if all(v is None for v in api.values()):
            report["verdict"] = "COULD NOT RUN — every API read failed"
            _write(args.out, report)
            print("\nVERDICT: COULD NOT RUN — every API read failed; nothing was compared.")
            print(f"Report: {args.out}")
            return 2

        before = await _mirror_counts(pool, args.company_id)
        report["mirror_counts_before"] = before

        if args.execute:
            print("Sync 1 of 2...")
            try:
                first = await sync_company(pool, args.company_id, client=client)
            except RentvineSyncDisabled as exc:
                # ⚠ The harness does NOT forge the flag, and that is deliberate.
                # `HELIXIS_RENTVINE__ENABLED` defaults false and render.yaml
                # declares it false on web AND worker — the connector ships
                # dark. Constructing a Settings with it flipped would let a
                # harness run a path the deployment refuses, which is the one
                # thing a validation run must never do: it would then report
                # RECONCILED about behaviour no customer can reach. So it names
                # the variable and stops.
                report["verdict"] = f"COULD NOT RUN — {exc}"
                _write(args.out, report)
                print(f"\nVERDICT: COULD NOT RUN — {exc}")
                print('\nSet it for THIS SHELL only: $env:HELIXIS_RENTVINE__ENABLED = "true"')
                print("The connector ships dark on purpose; this harness will not flip it for you.")
                print(f"Report: {args.out}")
                return 2
            report["sync_1"] = {
                "rows_by_table": first.rows_by_table,
                "skipped_no_id": first.skipped_no_id,
                "failed": first.failed,
                "confirmed_complete": sorted(first.confirmed_complete),
            }
            mirror = await _mirror_counts(pool, args.company_id)
            report["mirror_counts_after_1"] = mirror

            print("Sync 2 of 2 (idempotency)...")
            second = await sync_company(pool, args.company_id, client=client)
            report["sync_2"] = {
                "rows_by_table": second.rows_by_table,
                "failed": second.failed,
                "confirmed_complete": sorted(second.confirmed_complete),
            }
            after = await _mirror_counts(pool, args.company_id)
            report["mirror_counts_after_2"] = after
            idem = rec.idempotency(_total(mirror), _total(after))
        else:
            mirror = before
            idem = None
    finally:
        await client.aclose()
        await pool.close()

    result = rec.reconcile(api, mirror)
    report["reconciled"] = result.ok
    report["idempotent"] = None if idem is None else idem.ok
    report["verdict"] = (
        "RECONCILED" if result.ok and (idem is None or idem.ok) else "NOT RECONCILED"
    )
    _write(args.out, report)

    print()
    print(rec.render(result, idem))
    if not args.execute:
        print("\n(dry pass — nothing was synced, so a fresh company reads NOT RECONCILED")
        print(" correctly. Re-run with --execute to sync and compare.)")
    # ⚠ confirmed_complete is reported rather than gated on: a family the loop
    # guessed the end of is a Phase 8 FINDING about the spec's headers, not a
    # sync fault (gotcha 149), and conflating the two sends somebody after the
    # wrong layer.
    if args.execute:
        guessed = sorted(
            set(report["sync_1"]["rows_by_table"]) - set(report["sync_1"]["confirmed_complete"])
        )
        if guessed:
            print(f"\n  note: end-of-collection was not confirmed for {', '.join(guessed)}")
            print("        (units are read per property, unpaged, so the sync reads one")
            print("         response and checks the server's pagination headers against it;")
            print("         leases count only when the lease export's headers named the end)")
    notes = _truncation_notes(report.get("api_provenance", {}))
    if notes:
        print("\n  ⚠ Rentvine sent SHORT on routes the spec says cannot be paged:")
        for line in notes:
            print(f"      {line}")
        print("    Units are counted from the server's own total (the sync completes them")
        print("    from the units export).")
    print(f"\nReport: {args.out}")
    return 0 if report["verdict"] == "RECONCILED" else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    ap.add_argument("--company-id", help="the Occupella company whose mirror to compare")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument(
        "--check",
        action="store_true",
        help="report which credentials are set and exit; sends nothing",
    )
    ap.add_argument(
        "--execute",
        action="store_true",
        help="run the sync twice (WRITES to our mirror; still read-only against Rentvine)",
    )
    args = ap.parse_args(argv)

    try:  # Windows console: this prints ⚠ (gotcha 49)
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    missing = [n for n in CRED_VARS if not os.environ.get(n, "").strip()]
    db_set = bool(os.environ.get(DB_VAR, "").strip())

    if args.check:
        print("Environment (set/unset only — no values are printed):")
        for name in CRED_VARS:
            print(f"  {name:28} {'set' if name not in missing else 'UNSET'}")
        print(f"  {DB_VAR:28} {'set' if db_set else 'UNSET'}")
        return 0 if not missing and db_set else 2

    if missing or not db_set:
        absent = missing + ([] if db_set else [DB_VAR])
        print("VERDICT: COULD NOT RUN — missing " + ", ".join(absent))
        print("\nSet them in the environment, never as arguments.")
        print("Exit 2 is not a pass — nothing was validated.")
        return 2

    if not args.company_id:
        print("VERDICT: COULD NOT RUN — --company-id is required")
        print("\nIt scopes both the mirror count and the sync. Without it a count")
        print("would reach another tenant's rows, which reads as a closer match")
        print("rather than a failure (CLAUDE.md gotcha 47).")
        return 2

    return asyncio.run(_run(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
