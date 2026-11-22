"""Write a connector's rows into the shared mirror (the spine interface).

A connector maps its vendor's records into :data:`MirrorRows` (one
:class:`TableRead` per mirror table) and calls :func:`write_mirror`. Every row
lands in the EXISTING mirror tables with ``pms`` set and ``account_id = 0``,
on each table's own conflict target, in the same SQL shapes Rentvine's sync
uses (``rentvine/sync.py``), so every reader the product has works unchanged
(ADR 0010 Decision (a)). It shares none of Rentvine's code (design P1).

The rules, each from a defect this product already had:

* **A failed or incomplete table is STALE, never empty** (gotcha 106). Its
  rows that did arrive are written (they are real), it goes in
  :attr:`SyncResult.failed`, and :func:`stamp_freshness` does not stamp it.
* **Prune only from a complete read**, and never a row touched after the read
  began (gotcha 182). Not when a row came back without an id, and not when an
  id came back twice (a page boundary moved mid-read).
* **A delta prunes nothing by itself.** An incremental pass reads only what
  changed since its watermark (:attr:`TableRead.delta`), and a row that did
  not change is simply not in it. Only a complete id-only read
  (:attr:`TableRead.keep_ids`) says what the vendor no longer has.
* **A value a connector did not read is left as it was.** An upsert writes
  only the columns the row carries; a column the connector KNOWS is unknown is
  sent as ``None`` and written as NULL (rule 14), never left stale.
* **Every statement names the company and the system** (rule 11), so a
  connector can never touch another company's rows or another system's rows.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from helixis.pms.registry import PMS

logger = logging.getLogger(__name__)

#: Extra seconds a prune leaves alone beyond the read's own length, covering
#: the moment between measuring the read and the DELETE (Rentvine's margin).
PRUNE_TOUCH_MARGIN_S = 60.0

#: Tables keyed ``ON CONFLICT (company_id, buildium_id)``, and the columns an
#: upsert may write beyond ``buildium_id``, ``account_id``, ``company_id``,
#: ``pms`` and ``raw``. The same sets as Rentvine's ``TABLE_COLUMNS``, which
#: are pinned to the Buildium INSERTs; ``test_pms_mirror`` reads both sides.
KEYED_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "helixis_properties": (
        "name",
        "rental_sub_type",
        "address_line1",
        "city",
        "state",
        "postal_code",
        "is_active",
    ),
    "helixis_units": ("property_id", "unit_number", "market_rent", "is_occupied"),
    "helixis_leases": (
        "property_id",
        "unit_id",
        "status",
        "lease_from_date",
        "lease_to_date",
        "rent",
        "security_deposit",
    ),
    "helixis_tenants": ("first_name", "last_name", "email", "phone"),
    "helixis_owners": (
        "first_name",
        "last_name",
        "company_name",
        "email",
        "phone",
        "property_ids",
    ),
    "helixis_vendors": (
        "name",
        "is_company",
        "first_name",
        "last_name",
        "company_name",
        "is_active",
        "category_id",
        "category_name",
        "phone",
        "email",
    ),
    "helixis_bills": (
        "vendor_id",
        "bill_date",
        "due_date",
        "total_amount",
        "is_paid",
        "reference_number",
        "work_order_id",
    ),
    "helixis_applicants": (
        "first_name",
        "last_name",
        "email",
        "phone",
        "status",
        "applicant_group_id",
        "property_id",
        "unit_id",
        "tenant_id",
    ),
    "helixis_gl_accounts": (
        "account_number",
        "name",
        "type",
        "sub_type",
        "is_active",
        "is_bank_account",
        "parent_gl_account_id",
    ),
    "helixis_work_orders": (
        "task_id",
        "property_id",
        "unit_id",
        "title",
        "status",
        "priority",
        "category_id",
        "assigned_to_user_id",
        "pms_updated_at",
    ),
    # Rentvine writes no tasks: these are the columns Buildium's own writer
    # sets (``db.repositories.entities.upsert_task``; test_pms_mirror reads it).
    "helixis_tasks": (
        "task_type",
        "title",
        "status",
        "priority",
        "due_date",
        "property_id",
        "unit_id",
        "assigned_to_user_id",
        "category_name",
    ),
}

#: Point-in-time snapshot keyed ``(company_id, lease_id)``: replaced whole, and
#: only from a complete read (an empty table reads "every lease is current").
BALANCE_COLUMNS: tuple[str, ...] = (
    "lease_id",
    "property_id",
    "unit_id",
    "total_balance",
    "balance_0_30",
    "balance_31_60",
    "balance_61_90",
    "balance_over_90",
)

#: Replaced per parent: ``table -> (parent column, columns)``. A parent the read
#: covered loses its old rows; a parent it did not cover keeps them.
PER_PARENT: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "helixis_lease_transactions": (
        "lease_id",
        ("buildium_id", "lease_id", "transaction_date", "transaction_type", "total_amount"),
    ),
    "helixis_bill_lines": (
        "bill_id",
        (
            "bill_id",
            "line_index",
            "property_id",
            "unit_id",
            "gl_account_id",
            "amount",
            "memo",
            "ledger_id",
            "ledger_type",
        ),
    ),
}

#: Lease-to-tenant links, keyed ``(company_id, lease_buildium_id,
#: tenant_buildium_id)``.
LINK_TABLE = "helixis_lease_tenants"

#: The order tables are written in: parents first, so a prune that removes a
#: parent's children runs after the children were refreshed.
WRITE_ORDER: tuple[str, ...] = (
    "helixis_properties",
    "helixis_units",
    "helixis_owners",
    "helixis_tenants",
    "helixis_vendors",
    "helixis_leases",
    LINK_TABLE,
    "helixis_lease_transactions",
    "helixis_lease_balances",
    "helixis_work_orders",
    "helixis_tasks",
    "helixis_bills",
    "helixis_bill_lines",
    "helixis_gl_accounts",
    "helixis_applicants",
)

#: A prune of a parent also removes these children (same window, same
#: transaction): ``parent -> [(child table, child's parent column)]``.
_CHILDREN: Mapping[str, tuple[tuple[str, str], ...]] = {
    "helixis_properties": (("helixis_units", "property_id"),),
    # A link outlives its lease otherwise: a delta only rewrites the links of
    # the leases it read, and every link reader reads a link as "lives there".
    "helixis_leases": (
        ("helixis_lease_transactions", "lease_id"),
        ("helixis_lease_tenants", "lease_buildium_id"),
    ),
    "helixis_bills": (("helixis_bill_lines", "bill_id"),),
}


@dataclass(frozen=True)
class TableRead:
    """What one table's read produced, mapped to mirror columns."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    #: The vendor confirmed the whole collection (``paging.Collection.complete``).
    complete: bool = False
    #: The read failed: the table is stale and nothing of it is written.
    error: str | None = None
    #: How long the read took, so a prune spares rows touched during it.
    read_seconds: float = 0.0
    #: Per-parent tables, and the links in a delta: the parents this read
    #: covered. A covered parent ends with exactly the rows given for it (none
    #: is a real answer); every other parent keeps what it had.
    parents: frozenset[int] = frozenset()
    #: The rows are the records CHANGED since the connector's watermark, not
    #: the whole collection. They are written as usual; ``complete`` then means
    #: the vendor confirmed every change was read, so the table is stamped
    #: fresh. Nothing is pruned from a delta: a row that did not change is not
    #: in it. The balance snapshot is replaced whole and refuses one.
    delta: bool = False
    #: Every id the vendor has for this collection now, from a COMPLETE
    #: id-only read (a connector's reconcile pass), or ``None``. A keyed table
    #: in a delta prunes what is outside it and outside ``rows``, with a full
    #: read's touch window and cascade. Only a delta of a keyed table takes
    #: one: a full read already names everything the vendor has.
    keep_ids: frozenset[int] | None = None


