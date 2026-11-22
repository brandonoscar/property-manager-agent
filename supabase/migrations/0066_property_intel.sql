-- Property intelligence — free geo-data facts keyed off the property point.
--
-- One row per (company, property, source). ``source`` is one of the intel
-- clients in src/helixis/geo/intel.py: 'flood' (FEMA NFHL flood zone),
-- 'wildfire' (USFS Wildfire Hazard Potential), 'radon' (EPA county radon
-- zone), 'superfund' (EPA NPL sites near the point), 'fmr' (HUD Fair Market
-- Rent for the property's ZIP). Rows are written by the intel backfill
-- (geo/intel_backfill.py, off the webhook hot path) via the service-role
-- pool; ``detail`` carries the full parsed payload, ``code``/``label`` the
-- headline (e.g. code 'AE', label 'AE — high risk (1% annual chance)').
-- Absence of a row means "no data yet" — the agent tool says so honestly.
--
-- APPLIED to helixis-test 2026-08-17 via the Supabase MCP (migration
-- name: property_intel) — this file is the repo record per the no-runner
-- convention (CLAUDE.md §4 / gotcha 8).

create table if not exists public.helixis_property_intel (
    company_id           uuid        not null
        references public.companies(id) on delete cascade,
    buildium_property_id bigint      not null,
    source               text        not null,
    code                 text,
    label                text,
    detail               jsonb       not null default '{}',
    fetched_at           timestamptz not null default now(),
    primary key (company_id, buildium_property_id, source)
);

-- ── RLS: SELECT-only for company members (0038's pattern) ─────────────
-- The backend writes via the service role (bypasses RLS); the policy
-- gates user-scoped JWT reads.

alter table public.helixis_property_intel enable row level security;
drop policy if exists "members can read helixis property intel" on public.helixis_property_intel;
create policy "members can read helixis property intel"
    on public.helixis_property_intel for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_property_intel.company_id
               and cu.user_id    = auth.uid()
        )
    );
