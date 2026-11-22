-- 0050: Per-company daily LLM token ledger (pre-launch audit: spend cap).
--
-- One row per (company, UTC day), incremented by the LiteLLM success
-- callback for every completion anywhere in the fleet. The /agent/run
-- gate compares today's row against llm.daily_token_budget and 429s at
-- the ceiling. Rows are tiny and self-partitioning by day; no cleanup
-- needed at this scale.

create table if not exists public.helixis_llm_usage (
    company_id  uuid        not null
        references public.companies(id) on delete cascade,
    day         date        not null,
    tokens      bigint      not null default 0,
    requests    integer     not null default 0,
    updated_at  timestamptz not null default now(),
    primary key (company_id, day)
);

alter table public.helixis_llm_usage enable row level security;

drop policy if exists helixis_llm_usage_member_select on public.helixis_llm_usage;
create policy helixis_llm_usage_member_select
    on public.helixis_llm_usage for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = helixis_llm_usage.company_id
               and cu.user_id = auth.uid()
        )
    );
