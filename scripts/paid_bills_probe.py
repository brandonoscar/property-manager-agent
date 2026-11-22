"""Does this Buildium account have paid bills we are not mirroring?

READ-ONLY. Makes no writes, to Buildium or to the mirror.

WHY THIS EXISTS
The paid-bill fetch is NOT missing — ``buildium/backfill.py`` has asked for
``paidstatus=Paid`` with both date bounds since it was written, in its own try
block exactly as gotcha 14 requires. And the ingest derivation is correct:
``money.py::bill_is_paid`` reads ``PaidDate`` because ``IsPaid`` is NULL on
every production bill.

So when the mirror shows zero paid bills for a company, there are exactly three
explanations and they need very different responses:

  1. The account genuinely has no paid bills in the window   -> nothing to fix.
  2. It has them, but OUTSIDE the 365-day window             -> widen the window.
  3. The paid fetch is erroring every run                    -> a real bug.

Measured 2026-08-30: My Company 32 of 55 paid; Blue Ridge 0 of 67; Blue
Mountain 0 of 9. My Company's 32 were a backfill gap (rows predating #855,
fixed by migration 0081).

⚠ **Blue Ridge was then settled WITHOUT this script, and the method is worth
reusing before spending an API call.** All three explanations were eliminated
from the mirror alone:

  1. not mis-derivation — ``raw->>'PaidDate'`` is null on all 67;
  2. not outside the window — its bills span 2026-06-21..08-18, wholly inside
     the backfill's 365-day paid window;
  3. not an erroring fetch — ``_record_stage_freshness`` SKIPS any stage in
     ``failed_stages``, and a paid-side failure calls
     ``record_failure("bills", ...)``. Blue Ridge's ``bills`` row IS stamped
     (2026-08-30, row_count 67), and that counter increments in BOTH the
     unpaid and paid loops, so 67 = 67 unpaid + 0 paid.

So Blue Ridge genuinely has no paid bills, the mirror's zero is CORRECT, and
its "$26,000 across 67 bills" unpaid exposure is NOT overstated. one test workspace
matches the same pattern on an older stamp (2026-07-26).

That leaves this script for accounts whose freshness stamp is ABSENT or stale
— where the mirror cannot rule out a failing fetch — and for a NEW account
after onboarding, where there is no history to reason from at all.

    python scripts/paid_bills_probe.py --company-id <uuid>
    python scripts/paid_bills_probe.py --company-id <uuid> --years 5 --json

Exit 0 = probe ran and reported. Exit 2 = could not probe (missing
credentials). Deliberately NOT 0: a check that did not run is not a check that
passed, the same floor ``field_shape_probe.py`` holds.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, date, datetime, timedelta
from typing import Any

from helixis.buildium.api import BuildiumApi
from helixis.buildium.client import BuildiumClient
from helixis.buildium.credentials import load_buildium_settings_for_company
from helixis.buildium.money import bill_amount, bill_is_paid
from helixis.db.engine import init_pool
from helixis.settings import Settings

logger = logging.getLogger("paid_bills_probe")

#: Probe in ONE-YEAR slices rather than one wide range. Buildium's paid-bill
#: filter is the one live-probed as fussy (gotcha 14 — it 422s on a missing
#: bound), and a narrow window that returns rows is evidence; a wide window
#: that 422s is only evidence that the window was wide.
_SLICE_DAYS = 365


def _slices(years: int, today: date) -> list[tuple[date, date]]:
    """Consecutive one-year windows walking BACKWARD from today.

    Returned oldest-last so the output reads newest-first, which is the order a
    human scans for "when did we last pay anyone".
    """
    out: list[tuple[date, date]] = []
    end = today
    for _ in range(max(1, years)):
        start = end - timedelta(days=_SLICE_DAYS)
        out.append((start, end))
        end = start - timedelta(days=1)
    return out


async def _probe_window(api: BuildiumApi, start: date, end: date) -> dict[str, Any]:
    """One window. Never raises — a 422 here is a RESULT, not a crash.

    Each window is its own try block for gotcha 14's reason: the first
    regression in this area silently discarded a good fetch when a later one
    failed. A window that errors must not erase the windows that worked.
    """
    try:
        bills = await api.bills.list(
            paidstatus="Paid",
            frompaiddate=start.isoformat(),
            topaiddate=end.isoformat(),
        )
    except Exception as exc:  # noqa: BLE001 — the error IS the finding
        # ⚠ Every key the summariser reads must be present on BOTH returns.
        # The first cut omitted `confirmed_paid` here, so one failed window
        # raised KeyError in the roll-up — a crash on the error path, which is
        # the path that only runs when something is already wrong. It also
        # broke this script's stated contract: it exited 1 with a traceback
        # where the docstring promises an honest exit 2.
        return {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "error": f"{type(exc).__name__}: {exc}",
            "count": None,
            "confirmed_paid": None,
            "priced": None,
            "total": None,
        }

    # Confirm on the LIVE rows rather than trusting the filter: gotcha 24
    # records Buildium silently ignoring a filter param and returning the whole
    # account, 200 and no error. A "paid" list whose rows carry no PaidDate is
    # that failure, not a set of paid bills.
    with_paid_date = sum(1 for b in bills if bill_is_paid(b))
    amounts = [a for a in (bill_amount(b) for b in bills) if a is not None]
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "count": len(bills),
        "confirmed_paid": with_paid_date,
        "priced": len(amounts),
        "total": float(sum(amounts)) if amounts else None,
        "error": None,
    }


async def probe(company_id: str, years: int) -> dict[str, Any]:
    # ⚠ Per-COMPANY credentials, not the env pair. Buildium creds are stored
    # per company (Fernet-encrypted), so reading `settings.buildium` would
    # probe whatever account the env happens to point at while the
    # ``--company-id`` flag merely LABELLED the output — a probe that answers
    # confidently about the wrong account. Falls back to env only when the
    # company has no stored row, and says which it used.
    settings = Settings()

    # ⚠ The per-company lookup needs a DB POOL, and the first cut never opened
    # one. `load_buildium_settings_for_company` raised "Postgres pool not
    # initialized" on EVERY call, the except swallowed it as "a missing row is
    # a normal answer", and the probe fell back to env every single time — so
    # the fix written to stop `--company-id` merely LABELLING the output did
    # not stop it. The comment described a bug the code still had.
    source = "company"
    creds = None
    pool = None
    dsn = getattr(settings.supabase, "db_url", None)
    if dsn:
        try:
            pool = await init_pool(dsn)
            creds = await load_buildium_settings_for_company(company_id)
        except Exception as exc:  # noqa: BLE001 — no stored row is a real answer
            logger.info("no stored credentials for %s (%s)", company_id, exc)
    else:
        logger.info("no HELIXIS_SUPABASE__DB_URL — cannot read per-company credentials")

    if creds is None:
        creds = settings.buildium
        source = "env"

    if not (getattr(creds, "client_id", None) and getattr(creds, "client_secret", None)):
        if pool is not None:
            await pool.close()
        return {
            "ok": False,
            "reason": f"no Buildium credentials for company {company_id} and none in env",
            "company_id": company_id,
        }

    # ⚠ Name the ACCOUNT the answer is about. The env pair points at
    # apisandbox.buildium.com, which is a different account from the one
    # holding these bills — so an env-sourced verdict can be confidently about
    # the wrong books. Surfaced rather than assumed, because a probe that
    # cannot say which account it read is not evidence about any account.
    base_url = getattr(creds, "base_url", None) or "(default)"

    today = datetime.now(UTC).date()
    try:
        async with BuildiumClient(creds) as client:
            api = BuildiumApi(client)
            windows = [await _probe_window(api, s, e) for s, e in _slices(years, today)]
    finally:
        if pool is not None:
            await pool.close()

    found = sum(w["count"] or 0 for w in windows)
    confirmed = sum(w["confirmed_paid"] or 0 for w in windows)
    errored = [w for w in windows if w["error"]]
    return {
        "ok": True,
        "company_id": company_id,
        "credential_source": source,
        "base_url": base_url,
        "years_probed": years,
        "paid_bills_found": found,
        "confirmed_by_paid_date": confirmed,
        "windows_errored": len(errored),
        "windows": windows,
        # The whole point of the probe, stated rather than left to the reader.
        "verdict": _verdict(found, confirmed, errored, source=source),
    }


def _verdict(
    found: int, confirmed: int, errored: list[dict[str, Any]], *, source: str = "company"
) -> str:
    caveat = (
        ""
        if source == "company"
        else " ⚠ Read with ENV credentials, which may be a DIFFERENT account "
        "(the env pair points at the sandbox) — this verdict is not "
        "attributable to the company you named."
    )
    # Built as ONE string then caveated once. Appending per-branch is how the
    # first cut ended up caveating one of four returns — a qualifier that
    # applies to some verdicts and not others is worse than none, because the
    # uncaveated ones read as the trustworthy ones.
    if errored and not found:
        body = (
            "CANNOT TELL — every window errored. The zero in the mirror is "
            "unexplained, not confirmed. Read the window errors before "
            "concluding the account has no paid bills."
        )
    elif not found:
        body = (
            "NO PAID BILLS in the probed range. The mirror's zero is CORRECT "
            "and the _NO_PAYMENT_HISTORY caveat is telling the truth. Widen "
            "--years before treating this as final."
        )
    elif confirmed < found:
        body = (
            f"⚠ {found} rows returned but only {confirmed} carry a PaidDate. A "
            "filter Buildium ignored returns the whole account, 200 and no "
            "error (gotcha 24) — treat the unconfirmed rows as suspect."
        )
    else:
        body = (
            f"{found} PAID BILLS EXIST and are not in the mirror. Run the account "
            "backfill before access ends — this data is otherwise lost."
        )
    return body + caveat


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--company-id", required=True)
    ap.add_argument("--years", type=int, default=2, help="how far back to walk (default 2)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = asyncio.run(probe(args.company_id, args.years))

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if not result.get("ok"):
            print(f"could not probe: {result.get('reason')}")
        else:
            for w in result["windows"]:
                if w["error"]:
                    print(f"  {w['from']} .. {w['to']}   ERROR  {w['error']}")
                else:
                    total = f"${w['total']:,.2f}" if w["total"] is not None else "unpriced"
                    print(
                        f"  {w['from']} .. {w['to']}   {w['count']:>4} paid  "
                        f"({w['confirmed_paid']} confirmed)  {total}"
                    )
            print()
            print(result["verdict"])

    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
