#!/usr/bin/env python3
"""Build the schema on a NEW Supabase project, in the order that works.

The migrations in ``supabase/migrations/`` were applied to the original
deployment one at a time over months, and two of them do not replay on an
empty database:

* ``0002_phase1_memory_rls.sql`` is historical and says so in its header
  ("DO NOT RE-RUN"). 0011 and 0020 replaced it.
* ``src/helixis/db/schema/helixis_entities.sql`` lives outside the folder and
  must run before ``0020``, which turns on row-level security for its tables.

This script applies everything else in numeric order, each file as one
transaction, and stops at the first error. It refuses a database that already
has ``public.companies`` unless you pass ``--force``: it is for a first
install, not for upgrades.

The connection string comes from ``HELIXIS_SUPABASE__DB_URL`` (never a
command-line argument, so it stays out of your shell history and ``ps``).

    python scripts/apply_schema.py --dry-run     # print the order
    python scripts/apply_schema.py               # apply it

Mem0 creates its memory tables the first time the app stores a memory. Run
``python scripts/apply_schema.py --only 0025`` after that first boot so those
tables get row-level security too (``scripts/rls_guard.py`` checks).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO / "supabase" / "migrations"
ENTITIES = REPO / "src" / "helixis" / "db" / "schema" / "helixis_entities.sql"

#: Historical files that must not run on a new database (see the docstring).
SKIP = ("0002_",)
#: The out-of-folder schema file goes in just before this migration.
ENTITIES_BEFORE = "0020_"


def install_order() -> list[Path]:
    """Every file a new database needs, in the order it needs them."""
    order: list[Path] = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name.startswith(SKIP):
            continue
        if path.name.startswith(ENTITIES_BEFORE):
            order.append(ENTITIES)
        order.append(path)
    if ENTITIES not in order:
        raise SystemExit(f"no {ENTITIES_BEFORE}* migration found to place {ENTITIES.name}")
    return order


async def apply(files: list[Path], *, force: bool) -> int:
    import asyncpg

    url = os.environ.get("HELIXIS_SUPABASE__DB_URL", "")
    if not url:
        print("Set HELIXIS_SUPABASE__DB_URL to your Supabase database URL first.", file=sys.stderr)
        return 2
    conn = await asyncpg.connect(url)
    try:
        existing = await conn.fetchval("select to_regclass('public.companies') is not null")
        if existing and not force:
            print(
                "This database already has public.companies, so it is not new. "
                "Pass --force to apply anyway (only the files you name with --only "
                "are safe to re-run).",
                file=sys.stderr,
            )
            return 3
        for i, path in enumerate(files, 1):
            rel = path.relative_to(REPO)
            try:
                # One simple-query message: Postgres runs the whole file as a
                # single implicit transaction unless the file manages its own.
                await conn.execute(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001 — report which file and stop
                print(f"FAILED at {rel} ({i}/{len(files)}): {exc}", file=sys.stderr)
                return 1
            print(f"applied {rel}")
    finally:
        await conn.close()
    print(f"done: {len(files)} files")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="print the order and stop")
    parser.add_argument("--force", action="store_true", help="apply to a database that is not new")
    parser.add_argument(
        "--only", action="append", default=[], help="apply only files whose name starts with this"
    )
    args = parser.parse_args(argv)
    files = install_order()
    if args.only:
        files = [f for f in files if f.name.startswith(tuple(args.only))]
        args.force = True
        if not files:
            print(f"no file matches {args.only}", file=sys.stderr)
            return 2
    if args.dry_run:
        for path in files:
            print(path.relative_to(REPO))
        return 0
    return asyncio.run(apply(files, force=args.force))


if __name__ == "__main__":
    sys.exit(main())
