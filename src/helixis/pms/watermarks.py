"""Incremental-sync watermarks, one per (company, resource).

A connector polls "everything modified since the watermark". Three rules keep
that from losing a row:

* **Advance only after a clean pass.** The caller advances a resource's
  watermark only when its read was complete and its write succeeded. A failed
  or incomplete pass leaves it where it was, so the next pass re-reads.
* **Read behind it.** :func:`since` starts each read :data:`LAG` before the
  watermark: vendors stamp a row's modified time when the change BEGINS, so a
  row committed slowly can carry a time already behind a pass that finished.
  Propertyware's date filters are inclusive at both ends, so the overlap is
  read twice, and :func:`dedupe` drops the repeats on (id, modified time).
* **Never backwards.** :meth:`Watermark.advance` keeps the later of the stored
  and the new value, so two overlapping passes cannot rewind each other.

⚠ Its own table, not a column on ``helixis_mirror_sync_state``: that table is
in the Data Analyst's ``ALLOWED_TABLES`` and the analyst reads its columns
live, so a new column there would change every Buildium customer's analyst
schema (ADR 0010 F-4; design/connector-shared-layer.md).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

#: How far behind the watermark each read starts (Propertyware: windows
#: overlap by 10 minutes; design/propertyware-connector.md).
LAG = timedelta(minutes=10)

TABLE = "helixis_pms_sync_watermarks"


class Watermark:
    """One company's watermark for one resource (``"Tenants"``,
    ``"leases/charges"``: the connector names it)."""

    def __init__(self, pool: Any, company_id: str, resource: str):
        if not company_id or not resource:
            raise ValueError("a watermark needs a company and a resource")
        self._pool = pool
        self._company_id = company_id
        self._resource = resource

    async def read(self) -> datetime | None:
        """The stored watermark, or ``None`` before the first clean pass."""
        value = await self._pool.fetchval(
            f"SELECT watermark FROM {TABLE} WHERE company_id = $1::uuid AND resource = $2",
            self._company_id,
            self._resource,
        )
        return value if isinstance(value, datetime) else None

    async def advance(self, to: datetime) -> None:
        """Move the watermark to ``to``, never backwards. Call it only after a
        clean pass of this resource."""
        if to.tzinfo is None:
            raise ValueError("a watermark must carry a time zone")
        await self._pool.execute(
            f"""
            INSERT INTO {TABLE} (company_id, resource, watermark, updated_at)
            VALUES ($1::uuid, $2, $3, now())
            ON CONFLICT (company_id, resource) DO UPDATE
               SET watermark = GREATEST({TABLE}.watermark, EXCLUDED.watermark),
                   updated_at = now()
            """,
            self._company_id,
            self._resource,
            to,
        )


def since(watermark: datetime | None, lag: timedelta = LAG) -> datetime | None:
    """Where the next read starts: :data:`LAG` before the watermark, or
    ``None`` (read everything) before the first clean pass."""
    return None if watermark is None else watermark - lag


def _newer(candidate: Any, current: Any) -> bool:
    """Whether ``candidate`` is a later modified time than ``current``. A
    value that cannot be compared counts as later (the later row wins)."""
    if candidate is None:
        return False
    if current is None:
        return True
    try:
        return bool(candidate > current)
    except TypeError:
        return True


def dedupe(
    rows: Iterable[dict[str, Any]], *, id_key: str, modified_key: str
) -> list[dict[str, Any]]:
    """One row per id: the newest by ``modified_key``, in first-seen order.

    The overlap :func:`since` reads twice comes back as exact repeats, and a
    row changed during the read can come back as two versions. Keeping the
    newest, rather than whichever arrived last, means an older version listed
    after a newer one can never overwrite it. Rows without an id pass through
    for the mapper to refuse.
    """
    best: dict[Any, dict[str, Any]] = {}
    order: list[Any] = []
    no_id: list[dict[str, Any]] = []
    for row in rows:
        rid = row.get(id_key)
        if rid is None:
            no_id.append(row)
            continue
        current = best.get(rid)
        if current is None:
            best[rid] = row
            order.append(rid)
        elif _newer(row.get(modified_key), current.get(modified_key)):
            best[rid] = row
    return [best[rid] for rid in order] + no_id


__all__ = ["LAG", "TABLE", "Watermark", "dedupe", "since"]
