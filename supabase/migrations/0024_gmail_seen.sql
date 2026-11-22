-- 0024_gmail_seen.sql
--
-- Email-ingest feature — "important long emails go through the webhook
-- system." The webhook worker polls each connected user's Gmail, filters
-- to messages from a known Buildium contact that are long, and enqueues
-- them into ``buildium_events`` so they ride the existing enrichment →
-- planner → summarizer → Inbox pipeline (see
-- src/helixis/webhooks/email_ingest.py + email_triage.py).
--
-- This table is the poll cursor / dedup ledger: one row per Gmail message
-- the worker has already considered for a user, so a later poll cycle
-- never re-enqueues it. We record EVERY message the poll examined (not
-- just the ones that surfaced), so a message that didn't qualify (short,
-- or from a stranger) isn't re-examined every cycle forever.
--
-- Apply order: after 0023. Written + read ONLY by the worker's
-- service-role asyncpg pool (which bypasses RLS), same as the queue
-- itself — there is no user-facing read path, so no SELECT policy is
-- needed. RLS is still enabled with no permissive policy so a stray
-- anon/auth client can't read another tenant's mail metadata.

create table if not exists public.helixis_gmail_seen (
    user_id     uuid        not null
        references auth.users(id) on delete cascade,
    message_id  text        not null,
    company_id  uuid
        references public.companies(id) on delete cascade,
    surfaced    boolean     not null default false,  -- did it pass the gate + enqueue?
    seen_at     timestamptz not null default now(),
    primary key (user_id, message_id)
);

-- Cheap "have I seen this set of ids for this user" lookup on each poll.
create index if not exists helixis_gmail_seen_user_idx
    on public.helixis_gmail_seen (user_id, seen_at desc);

alter table public.helixis_gmail_seen enable row level security;
-- No permissive policy: only the service-role worker touches this table,
-- and service-role bypasses RLS. Enabling RLS with no policy denies all
-- anon/auth access by default (defence in depth — it's mailbox metadata).
