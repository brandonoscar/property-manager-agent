-- 0021_action_idempotency.sql
--
-- Agent-action idempotency guard (approved design). One row per
-- irreversible/write action that passes through ``guarded_action``
-- (src/helixis/agents/idempotency.py). The row is both the dedup claim
-- and the replay record, so an approved irreversible action cannot fire
-- twice across double-approval, retry, or a process restart.
--
-- Status machine (NO generic auto-retrying "failed"):
--   in_progress       — claimed; the side effect may be running.
--   succeeded         — completed; ``result`` is replayed on a repeat.
--   failed_retryable  — failed BEFORE the external side effect (validation /
--                       argument / confirm construction); safe to retry.
--   abandoned_unknown — ambiguous failure (timeout / connection / 5xx after
--                       send) OR a reaped stale claim; the side effect MAY
--                       have happened → NEVER auto-retried, needs review.
--   aborted           — user declined at the confirm gate; nothing fired →
--                       a later attempt may re-confirm (treated as fresh).
--
-- Per-tenant via ``company_id`` + RLS mirroring ``helixis_activity_log``
-- (0009). The backend writes through the service connection (RLS bypassed);
-- the policies are defense-in-depth for any anon/role access.
--
-- Apply order on prod (after 0020):
--   psql "$HELIXIS_SUPABASE__DB_URL" -f supabase/migrations/0021_action_idempotency.sql
--
-- Idempotent: create table/index if not exists; drop policy if exists
-- before each create policy.

begin;

create table if not exists public.helixis_action_idempotency (
    id           uuid        primary key default gen_random_uuid(),
    company_id   text        not null,
    idem_key     text        not null,        -- sha256 hex of FULL normalized action values
    action_type  text        not null,
    status       text        not null,        -- see status machine above
    result       jsonb,                       -- serialized prior result, replayed on repeat
    error        text,
    attempts     integer     not null default 1,
    lease_until  timestamptz,                 -- in_progress lease deadline (reaper input)
    expires_at   timestamptz,                 -- dedup window end; NULL = session-scoped, no expiry
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),
    unique (company_id, idem_key)
);

create index if not exists helixis_action_idempotency_company_status_idx
    on public.helixis_action_idempotency (company_id, status);

create index if not exists helixis_action_idempotency_expires_at_idx
    on public.helixis_action_idempotency (expires_at);

alter table public.helixis_action_idempotency enable row level security;

drop policy if exists "members read action idempotency"  on public.helixis_action_idempotency;
drop policy if exists "members write action idempotency" on public.helixis_action_idempotency;

create policy "members read action idempotency"
    on public.helixis_action_idempotency for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id::text = helixis_action_idempotency.company_id
               and cu.user_id          = auth.uid()
        )
    );

create policy "members write action idempotency"
    on public.helixis_action_idempotency for insert
    with check (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id::text = helixis_action_idempotency.company_id
               and cu.user_id          = auth.uid()
        )
    );

commit;
