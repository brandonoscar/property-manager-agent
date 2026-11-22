-- 0054: Entity signals — the operational-memory ledger (#534 Stage B).
--
-- One row per current advisory behavioral signal about a Buildium entity
-- (tenant / unit / owner / lease / property), deduplicated by
-- (company, entity, category). Rows are DERIVED DETERMINISTICALLY by the
-- webhook worker from the mirror (helixis_lease_balances aging buckets,
-- helixis_lease_transactions) on payment/lease events — NO per-event LLM
-- call, NO mem0 in the worker (gotcha #9: the worker stays off mem0's RAM).
-- The API process keeps mem0 for chat-learned facts; this table is the
-- cheaper deterministic path for operational signals.
--
-- ADVISORY ONLY: signals are surfaced in the "What Helixis noticed"
-- enrichment card + drafted replies (flag a chronic late-payer), never
-- auto-acted. PII is stripped before store. Company-scoped + RLS, mirroring
-- helixis_risk_flags (0051).
--
-- Lifecycle: derivation upserts what it currently computes for an entity
-- (refreshing last_seen_at + the signal text); a category it no longer
-- computes for that entity is left in place until re-derived (a good payer
-- never gets a row in the first place — derive_* returns None).

create table if not exists public.helixis_entity_signals (
    id            uuid        primary key default gen_random_uuid(),
    company_id    uuid        not null
        references public.companies(id) on delete cascade,
    -- The Buildium entity the signal is about. entity_id is the Buildium id
    -- as TEXT to match the entity_memory (Stage A) convention, where metadata
    -- ids are compared as strings.
    entity_type   text        not null
        check (entity_type in ('tenant', 'unit', 'owner', 'vendor', 'lease', 'property')),
    entity_id     text        not null,
    -- Human label used in prompts / the noticed card ("Unit 4B", "Lease 24195").
    entity_label  text        not null default '',
    -- What kind of signal (one row per category per entity): 'payment_behavior'
    -- (chronic late-payer), 'balance' (outstanding balance), extensible.
    category      text        not null,
    -- The advisory fact, PII-stripped, plain English.
    signal        text        not null,
    severity      text        not null default 'info'
        check (severity in ('info', 'warning', 'critical')),
    -- Provenance — 'operational' for worker-derived. Kept so a future
    -- analyst/LLM signal source can share the table without a schema change.
    source        text        not null default 'operational',
    -- The exact numbers the signal was computed from — the citation
    -- (late_count, periods_considered, aging buckets, balance, as_of).
    evidence      jsonb       not null default '{}'::jsonb,
    first_seen_at timestamptz not null default now(),
    last_seen_at  timestamptz not null default now(),
    unique (company_id, entity_type, entity_id, category)
);

create index if not exists helixis_entity_signals_entity_idx
    on public.helixis_entity_signals (company_id, entity_type, entity_id);

alter table public.helixis_entity_signals enable row level security;

-- Company-member read (writes are backend-only via the service-role pool,
-- which bypasses RLS — same pattern as helixis_risk_flags 0051).
drop policy if exists helixis_entity_signals_member_select on public.helixis_entity_signals;
create policy helixis_entity_signals_member_select
    on public.helixis_entity_signals for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_entity_signals.company_id
               and cu.user_id = auth.uid()
        )
    );