#: The spine interface: ``mirror table -> TableRead``.
MirrorRows = Mapping[str, TableRead]


@dataclass
class SyncResult:
    """What one write did, in terms an operator can act on."""

    company_id: str
    pms: str
    rows_by_table: dict[str, int] = field(default_factory=dict)
    #: Rows without a usable id. NEVER written: a made-up identity would
    #: collide with a real record on the next pass.
    skipped_no_id: dict[str, int] = field(default_factory=dict)
    #: Tables that are STALE this pass, with the reason (failed or incomplete).
    failed: dict[str, str] = field(default_factory=dict)
    #: Tables the vendor confirmed complete this pass.
    confirmed_complete: set[str] = field(default_factory=set)
    #: Rows deleted because the vendor no longer has them, by table.
    pruned: dict[str, int] = field(default_factory=dict)
    #: Tables this pass read by change only: their count in ``rows_by_table``
    #: is the rows that changed, never the table (:func:`stamp_freshness`).
    delta: set[str] = field(default_factory=set)

    @property
    def total_rows(self) -> int:
        return sum(self.rows_by_table.values())

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def fetched_nothing(self) -> bool:
        """Tables failed and none answered: a connection that does not work at
        all, which the cycle reports as an ERROR (Sentry's threshold). A table
        that answered with nothing, such as a quiet incremental read, is in
        ``rows_by_table`` with 0 and keeps this false."""
        answered = [table for table in self.rows_by_table if table not in self.failed]
        return bool(self.failed) and not answered and self.total_rows == 0


