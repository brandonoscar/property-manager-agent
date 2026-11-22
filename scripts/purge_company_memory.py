#!/usr/bin/env python3
"""Right-to-erasure: purge ALL memory for a company (SOC2 / GDPR / CCPA).

Admin-only, manual, and audited. There is intentionally NO public endpoint for
this — erasing a tenant's memory is a deliberate, controlled operation, so it
lives as a script an operator runs, not an API anyone can hit.

    python scripts/purge_company_memory.py <company_id> \
        [--requested-by <admin_auth_users_uuid>] [--yes]

Reads HELIXIS_* from the environment the same way the app does (needs the
Gemini key + Supabase DB URL so the real Mem0/pgvector store is reachable).
Prompts for confirmation unless --yes is passed. Writes one `delete_company`
audit row (attributed to --requested-by when given).
"""

from __future__ import annotations

import argparse
import asyncio
import sys


async def _purge(company_id: str, requested_by: str) -> int:
    from helixis.memory.manager import MemoryManager
    from helixis.settings import Settings

    mgr = MemoryManager(Settings())
    await mgr.initialize()
    if not mgr.available:
        print(
            "Memory not available — check HELIXIS_LLM__GOOGLE_API_KEY + HELIXIS_SUPABASE__DB_URL.",
            file=sys.stderr,
        )
        return -1

    existing = await mgr.get_all(company_id=company_id)
    print(f"Company {company_id}: {len(existing)} memories to erase.")
    if not existing:
        return 0  # nothing to erase — skip the delete call (and its audit row)
    return await mgr.delete_company_memory(company_id, requested_by=requested_by)


def main() -> None:
    ap = argparse.ArgumentParser(description="Erase ALL memory for a company (right-to-erasure).")
    ap.add_argument("company_id", help="public.companies(id) of the tenant to erase")
    ap.add_argument(
        "--requested-by",
        default="",
        help="admin auth.users(id) UUID recorded on the audit row",
    )
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    if not args.yes:
        ans = input(
            f"Erase ALL memory for company {args.company_id}? This cannot be undone. [y/N] "
        )
        if ans.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return

    count = asyncio.run(_purge(args.company_id, args.requested_by))
    if count < 0:
        sys.exit(1)
    print(f"Erased {count} memories for company {args.company_id}.")


if __name__ == "__main__":
    main()
