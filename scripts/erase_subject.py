#!/usr/bin/env python3
"""Erase one person's Helixis-authored data, and report what survives.

    python scripts/erase_subject.py --company <uuid> --type tenant \
        --buildium-id 115937 --email someone@example.com --dry-run
    python scripts/erase_subject.py --company <uuid> --type tenant --email … --confirm

⚠ Prints the SURVIVING half as loudly as the deleted half, and exits non-zero
when anything survives. An erasure that quietly reports success while the
subject is still in the mirror would tell an operator a legal request was
satisfied when it was not — and they are the one who has to answer for that.
The Buildium mirror ALWAYS survives (it re-syncs), so a non-zero exit is the
normal outcome, not an error: it means "your part is done, the controller's
part is not".
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from helixis.privacy import SUBJECT_TYPES, Subject, erase_subject  # noqa: E402
from scripts._console import force_utf8_console  # noqa: E402


async def _run(args: argparse.Namespace) -> int:
    from helixis.db.engine import get_pool, init_pool

    subject = Subject(
        entity_type=args.type,
        buildium_id=args.buildium_id,
        email=args.email,
    )

    if args.dry_run:
        print(
            f"DRY RUN — would erase {subject.entity_type} "
            f"(buildium_id={subject.buildium_id}, email={subject.email or '—'}) "
            f"in company {args.company}"
        )
        print("Re-run with --confirm to execute. Nothing was changed.")
        return 0

    await init_pool()
    memory = None
    if not args.skip_memory:
        from helixis.memory.manager import get_memory_manager

        memory = get_memory_manager()
        if memory is None:
            print("⚠ memory backend unavailable — agent memory will NOT be erased.")

    report = await erase_subject(get_pool(), args.company, subject, memory=memory)

    print(f"\nErased ({sum(report.erased.values())} rows):")
    for table, count in sorted(report.erased.items()):
        print(f"  {table:42} {count}")

    if report.failed:
        print("\n⚠ FAILED — these were not erased and must be retried:")
        for table, err in sorted(report.failed.items()):
            print(f"  {table:42} {err}")

    if report.not_erasable_here:
        print("\n⚠ STILL PRESENT — not erasable from here:")
        for table, reason in sorted(report.not_erasable_here.items()):
            print(f"  {table}\n      {reason}")

    print(f"\ncomplete={report.complete}")
    return 0 if report.complete else 2


def main() -> int:
    force_utf8_console()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--company", required=True, help="company_id (uuid)")
    p.add_argument("--type", required=True, choices=SUBJECT_TYPES)
    p.add_argument("--buildium-id", type=int, default=None)
    p.add_argument("--email", default=None)
    p.add_argument("--skip-memory", action="store_true", help="do not touch agent memory")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--confirm", action="store_true", help="actually delete")
    args = p.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
