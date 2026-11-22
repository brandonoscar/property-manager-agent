"""Take your data with you — as CSV, in a zip.

Two jobs in one module, and pairing them is deliberate:

* **Property managers live in Excel.** "Export to CSV" is asked for on day
  one, and a product that holds a portfolio and cannot hand it back reads as
  a trap.
* **Deleting an account without an export first is a data-loss button.**
  GDPR wants portability before erasure, and independently of the law, a
  destructive action whose only alternative is "lose everything" is one people
  are right to distrust.

⚠ **Company-scoped, every query, no exception** (gotcha 47). This is the one
surface whose entire output is somebody's business records; a missing `WHERE`
here does not produce a confusing answer, it hands one landlord another's
books in a file they keep.

⚠ **What is NOT here, and why.** No `helixis_sms_log` bodies, no chat
transcripts, no `orchestrator_runs`. Those carry other people's messages —
residents who never agreed to be in a landlord's export — and "export
everything" is not a licence to widen who can read a resident's text. The
tables below are the company's OWN operational records, which is what the
request actually means.
"""

from __future__ import annotations

import csv
import io
import logging
import zipfile
from datetime import UTC, datetime

import asyncpg

from helixis.common.support import contact_support

logger = logging.getLogger(__name__)

#: What a property manager would expect in a portfolio export, in reading
#: order. Table -> the columns worth having, because `select *` exports
#: internal ids and raw JSON blobs nobody can use in a spreadsheet.
#:
#: ⚠ Adding a table here is a DISCLOSURE decision, not a convenience one —
#: see the module docstring on what is deliberately absent.
#:
#: ⚠ Every column must exist (``test_every_export_column_exists`` reads the
#: migrations). Units asked for ``bedrooms``/``bathrooms``, leases for
#: ``rent_amount`` and balances for ``balance``/``as_of``, none of which any
#: migration creates (measured on helixis-test, 2026-10-05): each read failed,
#: and every customer's export carried those three files empty under a README
#: saying an empty file means "you have none".
EXPORT_TABLES: dict[str, tuple[str, ...]] = {
    "properties": (
        "buildium_id",
        "name",
        "address_line1",
        "city",
        "state",
        "postal_code",
    ),
    "units": ("buildium_id", "property_id", "unit_number", "market_rent", "is_occupied"),
    "tenants": ("buildium_id", "first_name", "last_name", "email", "phone"),
    "leases": (
        "buildium_id",
        "property_id",
        "unit_id",
        "status",
        "lease_from_date",
        "lease_to_date",
        "rent",
    ),
    "lease_balances": ("lease_id", "total_balance", "snapshot_at"),
    "work_orders": (
        "buildium_id",
        "property_id",
        "status",
        "priority",
        "title",
        "created_at",
    ),
    "tasks": ("buildium_id", "property_id", "status", "priority", "title", "created_at"),
    "bills": ("buildium_id", "vendor_id", "due_date", "total_amount", "is_paid"),
    "vendors": ("buildium_id", "name", "email", "phone"),
    "owners": ("buildium_id", "first_name", "last_name", "email"),
}


async def _rows(
    conn: asyncpg.Connection, table: str, columns: tuple[str, ...], company_id: str
) -> list[dict[str, object]] | None:
    """One table's rows for one company, or ``None`` when it could not be read.

    ⚠ Column names are interpolated because asyncpg cannot parameterise an
    identifier — so they come from `EXPORT_TABLES`, a module constant, and
    NEVER from a request. `company_id` is a real bound parameter.

    ⚠ A table that cannot be read is SKIPPED, not fatal. An export that
    refuses entirely because one table is unreadable gives the customer
    nothing; a zip missing one file gives them their portfolio.

    ⚠ ``None``, never ``[]``: an empty list becomes an empty file, which the
    README tells the reader means "you have none" (rule 14).
    """
    cols = ", ".join(f'"{c}"' for c in columns)
    try:
        records = await conn.fetch(
            f'select {cols} from public."helixis_{table}" where company_id = $1::uuid',  # noqa: S608
            company_id,
        )
    except Exception:  # noqa: BLE001
        logger.warning("export: skipping %s (unreadable)", table, exc_info=True)
        return None
    return [dict(r) for r in records]


def _to_csv(rows: list[dict[str, object]], columns: tuple[str, ...]) -> str:
    """⚠ Writes the HEADER even with no rows. An empty file says "you have no
    vendors"; a missing file is indistinguishable from an export that broke,
    and the reader cannot tell which without asking us."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(columns), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _cell(v) for k, v in row.items()})
    return buf.getvalue()


def _cell(value: object) -> object:
    """Datetimes as ISO; everything else as-is.

    ⚠ `str(datetime)` gives `2026-09-05 12:00:00+00:00`, which Excel parses
    inconsistently across locales. ISO-8601 is what a spreadsheet and a script
    both read the same way.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    return value


async def build_export(pool: asyncpg.Pool, company_id: str, company_name: str) -> bytes:
    """Every exportable table for one company, as a zip of CSVs.

    Held in memory on purpose: the largest account measured here is a few
    thousand rows, so streaming would add failure modes (a half-written zip a
    client cannot open) to solve a problem this data does not have. Revisit
    when an export stops fitting comfortably — not before.
    """
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest: list[str] = [
            f"Occupella export for {company_name}",
            f"Generated {datetime.now(UTC).isoformat()}",
            "",
            "One CSV per table. An empty file means you have no rows of that",
            "kind — not that the export failed.",
            "",
        ]
        unread: list[str] = []
        async with pool.acquire() as conn:
            for table, columns in EXPORT_TABLES.items():
                rows = await _rows(conn, table, columns, company_id)
                if rows is None:
                    unread.append(f"{table}.csv")
                    continue
                zf.writestr(f"{table}.csv", _to_csv(rows, columns))
                manifest.append(f"{table}.csv — {len(rows)} rows")
        if unread:
            # Named, never shipped empty: the line above says an empty file
            # means none, and these are files we failed to read.
            manifest += [
                "",
                "NOT EXPORTED, because of an error on our side (this is NOT an "
                f"empty table): {', '.join(unread)}. Please {contact_support()} and "
                "we will send them.",
            ]

        zf.writestr("README.txt", "\n".join(manifest) + "\n")

    logger.info("export: built for company=%s on %s", company_id, stamp)
    return buf.getvalue()


def export_filename(company_name: str) -> str:
    """A filename a person can find again in their Downloads folder."""
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in company_name).strip("-")
    return f"occupella-{safe or 'export'}-{datetime.now(UTC).strftime('%Y-%m-%d')}.zip"


__all__ = ["EXPORT_TABLES", "build_export", "export_filename"]
