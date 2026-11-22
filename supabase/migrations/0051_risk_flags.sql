-- 0051: Risk flags — the reconciliation ledger (roadmap §7 rank 4, Phase 1).
--
-- One row per detected "hidden risk" in a company's books, deduplicated by
-- (company, rule, fingerprint). Rows are written ONLY by the backend's
-- deterministic reconciliation sweep today; the same table is designed to
-- hold Phase-2 analyst (LLM) flags — hence `source` — so the surface built
-- on it never changes.
--
-- Lifecycle: a sweep upserts what it currently detects (refreshing
-- last_seen_at); flags it no longer detects auto-resolve; a PM can confirm
-- or dismiss, and a dismissal suppresses the flag from re-surfacing even
-- while the underlying condition persists.

create table if not exists public.helixis_risk_flags (
    id            uuid        primary key default gen_random_uuid(),
    company_id    uuid        not null
        references public.companies(id) on delete cascade,
    source        text        not null default 'rule'
        check (source in ('rule', 'analyst')),
    rule_key      text        not null,
    -- Stable identity of the finding within a rule (e.g. 'lease:12345').
    fingerprint   text        not null,
    severity      text        not null default 'warning'
        check (severity in ('info', 'warning', 'critical')),
    title         text        not null,
    detail        text        not null,
    -- The money at stake, when the rule can compute it (monthly gap,
    -- duplicate bill amount). Nullable — not every risk prices cleanly.
    amount        numeric(12, 2),
    property_id   bigint,
    entity_type   text,
    entity_id     bigint,
    -- The exact numbers the flag was computed from — the citation.
    evidence      jsonb       not null default '{}'::jsonb,
    status        text        not null default 'new'
        check (status in ('new', 'confirmed', 'dismissed', 'resolved')),
    first_seen_at timestamptz not null default now(),
    last_seen_at  timestamptz not null default now(),
    resolved_at   timestamptz,
    unique (company_id, rule_key, fingerprint)
);

create index if not exists helixis_risk_flags_company_status_idx
    on public.helixis_risk_flags (company_id, status);

alter table public.helixis_risk_flags enable row level security;

drop policy if exists helixis_risk_flags_member_select on public.helixis_risk_flags;
create policy helixis_risk_flags_member_select
    on public.helixis_risk_flags for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_risk_flags.company_id
               and cu.user_id = auth.uid()
        )
    );
