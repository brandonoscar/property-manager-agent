#!/usr/bin/env python3
"""Mem0 storage migration: ``user_id`` → ``company_id`` (intentionally a no-op).

Phase 1's brief asks for a one-shot migration that re-writes every memory
from ``filters={"user_id": ...}`` to ``filters={"company_id": ...}``.

After reading the codebase + the Mem0 OSS API docs, this migration is a
**no-op** by design. See ``docs/migrations/2026-04-mem0-company-scope.md``
for the full rationale. Short version:

  1. Open-source Mem0 has no ``org_id`` dimension. Its three scope keys are
     ``user_id``, ``agent_id``, ``run_id``. There is no ``company_id``.
  2. The codebase uses the convention ``filters={"user_id": company_id}`` —
     i.e. the Mem0-internal partition key is ``user_id`` but the value
     stored is the Helixis company id. This already gives us the desired
     "every user at the same company shares one memory pool" behaviour.
  3. Renaming the partition key from ``user_id`` to ``company_id`` would
     require a custom Mem0 backend; OSS Mem0 does not support arbitrary
     filter dimensions on its ``Memory`` / ``AsyncMemory`` classes.
  4. ``docs/prompts/01-memory.md`` (the design spec already in the repo)
     confirms this is the right design and shows ``filters={"user_id": company_id}``
     as the v3-correct invocation.

This script exists so the migration trail is auditable. Running it (with or
without ``--dry-run``) will report "no migration needed" and exit cleanly.

Usage:
    python scripts/migrate_mem0_to_company_scope.py --dry-run
    python scripts/migrate_mem0_to_company_scope.py
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Mem0 storage migration (no-op by design — see "
            "docs/migrations/2026-04-mem0-company-scope.md)"
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing (no effect — this script is a no-op).",
    )
    args = parser.parse_args(argv)

    print("Mem0 storage migration: no-op by design.")
    print()
    print("The codebase already partitions Mem0 storage by company via")
    print('  filters={"user_id": company_id}')
    print("which is the v3-correct invocation. Open-source Mem0 has no org_id")
    print("dimension; renaming the partition key to 'company_id' would require")
    print("a custom Mem0 backend, which is out of scope for Phase 1.")
    print()
    print("See docs/migrations/2026-04-mem0-company-scope.md for the rationale,")
    print("the conditions under which a real migration would become necessary,")
    print("and the rollback plan if those conditions arise.")
    print()
    if args.dry_run:
        print("(dry-run — same as a real run, since there is nothing to do.)")
    print("Exit: 0 (no rows touched)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