def _deleted(status: Any) -> int:
    """asyncpg's ``"DELETE 3"`` as ``3``; anything unreadable as ``0``."""
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except ValueError:
        return 0


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _upsert_keyed(
    conn: Any, table: str, row: dict[str, Any], company_id: str, pms: str
) -> None:
    """One row on ``(company_id, buildium_id)``, writing only the columns the
    row carries. Names come from :data:`KEYED_COLUMNS`, never from the row."""
    columns = [c for c in KEYED_COLUMNS[table] if c in row]
    all_cols = ("buildium_id", "account_id", "company_id", "pms", *columns, "raw")
    values: list[Any] = [_as_int(row["buildium_id"]), 0, company_id, pms]
    values += [row[c] for c in columns]
    values.append(json.dumps(row.get("raw", {}), default=str))
    placeholders = [f"${i}" for i in range(1, len(all_cols) + 1)]
    placeholders[2] += "::uuid"
    placeholders[-1] += "::jsonb"
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in ("pms", *columns, "raw"))
    await conn.execute(
        f"INSERT INTO {table} ({', '.join(all_cols)}) "  # noqa: S608 — names are
        f"VALUES ({', '.join(placeholders)}) "  # module constants, never row data
        f"ON CONFLICT (company_id, buildium_id) DO UPDATE SET {updates}, updated_at = now()",
        *values,
    )


async def _write_keyed(
    pool: Any, table: str, read: TableRead, company_id: str, pms: str, result: SyncResult
) -> None:
    written = skipped = 0
    kept: list[int] = []
    async with pool.acquire() as conn:
        for row in read.rows:
            rid = _as_int(row.get("buildium_id"))
            if rid is None:
                skipped += 1
                continue
            await _upsert_keyed(conn, table, row, company_id, pms)
            written += 1
            kept.append(rid)
    result.rows_by_table[table] = written
    if skipped:
        result.skipped_no_id[table] = skipped
        logger.warning(
            "%s sync %s: %d of %d %s rows had no usable id and were SKIPPED",
            pms,
            company_id,
            skipped,
            len(read.rows),
            table,
        )
    if not read.complete:
        return
    result.confirmed_complete.add(table)
    if read.delta and read.keep_ids is None:
        # A delta names what changed, never what is gone.
        return
    if skipped or len(set(kept)) != len(kept):
        logger.warning(
            "%s sync %s: %s NOT pruned: %s",
            pms,
            company_id,
            table,
            "rows without an id" if skipped else "an id came back twice",
        )
        return
    keep = sorted({*kept, *(read.keep_ids or ())}) if read.delta else kept
    await _prune_keyed(pool, table, keep, read.read_seconds, company_id, pms, result)


