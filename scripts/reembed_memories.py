#!/usr/bin/env python3
"""Re-embed the mem0 pgvector tables after an embedder/dimension change.

One-shot migration for the Gemini→Voyage embedder swap (gotcha 18): the live
``helixis_memories`` (+ ``helixis_memories_entities``) tables hold vectors from
``gemini-embedding-001`` at 1536 dims; the new embedder is ``voyage-4-lite`` at
1024. Old vectors are not just stale — they're geometrically meaningless to the
new embedder AND the wrong column width, so the tables must be rebuilt:

  1. read every row (id, payload) from both tables
  2. back up rows to a JSON file (the restore path if anything goes wrong)
  3. re-embed each payload's ``data`` text with the NEW embedder (batched)
  4. drop the tables, re-init MemoryManager (mem0 recreates ``helixis_memories``
     at the new dimension; RLS re-enabled by the manager — the entities sibling
     is recreated by explicit DDL mirroring mem0's shape, since mem0 only
     creates it lazily on first entity write)
  5. reinsert all rows with their new vectors
  6. verify counts + run the manager's health_probe

Idempotent-ish: rerunning after success re-embeds the same texts into the same
tables (a no-op semantically, pennies financially).

Run via the "Memory re-embed (Voyage migration)" workflow_dispatch, or locally:

    HELIXIS_SUPABASE__DB_URL=...       \
    HELIXIS_LLM__VOYAGE_API_KEY=...    \
    HELIXIS_LLM__ANTHROPIC_API_KEY=... \
    python scripts/reembed_memories.py [--dry-run] [--backup PATH]

``--dry-run`` reads + backs up + embeds a 3-row sample, but writes NOTHING.
Requires the ``[memory]`` extra (psycopg + mem0 for the re-init step).
Exit 0 = migrated + verified. Exit 1 = failed (tables untouched unless the
log says otherwise; the backup file always exists once reading succeeded).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

# Allow running from the repo root without installation tweaks.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from helixis.memory.voyage_embedder import VoyageEmbedder  # noqa: E402
from helixis.settings import Settings  # noqa: E402

TABLES = ("helixis_memories", "helixis_memories_entities")


class _EmbedderConfig:
    """Duck-typed BaseEmbedderConfig — enough for VoyageEmbedder."""

    def __init__(self, model: str, api_key: str, embedding_dims: int):
        self.model = model
        self.api_key = api_key
        self.embedding_dims = embedding_dims


def _read_rows(conn: Any, table: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass(%s) IS NOT NULL",
            (f"public.{table}",),
        )
        if not cur.fetchone()[0]:
            print(f"[reembed] table {table} does not exist — skipping")
            return []
        cur.execute(f"SELECT id, payload FROM {table}")  # noqa: S608 — fixed table names
        return [{"id": str(r[0]), "payload": r[1]} for r in cur.fetchall()]


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in vec) + "]"


def load_rows_from_backup(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Load a prior run's backup JSON as the row source (disaster recovery).

    Shape is exactly what this script writes: {table: [{id, payload}, ...]}.
    Payloads may arrive as JSON strings (psycopg's jsonb round-trips through
    ``json.dumps(default=str)``) — normalize to dicts. Unknown tables in the
    file are ignored; known tables missing from the file become empty."""
    raw = json.loads(Path(path).read_text())
    out: dict[str, list[dict[str, Any]]] = {}
    for table in TABLES:
        rows = raw.get(table, [])
        normalized = []
        for row in rows:
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            normalized.append({"id": str(row["id"]), "payload": payload})
        out[table] = normalized
    return out


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="read + sample-embed only")
    parser.add_argument(
        "--backup",
        default="reembed_backup.json",
        help="path for the pre-migration row backup (JSON)",
    )
    parser.add_argument(
        "--restore-from",
        default="",
        help=(
            "disaster recovery: load rows from a prior run's backup JSON "
            "instead of the live tables (use when the tables were dropped "
            "by a partially-failed run)"
        ),
    )
    args = parser.parse_args()

    settings = Settings()
    db_url = settings.supabase.db_url
    if not db_url:
        print("[reembed] FAIL: HELIXIS_SUPABASE__DB_URL not set")
        return 1
    if not settings.llm.voyage_api_key:
        print("[reembed] FAIL: HELIXIS_LLM__VOYAGE_API_KEY not set")
        return 1

    dims = settings.memory.embedding_dims
    model = settings.memory.embedding_model
    embedder = VoyageEmbedder(_EmbedderConfig(model, settings.llm.voyage_api_key, dims))
    print(f"[reembed] target embedder: {model} @ {dims} dims")

    import psycopg

    rows_by_table: dict[str, list[dict[str, Any]]] = {}
    if args.restore_from:
        rows_by_table = load_rows_from_backup(Path(args.restore_from))
        for table in TABLES:
            print(f"[reembed] {table}: {len(rows_by_table[table])} rows (from backup)")
        if not any(rows_by_table.values()):
            print(f"[reembed] FAIL: restore file {args.restore_from} contains no rows")
            return 1
    else:
        with psycopg.connect(db_url) as conn:
            for table in TABLES:
                rows_by_table[table] = _read_rows(conn, table)
                print(f"[reembed] {table}: {len(rows_by_table[table])} rows")

    total = sum(len(v) for v in rows_by_table.values())
    if total == 0:
        # Refuse to "migrate" nothing — run #5 hit exactly this: the tables
        # were already dropped by an earlier failed attempt, the live read
        # found 0 rows, and completing would have looked like success while
        # silently skipping the restore.
        print(
            "[reembed] FAIL: 0 rows to migrate — the live tables are empty or "
            "missing. If a previous run dropped them, re-run the workflow "
            "with restore_run_id set to the run holding the backup artifact "
            "(the 314-row pre-migration backup is run 29952849913)."
        )
        return 1
    backup_path = Path(args.backup)
    backup_path.write_text(json.dumps(rows_by_table, default=str), encoding="utf-8")
    print(f"[reembed] backup written: {backup_path} ({total} rows)")

    if args.dry_run:
        sample = [r["payload"].get("data", "") for rows in rows_by_table.values() for r in rows][:3]
        if sample:
            vecs = embedder.embed_batch(sample, "add")
            ok = all(len(v) == dims for v in vecs)
            print(f"[reembed] dry-run: embedded {len(vecs)} sample rows, dims ok={ok}")
            if not ok:
                return 1
        print("[reembed] dry-run complete — no writes performed")
        return 0

    # Embed everything BEFORE dropping anything, so an embed failure leaves
    # the live tables untouched.
    vectors_by_table: dict[str, list[list[float]]] = {}
    for table, rows in rows_by_table.items():
        texts = [str(r["payload"].get("data", "")) for r in rows]
        vectors = embedder.embed_batch(texts, "add") if texts else []
        bad = [i for i, v in enumerate(vectors) if len(v) != dims]
        if bad:
            print(f"[reembed] FAIL: {table} rows {bad[:5]} returned wrong dims")
            return 1
        vectors_by_table[table] = vectors
        # Stamp every migrated row with the model that embedded it — the
        # drift guard health_probe checks this, and it makes the migration
        # self-documenting (a future model change is detectable per row).
        for row in rows:
            row["payload"]["embedding_model"] = model
            row["payload"]["embedding_dims"] = dims
        print(f"[reembed] {table}: embedded {len(vectors)} rows (stamped {model}@{dims})")

    # Point of no return: drop, recreate, reinsert.
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            for table in TABLES:
                cur.execute(f"DROP TABLE IF EXISTS {table}")  # noqa: S608
        conn.commit()
    print("[reembed] old tables dropped")

    from helixis.memory.manager import MemoryManager

    manager = MemoryManager(settings)
    await manager.initialize()
    if not manager.available:
        print(
            "[reembed] FAIL: MemoryManager did not initialize — tables are "
            f"dropped but data is safe in {backup_path}; fix init and rerun"
        )
        return 1

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            # Create BOTH tables explicitly (mirroring mem0's 3-column
            # pgvector shape). mem0 2.0.12 creates its collection LAZILY on
            # first operation — NOT at init — so relying on
            # manager.initialize() left helixis_memories missing and run #5
            # died at the count verify. Explicit DDL is idempotent and makes
            # this script self-sufficient; mem0's own _ensure_collection is
            # a no-op against an existing table.
            for table in TABLES:
                cur.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} "  # noqa: S608 — fixed names
                    f"(id UUID PRIMARY KEY, vector vector({dims}), payload JSONB)"
                )
                cur.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")  # noqa: S608
            for table in TABLES:
                rows = rows_by_table[table]
                vectors = vectors_by_table[table]
                for row, vec in zip(rows, vectors, strict=True):
                    cur.execute(
                        f"INSERT INTO {table} (id, vector, payload) "  # noqa: S608
                        "VALUES (%s, %s::vector, %s) ON CONFLICT (id) DO UPDATE "
                        "SET vector = EXCLUDED.vector, payload = EXCLUDED.payload",
                        (row["id"], _vector_literal(vec), json.dumps(row["payload"])),
                    )
        conn.commit()

    # Verify: counts match and the read path works end-to-end.
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            for table in TABLES:
                cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608
                count = cur.fetchone()[0]
                expected = len(rows_by_table[table])
                print(f"[reembed] {table}: {count}/{expected} rows after migration")
                if count != expected:
                    print("[reembed] FAIL: row count mismatch")
                    return 1

    ok, detail = await manager.health_probe()
    print(f"[reembed] health_probe: ok={ok} ({detail})")
    if not ok:
        return 1

    print(f"[reembed] PASS — {total} rows re-embedded at {dims} dims on {model}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
