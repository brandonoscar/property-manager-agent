-- 0025_rls_on_runtime_tables.sql
--
-- Close the forgotten-RLS gap (risk #1) on two classes of public table that
-- the migration chain never locked down:
--
--   A. Mem0-created tables (helixis_memories, helixis_memories_entities,
--      mem0migrations). Mem0 creates these at RUNTIME via the service-role
--      ``HELIXIS_SUPABASE__DB_URL`` connection (see memory/manager.py), so they
--      were never in a migration and never had RLS enabled. The live security
--      advisor flagged all three as ``rls_disabled_in_public`` (ERROR): fully
--      exposed to the ``anon`` + ``authenticated`` roles, i.e. readable by
--      anyone holding the public anon key via PostgREST. They hold tenant
--      memory content (potential PII).
--
--      Fix: enable RLS with NO permissive policy (deny-all). The backend
--      reaches these tables ONLY through the privileged pool, which BYPASSES
--      RLS — so the app path is unaffected, while anon/authenticated access is
--      denied by default. This is the same pattern already used for
--      ``helixis_gmail_seen`` (0024).
--
--   B. ``buildium_events`` reproducibility. The live DB already has RLS +
--      membership policies on this table (applied out-of-band), but NO LIVE
--      migration reproduces it: it was only ever set up in
--      ``0002_phase1_memory_rls.sql``, which was never applied (see its header).
--      A fresh environment provisioned purely from the migrations would create
--      ``buildium_events`` (0007) with RLS OFF. This section re-states the RLS +
--      policies idempotently so fresh installs match live. On the live DB it is
--      a no-op (drop-if-exists + recreate of the same policies).
--
-- Apply order: after 0024. Idempotent — safe to re-run.
--
-- NOTE (durable companion): the Mem0 tables are library-created, so a migration
-- alone cannot guarantee coverage on a brand-new environment (the table may not
-- exist yet at apply time). memory/manager.py also enables RLS on these names
-- right after Mem0 initializes; this migration closes the gap on the EXISTING
-- live DB now, and the boot-time lock covers fresh provisions going forward.

begin;

-- ── A. Mem0 runtime tables — deny-all (service-role only) ──────────────
-- ``if exists`` so this still applies cleanly on an environment where Mem0
-- has not yet created one of these tables.
alter table if exists public.helixis_memories          enable row level security;
alter table if exists public.helixis_memories_entities enable row level security;
alter table if exists public.mem0migrations            enable row level security;

-- Belt-and-suspenders: revoke direct grants so the tables stay locked even if a
-- permissive policy is added by mistake later. The privileged pool role is
-- unaffected (it owns / bypasses).
-- Guarded like the lines above: on a new database Mem0 has not created these
-- yet. Re-apply this file after the first boot with memory on (SELF-HOSTING.md).
do $$
declare t text;
begin
    foreach t in array array['helixis_memories', 'helixis_memories_entities', 'mem0migrations']
    loop
        if to_regclass('public.' || t) is not null then
            execute format('revoke all on public.%I from anon, authenticated', t);
        end if;
    end loop;
end $$;

-- ── B. buildium_events — reproduce live RLS for fresh installs ─────────
alter table if exists public.buildium_events enable row level security;

drop policy if exists "members can read buildium_events"  on public.buildium_events;
drop policy if exists "members can write buildium_events" on public.buildium_events;

create policy "members can read buildium_events"
    on public.buildium_events for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = buildium_events.company_id
               and cu.user_id    = auth.uid()
        )
    );

create policy "members can write buildium_events"
    on public.buildium_events for all
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = buildium_events.company_id
               and cu.user_id    = auth.uid()
        )
    )
    with check (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = buildium_events.company_id
               and cu.user_id    = auth.uid()
        )
    );

commit;