async def _prune_keyed(
    pool: Any,
    table: str,
    keep: list[int],
    read_seconds: float,
    company_id: str,
    pms: str,
    result: SyncResult,
) -> None:
    """Delete this system's rows of ``table`` whose id is not in ``keep``,
    sparing any row touched since the read began, and their children."""
    window = read_seconds + PRUNE_TOUCH_MARGIN_S
    async with pool.acquire() as conn, conn.transaction():
        gone = await conn.execute(
            f"DELETE FROM {table} WHERE company_id = $1::uuid AND pms = $2 "  # noqa: S608
            "AND NOT (buildium_id = ANY($3::bigint[])) "
            "AND updated_at < now() - make_interval(secs => $4::double precision)",
            company_id,
            pms,
            keep,
            window,
        )
        result.pruned[table] = _deleted(gone)
        for child, parent_col in _CHILDREN.get(table, ()):
            touched = "AND updated_at < now() - make_interval(secs => $4::double precision)"
            if child in PER_PARENT:
                # Replaced per parent, so they carry no touch time of their own
                # worth trusting; the parent's window is the guard.
                touched = ""
            child_gone = await conn.execute(
                f"DELETE FROM {child} WHERE company_id = $1::uuid AND pms = $2 "  # noqa: S608
                f"AND NOT ({parent_col} = ANY($3::bigint[])) {touched}",
                company_id,
                pms,
                keep,
                *((window,) if touched else ()),
            )
            result.pruned[child] = result.pruned.get(child, 0) + _deleted(child_gone)


async def _write_balances(
    pool: Any, read: TableRead, company_id: str, pms: str, result: SyncResult
) -> None:
    """Replace the whole snapshot, or leave it alone. Only a complete read may
    replace it: a lease missing from the snapshot reads as "current"."""
    table = "helixis_lease_balances"
    if not read.complete:
        result.failed[table] = "incomplete read: the old snapshot stands"
        return
    rows = [r for r in read.rows if _as_int(r.get("lease_id")) is not None]
    if len(rows) != len(read.rows):
        result.failed[table] = "a balance came without a lease id: the old snapshot stands"
        return
    async with pool.acquire() as conn, conn.transaction():
        gone = await conn.execute(
            f"DELETE FROM {table} WHERE company_id = $1::uuid AND pms = $2 "  # noqa: S608
            "AND NOT (lease_id = ANY($3::bigint[]))",
            company_id,
            pms,
            [_as_int(r["lease_id"]) for r in rows],
        )
        for row in rows:
            await conn.execute(
                f"""
                INSERT INTO {table}
                    (company_id, account_id, pms, {", ".join(BALANCE_COLUMNS)}, raw)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb)
                ON CONFLICT (company_id, lease_id) DO UPDATE SET
                    pms             = EXCLUDED.pms,
                    property_id     = EXCLUDED.property_id,
                    unit_id         = EXCLUDED.unit_id,
                    total_balance   = EXCLUDED.total_balance,
                    balance_0_30    = EXCLUDED.balance_0_30,
                    balance_31_60   = EXCLUDED.balance_31_60,
                    balance_61_90   = EXCLUDED.balance_61_90,
                    balance_over_90 = EXCLUDED.balance_over_90,
                    snapshot_at     = now(),
                    raw             = EXCLUDED.raw
                """,  # noqa: S608 — names are module constants
                company_id,
                0,
                pms,
                *(row.get(c) for c in BALANCE_COLUMNS),
                json.dumps(row.get("raw", {}), default=str),
            )
    result.rows_by_table[table] = len(rows)
    result.confirmed_complete.add(table)
    result.pruned[table] = _deleted(gone)


