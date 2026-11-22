#!/usr/bin/env python3
"""RLS guard — fail if any ``public`` table is missing Row Level Security.

This is the CI counterpart to Supabase's database linter ``0013_rls_disabled_in
_public``. Forgotten RLS on a multi-tenant ``public`` table is the #1 cross-
tenant leak vector (risk #1): the table is exposed to the ``anon`` +
``authenticated`` roles, so anyone with the public anon key can read every
tenant's rows via PostgREST. It already bit us — Mem0's runtime tables
(``helixis_memories`` et al.) shipped RLS-off, and ``buildium_events`` was only
ever RLS-enabled in a migration that never ran.

Two independent checks:

  * STATIC (always runs, no DB): every ``create table … public.X`` in
    supabase/migrations/ must have a matching ``enable row level security`` for
    X somewhere in the migration set, or be on ``SERVICE_ONLY_ALLOWLIST``.
    Catches the ``buildium_events`` class — a committed table whose RLS the
    migrations never reproduce. CANNOT see library-created tables (Mem0), which
    is what the live check is for.

  * LIVE (runs only when ``HELIXIS_SUPABASE__DB_URL`` is set): queries
    ``pg_tables.rowsecurity`` and fails on any ``public`` table with RLS off
    that isn't allow-listed. Catches the Mem0 class. Wire this into a scheduled
    workflow (CI on a PR has no DB egress).

Exit 0 = clean; exit 1 = a finding. ``--json`` prints machine-readable findings.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATIONS_GLOB = os.path.join(ROOT, "supabase", "migrations", "*.sql")

# Tables that are intentionally reachable ONLY via the service-role pool (which
# bypasses RLS). They should still have RLS ENABLED (deny-all), so they pass the
# rowsecurity check; this allowlist is only for the rare case where a table is
# legitimately RLS-off. Keep it empty unless there is a documented reason.
SERVICE_ONLY_ALLOWLIST: frozenset[str] = frozenset()

# Extension-owned public tables that ship RLS-off and hold NO tenant data.
# PostGIS's ``spatial_ref_sys`` is a global spatial-reference lookup created by
# ``create extension postgis`` (migration 0053, for maps) — it cannot be
# meaningfully RLS'd and carries no tenant rows, so it's excluded from the LIVE
# rowsecurity check. It never appears in the STATIC migration scan because
# PostGIS creates it via CREATE EXTENSION, not a ``create table`` statement, so
# this only affects the live path.
_EXTENSION_OWNED_RLS_OFF: frozenset[str] = frozenset({"spatial_ref_sys"})

# The live check tolerates both the service-only allowlist and extension-owned
# reference tables; the static check keeps the stricter (empty) allowlist.
LIVE_ALLOWLIST: frozenset[str] = SERVICE_ONLY_ALLOWLIST | _EXTENSION_OWNED_RLS_OFF

# Historical migrations kept in the tree for the audit trail but never applied
# (e.g. 0002 — see its header). Their CREATE / ENABLE RLS statements must NOT
# count: 0002 "enables RLS" on buildium_events but never ran, which is precisely
# how that table shipped RLS-off. Files carrying this marker are skipped.
_DEAD_MARKER = "DO NOT RE-RUN"

_CREATE_RE = re.compile(
    r"create\s+table\s+(?:if\s+not\s+exists\s+)?(?:public\.)?([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
_ENABLE_RLS_RE = re.compile(
    r"alter\s+table\s+(?:if\s+exists\s+)?(?:public\.)?([a-z_][a-z0-9_]*)\s+"
    r"enable\s+row\s+level\s+security",
    re.IGNORECASE,
)


_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_sql_comments(text: str) -> str:
    """Remove ``--`` line comments and ``/* */`` blocks.

    Comments routinely contain prose like "create table if not exists" (e.g.
    the idempotency notes in 0009/0021/0022); without stripping them the create-
    table regex matches inside the comment and captures the keyword ``if``.
    """
    return _LINE_COMMENT_RE.sub("", _BLOCK_COMMENT_RE.sub("", text))


def find_unprotected_tables(
    sql_texts: list[str],
    allowlist: frozenset[str] = SERVICE_ONLY_ALLOWLIST,
) -> list[str]:
    """Return tables created in the SQL set that never get RLS enabled.

    Pure function (no IO) so it can be unit-tested. ``sql_texts`` is the list of
    migration file contents. A table is "protected" if any text in the set
    enables RLS on it — RLS may land in a later migration than the create.
    """
    created: set[str] = set()
    rls_enabled: set[str] = set()
    for raw in sql_texts:
        if _DEAD_MARKER in raw.upper():
            continue  # historical, never-applied migration — ignore entirely
        text = _strip_sql_comments(raw)
        created.update(m.lower() for m in _CREATE_RE.findall(text))
        rls_enabled.update(m.lower() for m in _ENABLE_RLS_RE.findall(text))
    unprotected = created - rls_enabled - {t.lower() for t in allowlist}
    return sorted(unprotected)


def _read_migration_texts() -> list[str]:
    texts = []
    for path in sorted(glob.glob(MIGRATIONS_GLOB)):
        with open(path, encoding="utf-8") as fh:
            texts.append(fh.read())
    return texts


async def find_live_unprotected(
    db_url: str,
    allowlist: frozenset[str] = LIVE_ALLOWLIST,
) -> list[str]:
    """Query the live DB for public tables with RLS disabled (minus allowlist)."""
    import asyncpg

    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND rowsecurity = false"
        )
    finally:
        await conn.close()
    names = {r["tablename"].lower() for r in rows}
    return sorted(names - {t.lower() for t in allowlist})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--live",
        action="store_true",
        help="also run the live pg_tables check (needs HELIXIS_SUPABASE__DB_URL)",
    )
    args = parser.parse_args()

    findings: dict[str, list[str]] = {}

    static_hits = find_unprotected_tables(_read_migration_texts())
    if static_hits:
        findings["static_migrations"] = static_hits

    db_url = os.environ.get("HELIXIS_SUPABASE__DB_URL", "")
    if args.live:
        if not db_url:
            findings["live_error"] = ["--live requested but HELIXIS_SUPABASE__DB_URL is unset"]
        else:
            live_hits = asyncio.run(find_live_unprotected(db_url))
            if live_hits:
                findings["live_rls_disabled"] = live_hits

    if args.json:
        print(json.dumps(findings, indent=2))
    else:
        if not findings:
            print("RLS guard: OK — every checked public table has RLS enabled.")
        else:
            for kind, tables in findings.items():
                print(f"RLS guard FAIL [{kind}]: {', '.join(tables)}")

    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
