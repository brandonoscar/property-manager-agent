-- 0022_pending_confirmations.sql
--
-- Durable record of every confirm-gate decision (the #208/#209 write
-- confirmation gate, ``confirm_or_abort`` in src/helixis/agents/tools.py).
-- One row per surfaced confirmation. The live in-process ``asyncio.Future``
-- remains the authoritative fast path for a running agent; THIS table is the
-- durability + audit layer underneath it, so that:
--
--   1. every approve/deny is recorded (audit) — who confirmed what, when;
--   2. a confirmation orphaned by a process restart fails HONESTLY instead of
--      silently. When ``/agent/confirm`` arrives and no live Future exists
--      (the run's coroutine died on restart), the row lets us return an
--      ``orphaned`` status to the UI ("the assistant restarted — please
--      re-send") rather than a silent no-op that hangs forever.
--
-- This is NOT true resume: a restart still loses the paused tool call (that
-- requires checkpoint/replay of the agent run, a separate effort). This makes
-- the loss honest + auditable and lays the persistence groundwork.
--
-- Status machine:
--   pending   — surfaced to the user; the run is awaiting an in-process answer.
--   approved  — user approved while a live waiter existed (action proceeds).
--   denied    — user denied while a live waiter existed.
--   expired   — the in-process wait timed out (confirm_or_abort timeout) with
--               no answer; nothing fired.
--   orphaned  — no live waiter when the answer arrived, OR reconciled at boot
--               (a previous process held the wait and died). The action will
--               NOT run; the user must re-issue the request.
--
-- Best-effort, NOT fail-closed: unlike the idempotency guard (0021), the live
-- in-memory path works fully within a single process. If this store is
-- unavailable the system degrades to pre-0022 behavior — it never blocks a
-- confirmable write.
--
-- Per-tenant via ``company_id`` + RLS mirroring ``helixis_action_idempotency``
-- (0021) / ``helixis_activity_log`` (0009). The backend writes through the
-- service connection (RLS bypassed); the policies are defense-in-depth.
--
-- Apply order on prod (after 0021 — there is no migration runner):
--   psql "$HELIXIS_SUPABASE__DB_URL" -f supabase/migrations/0022_pending_confirmations.sql
--
-- Idempotent: create table/index if not exists; drop policy if exists before
-- each create policy.

begin;

create table if not exists public.helixis_pending_confirmations (
    confirm_id   uuid        primary key,        -- minted by confirm_or_abort
    company_id   text        not null,
    session_id   text,
    user_id      text,
    action       text        not null,           -- machine action tag
    title        text,
    details      text,
    items        jsonb       not null default '[]'::jsonb,
    status       text        not null default 'pending',   -- see status machine above
    expires_at   timestamptz,                     -- created_at + confirm timeout
    created_at   timestamptz not null default now(),
    resolved_at  timestamptz
);

create index if not exists helixis_pending_confirmations_company_status_idx
    on public.helixis_pending_confirmations (company_id, status);

create index if not exists helixis_pending_confirmations_status_expires_idx
    on public.helixis_pending_confirmations (status, expires_at);

alter table public.helixis_pending_confirmations enable row level security;

drop policy if exists "members read pending confirmations"  on public.helixis_pending_confirmations;
drop policy if exists "members write pending confirmations" on public.helixis_pending_confirmations;

create policy "members read pending confirmations"
    on public.helixis_pending_confirmations for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id::text = helixis_pending_confirmations.company_id
               and cu.user_id          = auth.uid()
        )
    );

create policy "members write pending confirmations"
    on public.helixis_pending_confirmations for insert
    with check (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id::text = helixis_pending_confirmations.company_id
               and cu.user_id          = auth.uid()
        )
    );

commit;