async def _write_per_parent(
    pool: Any, table: str, read: TableRead, company_id: str, pms: str, result: SyncResult
) -> None:
    """Delete then insert each covered parent's rows, in one transaction. A
    parent the read did not cover keeps exactly what it had."""
    parent_col, columns = PER_PARENT[table]
    keyed = "buildium_id" in columns
    by_parent: dict[int, list[dict[str, Any]]] = {p: [] for p in read.parents}
    skipped = 0
    for row in read.rows:
        parent = _as_int(row.get(parent_col))
        if parent is None or parent not in by_parent:
            skipped += 1
            continue
        if keyed and _as_int(row.get("buildium_id")) is None:
            skipped += 1
            continue
        by_parent[parent].append(row)
    if keyed:
        # Lease transactions carry their own id: an upsert on it, as the
        # Buildium and Rentvine paths do, in case one moved between leases.
        cols: tuple[str, ...] = ("company_id", "pms", "account_id", *columns, "raw")
        updated = [c for c in ("pms", *columns, "raw") if c != "buildium_id"]
        conflict = (
            " ON CONFLICT (company_id, buildium_id) DO UPDATE SET "
            + ", ".join(f"{c} = EXCLUDED.{c}" for c in updated)
            + ", updated_at = now()"
        )
    else:
        cols = ("company_id", "pms", *columns)
        conflict = ""
    placeholders = [f"${i}" for i in range(1, len(cols) + 1)]
    placeholders[0] += "::uuid"
    if keyed:
        placeholders[-1] += "::jsonb"
    insert = (
        f"INSERT INTO {table} ({', '.join(cols)}) "  # noqa: S608 — module constants
        f"VALUES ({', '.join(placeholders)}){conflict}"
    )
    written = 0
    async with pool.acquire() as conn, conn.transaction():
        for parent, rows in by_parent.items():
            await conn.execute(
                f"DELETE FROM {table} WHERE company_id = $1::uuid AND pms = $2 "  # noqa: S608
                f"AND {parent_col} = $3",
                company_id,
                pms,
                parent,
            )
            for row in rows:
                values: list[Any] = [company_id, pms]
                if keyed:
                    values.append(0)
                values += [row.get(c) for c in columns]
                if keyed:
                    values.append(json.dumps(row.get("raw", {}), default=str))
                await conn.execute(insert, *values)
                written += 1
    result.rows_by_table[table] = written
    if skipped:
        result.skipped_no_id[table] = skipped
    if read.complete and not skipped:
        result.confirmed_complete.add(table)
    else:
        result.failed[table] = (
            "incomplete read: parents not covered keep their old rows"
            if not read.complete
            else f"{skipped} row(s) had no id or named a parent the read did not cover"
        )


_LINK_UPSERT = f"""
    INSERT INTO {LINK_TABLE}
        (company_id, lease_buildium_id, tenant_buildium_id, status,
         move_in_date, pms)
    VALUES ($1::uuid, $2, $3, $4, $5, $6)
    ON CONFLICT (company_id, lease_buildium_id, tenant_buildium_id)
    DO UPDATE SET
        status = EXCLUDED.status,
        move_in_date = COALESCE(EXCLUDED.move_in_date, {LINK_TABLE}.move_in_date),
        pms = EXCLUDED.pms,
        updated_at = now()
"""  # noqa: S608 — the name is a module constant


