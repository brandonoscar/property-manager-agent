"""Live tenant-isolation invariants — the half a unit test cannot see.

``test_tenant_scoping_corpus`` proves every ``helixis_*`` query carries
``company_id``; ``test_route_company_scope_corpus`` proves the id came from the
caller rather than from the request body; ``test_chat_threads_are_per_user``
proves a thread belongs to one person. All three read SOURCE, and all three go
green on a database that has already drifted.

This asks the DATABASE. Four invariants, each phrased so a violation is a row
count rather than a judgement:

1. **Orphan scope** — a company-scoped row pointing at a company that no longer
   exists. Nothing can read it and nothing will purge it (ToS §18 keys the
   Buildium purge on the company), so it is an unowned copy of somebody's data.
2. **A user in two companies** — `require_unambiguous_company` calls this an
   invariant VIOLATION rather than a supported shape (founder directive
   2026-09-03, one account one company), and refuses the three irreversible
   actions when it sees one. If the count is ever non-zero, those actions are
   already declining for a real person.
3. **A chat row whose author is not a member of the company it is filed
   under** — the precondition the per-user thread scoping now rests on
   (gotcha 85). A row like that is invisible to its author AND to the company,
   which is a leak in the "silently loses data" direction rather than the
   "shows the wrong person" one; both matter.
4. **RLS off on an app table** — the live half of ``rls_guard.py``, kept here
   too because this probe is the one that FILES AN ISSUE. ``spatial_ref_sys``
   is PostGIS's coordinate-system reference table, owned by the extension and
   carrying no tenant data, so it is the one documented exemption.

⚠ **EXIT 2 IS NOT A PASS.** No ``HELIXIS_SUPABASE__DB_URL`` means the probe did
not run, and a check that did not run is not one that passed (the LED-013
skip-floor, and gotcha 23's whole lesson — a credential-gated check that never
executes stays broken invisibly for a month). The workflow files an issue on 2
exactly as it does on 1.

⚠ **THE SQL HERE WAS RUN AGAINST helixis-test ON 2026-09-04 AND CAME BACK
0/0/0/0 WITH 60 OF 61 TABLES RLS-ON.** That matters because this container
cannot reach ``supabase.co`` over asyncpg (gotcha 5's egress allowlist), so the
PYTHON WRAPPER has never made a live connection — only the queries have been
executed, through the Supabase MCP, in the form written below. The first
scheduled run is what proves the wrapper. Saying so beats implying an
end-to-end verification that did not happen (gotcha 23).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

#: PostGIS ships this and owns it; it holds coordinate systems, not tenant
#: rows. Anything else appearing here is a finding, not a second exemption.
_RLS_EXEMPT_TABLES = frozenset({"spatial_ref_sys"})

#: ⚠ Deliberately CROSS-COMPANY, and gotcha 47 requires saying so: these are
#: FLEET-WIDE invariants, so a company filter would defeat the question. No row
#: CONTENT is selected — counts only, so a finding names a number and never
#: carries a tenant's data into a GitHub issue.
_INVARIANTS_SQL = """
with orphan_chat as (
  select count(*) n from public.helixis_chat_messages m
   where not exists (select 1 from public.companies c where c.id = m.company_id)
),
orphan_props as (
  select count(*) n from public.helixis_properties p
   where not exists (select 1 from public.companies c where c.id = p.company_id)
),
multi_company_users as (
  select count(*) n from (
    select user_id from public.company_users
     group by user_id having count(distinct company_id) > 1
  ) x
),
membership_write_paths as (
  -- Each write PostgREST could make to company_users: a write policy, or an
  -- INSERT/UPDATE/DELETE grant to anon or authenticated. 0112 takes all away;
  -- 9 on helixis-test before it (3 policies, 6 grants; read 2026-11-11).
  select (select count(*) from pg_policies
           where schemaname = 'public' and tablename = 'company_users'
             and cmd in ('INSERT', 'UPDATE', 'DELETE', 'ALL'))
       + (select count(*)
            from (values ('anon'), ('authenticated')) r(rolname)
            cross join (values ('INSERT'), ('UPDATE'), ('DELETE')) p(priv)
           where has_table_privilege(r.rolname, 'public.company_users', p.priv)) n
),
chat_author_not_a_member as (
  select count(*) n from public.helixis_chat_messages m
   where m.user_id is not null
     and not exists (
       select 1 from public.company_users cu
        where cu.user_id = m.user_id and cu.company_id = m.company_id
     )
)
select (select n from orphan_chat)              as orphan_chat_rows,
       (select n from orphan_props)             as orphan_property_rows,
       (select n from multi_company_users)      as users_in_multiple_companies,
       (select n from chat_author_not_a_member) as chat_rows_author_not_member,
       (select n from membership_write_paths)   as company_users_api_write_paths
