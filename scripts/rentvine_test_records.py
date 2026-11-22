"""Give the Rentvine sandbox the records the newest sync paths have never seen.

WHY THIS EXISTS
    The sync reads rent payments, move-out dates and applications, and the
    sandbox has none of them (measured 2026-10-02: 12 lease transactions, all
    charges; no move-out date on any lease; no applications). Code that has
    only met fixtures is not proven. This makes the two of them the API can
    make, so the next sync reads real rows:

      * ONE rent payment (``POST /accounting/leases/{leaseID}/payments``) on a
        lease that owes at least ``--amount``, applied to its oldest charges.
        It settles the sign question in ``mapper.LEASE_TRANSACTION_TYPE``.
      * ONE expected move-out date (``POST /leases/{leaseID}``) on a lease
        with none, ``--move-out-days`` from today, notice given today. It opens
        a turnover on the next sync (``sync._start_turnovers``).

    And, as its own mode (``--rent-lease``), ONE rent change: the lease's rent
    recurring charge (``POST /leases/{leaseID}/recurring-charges/{chargeID}``,
    documented as a partial update) moved by ``--rent-amount``, so a sync can
    see whether a rent change moves the export's ``dateTimeModified``.
    (Measured 2026-10-02: it does not, which is why every lease's rent is
    re-read daily, ``sync.RENT_READ_ON``.)

    An APPLICATION cannot be made here: the API documents no create endpoint
    for one (only a status update), and documented endpoints are the only ones
    this project calls (terms rule 5). Make it in Rentvine's screen.

HOW IT WRITES, AND WHAT IT REFUSES
    The same client the sandbox seed uses (``rentvine_seed_sandbox.PacedClient``):
    one request a second at most, a hard call budget, no retries, and the first
    429 or error stops the run. And:

      * the lease update is READ-MERGE-WRITE: the spec does not say whether
        ``updateLease`` replaces the whole lease, so every field it accepts is
        sent back as it was read, with only the two dates changed;
      * ``emailReceipt`` is false, so nobody is emailed;
      * any account but ``--confirm-account`` is refused, and nothing is
        written without ``--execute --i-accept-the-write``;
      * what it did goes in a manifest (never committed), which ``--undo``
        reads to put the move-out dates back. A PAYMENT CANNOT BE UNDONE here:
        the API documents no void. Void it in Rentvine if you want it gone.

CREDENTIALS
    Environment only, never arguments, the same three names as the seed:

      $env:RENTVINE_ACCOUNT_CODE = "demoaccount41"
      $s = Read-Host "RENTVINE_API_KEY" -AsSecureString
      $env:RENTVINE_API_KEY      = [Net.NetworkCredential]::new("", $s).Password
      $s = Read-Host "RENTVINE_API_SECRET" -AsSecureString
      $env:RENTVINE_API_SECRET   = [Net.NetworkCredential]::new("", $s).Password

USAGE
    python scripts/rentvine_test_records.py --confirm-account demoaccount41
    python scripts/rentvine_test_records.py --confirm-account demoaccount41 \\
        --execute --i-accept-the-write
    python scripts/rentvine_test_records.py --confirm-account demoaccount41 \\
        --rent-lease 6 --execute --i-accept-the-write
    python scripts/rentvine_test_records.py --confirm-account demoaccount41 \\
        --undo --execute --i-accept-the-write

EXIT CODES
    0  done (or, dry, printed)
    1  stopped part-way: the manifest says what was written
    2  could not run (credentials, account, flags, or nothing suitable to use)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import urllib.parse
from decimal import Decimal, InvalidOperation
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import rentvine_seed_sandbox as seed  # noqa: E402

DEFAULT_MANIFEST = "docs/rentvine/test-records-manifest.json"
DEFAULT_AMOUNT = "100.00"
DEFAULT_MOVE_OUT_DAYS = 21
#: How far ``--rent-lease`` moves the rent when ``--rent-amount`` is not given.
DEFAULT_RENT_STEP = "25.00"
#: Two export pages, two lease reads, a payment and an update, plus slack.
DEFAULT_MAX_CALLS = 20
#: The export's measured page size (``sync.LEASES_EXPORT_PAGE_SIZE``).
_EXPORT_PAGE_SIZE = 25
_EXPORT_MAX_PAGES = 8

#: Every field ``updateLease`` accepts (the spec's request schema). Each one the
#: lease carries is sent back unchanged, so a full-replace server loses nothing.
UPDATE_LEASE_FIELDS = (
    "assignedToUserID",
    "noticeDate",
    "expectedMoveOutDate",
    "moveInDate",
    "moveOutDate",
    "depositRefundDueDate",
    "closedDate",
    "closedDescription",
    "startDate",
    "endDate",
    "increaseEligibilityDate",
    "leaseReturnChargeAmount",
    "lateFeeSettingID",
    "allowedPaymentAmountTypeID",
    "isTenantPastDueNoticeEnabled",
    "statementSettingID",
    "sendStatements",
    "code",
)
#: The two this script changes, and ``--undo`` restores.
MOVE_OUT_FIELDS = ("noticeDate", "expectedMoveOutDate")


def _money(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def export_leases(client: seed.PacedClient) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """``(lease, balances)`` for every lease the paged export lists."""
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for page in range(1, _EXPORT_MAX_PAGES + 1):
        query = urllib.parse.urlencode(seed.page_params(page, _EXPORT_PAGE_SIZE))
        rows = seed.rows_of(client.call("GET", f"/leases/export?{query}"))
        for row in rows:
            balances = row.get("balances")
            out.append((seed.record(row, "lease"), balances if isinstance(balances, dict) else {}))
        if len(rows) < _EXPORT_PAGE_SIZE:
            return out
    raise seed.SeedStopError(f"the lease export ran past {_EXPORT_MAX_PAGES} pages")


def pick_leases(
    leases: list[tuple[dict[str, Any], dict[str, Any]]], amount: Decimal
) -> tuple[str | None, str | None]:
    """``(lease to pay, lease to give a move-out)``: the first that owes at least
    ``amount``, and the first OTHER one with no move-out date at all."""
    pay = next(
        (
            str(lease["leaseID"])
            for lease, bal in leases
            if lease.get("leaseID") is not None
            and (_money(bal.get("unpaidTotalAmount")) or Decimal(0)) >= amount
        ),
        None,
    )
    move = next(
        (
            str(lease["leaseID"])
            for lease, _bal in leases
            if lease.get("leaseID") is not None
            and str(lease["leaseID"]) != pay
            and not lease.get("moveOutDate")
            and not lease.get("expectedMoveOutDate")
        ),
        None,
    )
    return pay, move


def active_lease_tenant_id(body: Any) -> str | None:
    """The first ACTIVE lease-tenant on a ``getLease?includes=tenants`` body."""
    tenants = body.get("tenants") if isinstance(body, dict) else None
    for item in tenants or []:
        link = item.get("leaseTenant") if isinstance(item, dict) else None
        if isinstance(link, dict) and str(link.get("isActive")) == "1":
            if link.get("leaseTenantID") not in (None, ""):
                return str(link["leaseTenantID"])
    return None


def payment_body(
    lease_id: str, lease_tenant_id: str, amount: str, today: dt.date
) -> dict[str, Any]:
    return {
        "leaseID": lease_id,
        "leaseTenantID": lease_tenant_id,
        "amount": amount,
        "datePosted": today.isoformat(),
        "applyAutomatically": True,
        "hasPrepayment": False,
        "bankAccountID": None,
        "emailReceipt": False,
        "description": "OCC-TEST payment (Occupella sync check)",
        "reference": "OCC-TEST",
    }


def lease_update_body(lease: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """Every accepted field the lease carries, as read, with ``changes`` on top."""
    body = {k: lease[k] for k in UPDATE_LEASE_FIELDS if k in lease}
    body.update(changes)
    return body


def _write(path: pathlib.Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def make(
    client: seed.PacedClient,
    manifest: dict[str, Any],
    *,
    amount: str,
    move_out_days: int,
    today: dt.date,
    execute: bool,
) -> None:
    leases = export_leases(client)
    pay_id, move_id = pick_leases(leases, Decimal(amount))
    if pay_id is None:
        raise seed.SeedStopError(f"no lease owes at least {amount}; lower --amount")
    if move_id is None:
        raise seed.SeedStopError("every other lease already has a move-out date")

    paying = client.call("GET", f"/leases/{pay_id}?includes=tenants")
    tenant = active_lease_tenant_id(paying)
    if tenant is None:
        raise seed.SeedStopError(f"lease {pay_id} has no active tenant to pay from")
    pay = payment_body(pay_id, tenant, amount, today)

    moving = seed.single_record(client.call("GET", f"/leases/{move_id}"), "lease")
    template = moving.get("startDate")
    leaving = today + dt.timedelta(days=move_out_days)
    changes = {
        "noticeDate": seed.same_date_format(template, today.year, today.month, today.day),
        "expectedMoveOutDate": seed.same_date_format(
            template, leaving.year, leaving.month, leaving.day
        ),
    }
    update = lease_update_body(moving, changes)

    if not execute:
        print(f"DRY RUN after {client.calls} read(s). Would send:")
        print(f"  POST /accounting/leases/{pay_id}/payments {json.dumps(pay)}")
        print(f"  POST /leases/{move_id} {json.dumps(update)}")
        return

    paid = client.call("POST", f"/accounting/leases/{pay_id}/payments", pay)
    manifest["payment"] = {
        "lease_id": pay_id,
        "transaction_id": seed.find_id(paid, "transactionID"),
        "amount": amount,
    }
    manifest["move_out"] = {
        "lease_id": move_id,
        "before": {k: moving.get(k) for k in MOVE_OUT_FIELDS},
        "after": changes,
    }
    got = seed.single_record(client.call("POST", f"/leases/{move_id}", update), "lease")
    manifest["move_out"]["read_back"] = {k: got.get(k) for k in MOVE_OUT_FIELDS}


def rent_charge(rows: list[dict[str, Any]], rent: Decimal) -> dict[str, Any]:
    """The lease's RENT recurring charge: the one non-move-in charge whose
    amount is the lease's rent. Anything but exactly one stops the run, never a
    guess at which charge is the rent."""
    charges = [seed.record(r, "recurringCharge") for r in rows]
    matches = [
        c
        for c in charges
        if _money(c.get("amount")) == rent and str(c.get("isMoveInCharge")) != "1"
    ]
    if len(matches) != 1:
        raise seed.SeedStopError(
            f"{len(matches)} recurring charge(s) match the rent {rent} "
            f"({len(charges)} on the lease); not guessing which is the rent"
        )
    return matches[0]


def change_rent(
    client: seed.PacedClient,
    manifest: dict[str, Any],
    *,
    lease_id: str,
    new_amount: str | None,
    execute: bool,
) -> None:
    lease = seed.single_record(client.call("GET", f"/leases/{lease_id}"), "lease")
    rent = _money(lease.get("rentAmount"))
    if rent is None:
        raise seed.SeedStopError(f"lease {lease_id} carries no readable rentAmount")
    rows = seed.rows_of(client.call("GET", f"/leases/{lease_id}/recurring-charges"))
    charge = rent_charge(rows, rent)
    charge_id = charge.get("leaseRecurringChargeID")
    if charge_id in (None, ""):
        raise seed.SeedStopError("the rent charge has no leaseRecurringChargeID")
    target = new_amount or f"{rent + Decimal(DEFAULT_RENT_STEP):.2f}"
    # ⚠ The EXPORT's dateTimeModified, never getLease's: the two routes report
    # different stamps for the same lease (measured 2026-10-01; 2026-10-02 lease
    # 6 read 2026-10-01 04:18:30 on the export and 2026-09-08 on getLease), and
    # the export's is the one the sync compares (sync.RENT_READ_MARKER).
    modified = next(
        (
            row.get("dateTimeModified")
            for row, _bal in export_leases(client)
            if str(row.get("leaseID")) == str(lease_id)
        ),
        None,
    )
    path = f"/leases/{lease_id}/recurring-charges/{charge_id}"
    # Documented as a partial update: only the field that changes is sent.
    body = {"amount": target}
    if not execute:
        print(f"DRY RUN after {client.calls} read(s). Would send POST {path} {json.dumps(body)}")
        print(f"  (rent {rent:.2f} -> {target}; export dateTimeModified now {modified!r})")
        return
    manifest["rent_change"] = {
        "lease_id": lease_id,
        "charge_id": str(charge_id),
        "before": f"{rent:.2f}",
        "after": target,
        "modified_before": modified,
    }
    client.call("POST", path, body)


def undo(client: seed.PacedClient, manifest: dict[str, Any], *, execute: bool) -> None:
    move = manifest.get("move_out")
    rent = manifest.get("rent_change")
    todo_move = bool(move) and not move.get("undone")
    todo_rent = bool(rent) and not rent.get("undone")
    if not (todo_move or todo_rent):
        raise seed.SeedStopError("the manifest records nothing left to put back")
    if todo_move:
        lease = seed.single_record(client.call("GET", f"/leases/{move['lease_id']}"), "lease")
        update = lease_update_body(lease, dict(move["before"]))
        if not execute:
            print(f"DRY RUN. Would send POST /leases/{move['lease_id']} {json.dumps(update)}")
        else:
            client.call("POST", f"/leases/{move['lease_id']}", update)
            move["undone"] = True
    if todo_rent:
        path = f"/leases/{rent['lease_id']}/recurring-charges/{rent['charge_id']}"
        body = {"amount": rent["before"]}
        if not execute:
            print(f"DRY RUN. Would send POST {path} {json.dumps(body)}")
        else:
            client.call("POST", path, body)
            rent["undone"] = True


def main(argv: list[str] | None = None, transport: seed.Transport = seed.urllib_transport) -> int:
    parser = argparse.ArgumentParser(description="Add one payment and one move-out to the sandbox.")
    parser.add_argument("--confirm-account", required=True, help="must equal RENTVINE_ACCOUNT_CODE")
    parser.add_argument("--execute", action="store_true", help="write (default: dry run)")
    parser.add_argument("--i-accept-the-write", action="store_true")
    parser.add_argument("--undo", action="store_true", help="put the move-out and rent back")
    parser.add_argument("--rent-lease", default=None, help="change THIS lease's rent instead")
    parser.add_argument("--rent-amount", default=None, help="the new rent (default: +25.00)")
    parser.add_argument("--amount", default=DEFAULT_AMOUNT)
    parser.add_argument("--move-out-days", type=int, default=DEFAULT_MOVE_OUT_DAYS)
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS)
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)

    creds = seed.Creds()
    if creds.missing:
        print(f"missing environment variables: {', '.join(creds.missing)}", file=sys.stderr)
        return 2
    if creds.account != args.confirm_account.strip():
        print(
            f"RENTVINE_ACCOUNT_CODE is {creds.account!r}, not {args.confirm_account!r}. Refusing.",
            file=sys.stderr,
        )
        return 2
    if args.execute and not args.i_accept_the_write:
        print("--execute needs --i-accept-the-write too.", file=sys.stderr)
        return 2
    for flag, value in (("--amount", args.amount), ("--rent-amount", args.rent_amount)):
        if value is not None and (_money(value) is None or Decimal(value) <= 0):
            print(f"{flag} {value!r} is not a positive amount.", file=sys.stderr)
            return 2

    path = pathlib.Path(args.manifest)
    manifest: dict[str, Any] = {"account": creds.account}
    if path.exists():
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("account") != creds.account:
            print(f"{path} belongs to another account. Refusing.", file=sys.stderr)
            return 2
        # One record of each kind per manifest, so --undo always knows what it is undoing.
        kind = "rent_change" if args.rent_lease else "payment"
        if not args.undo and args.execute and manifest.get(kind):
            print(
                f"{path} already records a run. --undo it, or pass another --manifest.",
                file=sys.stderr,
            )
            return 2
    elif args.undo:
        print(f"no manifest at {path}, so there is nothing to undo.", file=sys.stderr)
        return 2

    client = seed.PacedClient(
        creds, transport, interval=seed.MIN_INTERVAL_S, max_calls=args.max_calls
    )
    try:
        if args.undo:
            undo(client, manifest, execute=args.execute)
        elif args.rent_lease:
            change_rent(
                client,
                manifest,
                lease_id=args.rent_lease,
                new_amount=args.rent_amount,
                execute=args.execute,
            )
        else:
            make(
                client,
                manifest,
                amount=args.amount,
                move_out_days=args.move_out_days,
                today=dt.date.today(),
                execute=args.execute,
            )
    except seed.SeedStopError as exc:
        if args.execute:
            _write(path, manifest)
        print(f"STOPPED after {client.calls} call(s): {exc}", file=sys.stderr)
        if args.execute:
            print(f"  what was written is in {path}.", file=sys.stderr)
        return 1 if args.execute else 2

    if not args.execute:
        return 0
    _write(path, manifest)
    if args.undo:
        print(
            f"put back in {client.calls} call(s): the move-out and/or rent the manifest recorded."
        )
        if manifest.get("payment"):
            print("  The payment stays: the API documents no void. Void it in Rentvine if needed.")
        return 0
    if args.rent_lease:
        rc = manifest["rent_change"]
        print(f"done in {client.calls} call(s). Manifest: {path}")
        print(f"  rent on lease {rc['lease_id']}: {rc['before']} -> {rc['after']}")
        print("  Next: press Re-sync in Occupella, then ask Claude whether the lease's")
        print("  export dateTimeModified moved (it was " + repr(rc["modified_before"]) + ").")
        return 0
    pay, move = manifest["payment"], manifest["move_out"]
    print(f"done in {client.calls} call(s). Manifest: {path}")
    print(f"  payment {pay['amount']} on lease {pay['lease_id']}", end="")
    print(f" (transaction {pay['transaction_id']})")
    print(f"  expected move-out {move['after']['expectedMoveOutDate']} on lease {move['lease_id']}")
    if move["read_back"].get("expectedMoveOutDate") in (None, ""):
        print("  ⚠ Rentvine's reply did not carry the new move-out date: check the lease by hand.")
    print("  Next: make one application in Rentvine's screen, then press Re-sync in Occupella.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