async def _write_links_delta(
    pool: Any, read: TableRead, company_id: str, pms: str, result: SyncResult
) -> None:
    """A delta's links, per lease. Each lease the delta read (``parents``)
    ends with exactly the links given for it, so a lease that no longer has
    a tenant loses its link; every other lease keeps its own."""
    by_lease: dict[int, list[tuple[int, dict[str, Any]]]] = {p: [] for p in read.parents}
    skipped = 0
    for row in read.rows:
        lease, tenant = (
            _as_int(row.get("lease_buildium_id")),
            _as_int(row.get("tenant_buildium_id")),
        )
        if lease is None or tenant is None or lease not in by_lease:
            skipped += 1
            continue
        by_lease[lease].append((tenant, row))
    written = pruned = 0
    async with pool.acquire() as conn, conn.transaction():
        for lease, links in by_lease.items():
            gone = await conn.execute(
                f"DELETE FROM {LINK_TABLE} WHERE company_id = $1::uuid AND pms = $2 "  # noqa: S608
                "AND lease_buildium_id = $3 AND NOT (tenant_buildium_id = ANY($4::bigint[]))",
                company_id,
                pms,
                lease,
                [tenant for tenant, _row in links],
            )
            pruned += _deleted(gone)
            for tenant, row in links:
                await conn.execute(
                    _LINK_UPSERT,
                    company_id,
                    lease,
                    tenant,
                    row.get("status"),
                    row.get("move_in_date"),
                    pms,
                )
                written += 1
    result.rows_by_table[LINK_TABLE] = written
    result.pruned[LINK_TABLE] = pruned
    if skipped:
        result.skipped_no_id[LINK_TABLE] = skipped
    if read.complete and not skipped:
        result.confirmed_complete.add(LINK_TABLE)
    else:
        result.failed[LINK_TABLE] = (
            "incomplete read: leases not covered keep their old links"
            if not read.complete
            else f"{skipped} link(s) had no id or named a lease the read did not cover"
        )


async def _write_links(
    pool: Any, read: TableRead, company_id: str, pms: str, result: SyncResult
) -> None:
    """Upsert the links; from a complete read, remove the links it no longer
    lists. ``move_in_date`` is only ever filled in, never blanked."""
    if read.delta:
        await _write_links_delta(pool, read, company_id, pms, result)
        return
    links: list[tuple[int, int, dict[str, Any]]] = []
    skipped = 0
    for row in read.rows:
        lease, tenant = (
            _as_int(row.get("lease_buildium_id")),
            _as_int(row.get("tenant_buildium_id")),
        )
        if lease is None or tenant is None:
            skipped += 1
            continue
        links.append((lease, tenant, row))
    async with pool.acquire() as conn:
        for lease, tenant, row in links:
            await conn.execute(
                _LINK_UPSERT,
                company_id,
                lease,
                tenant,
                row.get("status"),
                row.get("move_in_date"),
                pms,
            )
    result.rows_by_table[LINK_TABLE] = len(links)
    if skipped:
        result.skipped_no_id[LINK_TABLE] = skipped
    if not read.complete or skipped:
        return
    result.confirmed_complete.add(LINK_TABLE)
    window = read.read_seconds + PRUNE_TOUCH_MARGIN_S
    async with pool.acquire() as conn, conn.transaction():
        gone = await conn.execute(
            f"""
            DELETE FROM {LINK_TABLE} lt
             WHERE lt.company_id = $1::uuid
               AND lt.pms = $2
               AND NOT EXISTS (
                     SELECT 1 FROM unnest($3::bigint[], $4::bigint[]) AS keep(l, t)
                      WHERE keep.l = lt.lease_buildium_id
                        AND keep.t = lt.tenant_buildium_id)
               AND lt.updated_at < now() - make_interval(secs => $5::double precision)
            """,  # noqa: S608 — the name is a module constant
            company_id,
            pms,
            [lease for lease, _t, _r in links],
            [tenant for _l, tenant, _r in links],
            window,
        )
    result.pruned[LINK_TABLE] = _deleted(gone)