"""

_RLS_SQL = """
select tablename
  from pg_tables
 where schemaname = 'public'
   and not rowsecurity
 order by tablename
"""

#: invariant key -> the sentence a human needs when the issue lands at 2am.
_WHAT_IT_MEANS: dict[str, str] = {
    "orphan_chat_rows": (
        "chat rows point at a company that no longer exists — unreadable by "
        "anyone and skipped by the Buildium purge, so they are an unowned copy "
        "of somebody's conversation"
    ),
    "orphan_property_rows": (
        "mirrored properties point at a company that no longer exists — same "
        "unowned-copy problem, and a ToS §18 concern because the purge is "
        "keyed on the company"
    ),
    "users_in_multiple_companies": (
        "an account spans more than one workspace. require_unambiguous_company "
        "treats that as an invariant violation, so buying a number, filing the "
        "A2P attestation and opening Stripe Checkout are ALREADY refusing for "
        "this person with a 409"
    ),
    "company_users_api_write_paths": (
        "a signed-in user can write company_users through PostgREST (a write "
        "policy, or a write grant to anon or authenticated): any account holder "
        "can then add an existing user to their own company with no acceptance "
        "and become their active workspace. Migration 0112 closes it; check it "
        "is applied (gotcha 213)"
    ),
    "chat_rows_author_not_member": (
        "a chat row is filed under a company its author does not belong to. "
        "Thread reads scope on (company_id, user_id), so this row is invisible "
        "to its author AND to the company — data loss, not just misfiling"
    ),
}


def evaluate(counts: dict[str, int], tables_without_rls: list[str]) -> list[str]:
    """Pure core: the findings, in the words the issue will carry.

    Separated from the connection so it is unit-testable without a database —
    the wrapper is the part this container cannot exercise (see the module
    docstring), so the logic must not live inside it.
    """
    findings: list[str] = []
    for key, meaning in _WHAT_IT_MEANS.items():
        n = int(counts.get(key, 0) or 0)
        if n:
            findings.append(f"{key} = {n}: {meaning}")
    unexpected = sorted(set(tables_without_rls) - _RLS_EXEMPT_TABLES)
    if unexpected:
        findings.append(
            f"RLS is OFF on {len(unexpected)} public table(s): {', '.join(unexpected)}. "
            "Every app table needs it — a public table without RLS is the top "
            "cross-tenant leak vector, and a VIEW over one opts out by default "
            "unless it sets security_invoker (gotcha 82)."
        )
    return findings


async def _probe(db_url: str) -> tuple[dict[str, int], list[str]]:
    import asyncpg  # imported here so --help works without the dep

    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        row = await conn.fetchrow(_INVARIANTS_SQL)
        rls_rows = await conn.fetch(_RLS_SQL)
    finally:
        await conn.close()
    return dict(row or {}), [r["tablename"] for r in rls_rows]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    db_url = os.environ.get("HELIXIS_SUPABASE__DB_URL", "")
    if not db_url:
        # ⚠ 2, never 0. A probe that could not run has validated nothing, and
        # reporting that as clean is exactly how gotcha 23's check stayed
        # broken for a month with a green-looking history.
        msg = "HELIXIS_SUPABASE__DB_URL is unset — the probe did NOT run (this is not a pass)"
        print(json.dumps({"ran": False, "reason": msg}) if args.json else f"SKIPPED: {msg}")
        return 2

    try:
        counts, tables_without_rls = asyncio.run(_probe(db_url))
    except Exception as exc:  # noqa: BLE001 — an unreachable DB is exit 2, not a clean run
        msg = f"could not reach the database: {type(exc).__name__}: {exc}"
        print(json.dumps({"ran": False, "reason": msg}) if args.json else f"ERROR: {msg}")
        return 2

    findings = evaluate(counts, tables_without_rls)
    payload: dict[str, Any] = {
        "ran": True,
        "counts": counts,
        "tables_without_rls": tables_without_rls,
        "findings": findings,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for key, value in counts.items():
            print(f"  {key} = {value}")
        print(f"  tables without RLS: {tables_without_rls or '(none)'}")
        print()
        if findings:
            print(f"FINDINGS ({len(findings)}):")
            for f in findings:
                print(f"  - {f}")
        else:
            print("CLEAN — every tenant-isolation invariant holds.")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