async def write_mirror(pool: Any, company_id: str, pms: str, rows: MirrorRows) -> SyncResult:
    """Write one pass's reads into the mirror. Never stamps freshness: the
    caller does that with :func:`stamp_freshness`, from the same result."""
    if pms not in PMS or PMS[pms].live_tools:
        raise ValueError(f"{pms!r} is not a system read from the mirror only")
    if not company_id:
        raise ValueError("a mirror write needs a company")
    unknown = sorted(set(rows) - set(WRITE_ORDER))
    if unknown:
        raise ValueError(f"not mirror tables this writer knows: {unknown}")
    for table, asked in rows.items():
        # A connector bug, refused before anything is written.
        if asked.delta and table == "helixis_lease_balances":
            raise ValueError("the balance snapshot is replaced whole; a delta cannot replace it")
        if asked.keep_ids is not None and not (asked.delta and table in KEYED_COLUMNS):
            raise ValueError(f"{table}: keep_ids goes with a delta of a keyed table only")
    result = SyncResult(company_id=company_id, pms=pms)
    for table in WRITE_ORDER:
        read = rows.get(table)
        if read is None:
            continue
        if read.error is not None:
            result.failed[table] = read.error
            continue
        try:
            if table in KEYED_COLUMNS:
                await _write_keyed(pool, table, read, company_id, pms, result)
            elif table == "helixis_lease_balances":
                await _write_balances(pool, read, company_id, pms, result)
            elif table in PER_PARENT:
                await _write_per_parent(pool, table, read, company_id, pms, result)
            else:
                await _write_links(pool, read, company_id, pms, result)
        except Exception as exc:  # noqa: BLE001 — one table's failure is that table's
            logger.warning("%s sync %s: writing %s failed", pms, company_id, table, exc_info=True)
            result.failed[table] = f"write failed: {type(exc).__name__}"
            continue
        if read.delta:
            result.delta.add(table)
        if not read.complete and table not in result.failed:
            result.failed[table] = "incomplete read: written, not stamped, not pruned"
    return result


async def stamp_freshness(
    pool: Any, result: SyncResult, stages: Mapping[str, str], *, source: str
) -> list[str]:
    """Stamp ``helixis_mirror_sync_state`` for each stage this pass refreshed.

    One stamp per STAGE, and only when every table mapped to it was reached
    and is clean: a stage shared by two tables is never called fresh from
    whichever came last (Rentvine's ``_record_freshness``). Returns the stages
    stamped. Best-effort, like ``record_sync`` itself.

    ⚠ A delta stamps the time and keeps the stored count. Its count is the rows
    that changed, and ``row_count`` is read as the table's size: a properties
    stage stamped 0 by a quiet pass is how ``mirror_tools`` would tell a
    customer their account has no properties (``_empty_after_a_successful_sync``).
    ``record_sync`` keeps the last full read's count when given ``None``.
    """
    from helixis.db.repositories.mirror_sync_state import record_sync

    tables_by_stage: dict[str, list[str]] = {}
    for table, stage in stages.items():
        tables_by_stage.setdefault(stage, []).append(table)
    stamped: list[str] = []
    for stage, tables in tables_by_stage.items():
        if any(t in result.failed or t not in result.rows_by_table for t in tables):
            continue
        await record_sync(
            pool,
            company_id=result.company_id,
            table_name=stage,
            row_count=(
                None if any(t in result.delta for t in tables) else result.rows_by_table[tables[0]]
            ),
            source=source,
        )
        stamped.append(stage)
    return stamped


__all__ = [
    "BALANCE_COLUMNS",
    "KEYED_COLUMNS",
    "LINK_TABLE",
    "PER_PARENT",
    "PRUNE_TOUCH_MARGIN_S",
    "WRITE_ORDER",
    "MirrorRows",
    "SyncResult",
    "TableRead",
    "stamp_freshness",
    "write_mirror",
]
