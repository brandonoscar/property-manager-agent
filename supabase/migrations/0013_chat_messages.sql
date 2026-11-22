-- 0013_chat_messages.sql
--
-- PR #71 — persistent chat-session history.
--
-- Background. Pre-PR-#71 the orchestrator called ``Runner.run_streamed``
-- with the raw ``request.task`` each turn — no history, no context.
-- Brandon's "what leases are about to end" → "30 days" smoke test
-- exposed the bug: the assistant lost the original question because
-- every turn started cold. This migration adds the storage backing
-- the new ``src/helixis/agents/chat_history.py`` module reads from
-- before each turn and writes to after.
--
-- One row per turn (role in {user, assistant, tool}). The bidirectional
-- index supports both ``WHERE session_id = $1 ORDER BY created_at``
-- (history-load before a turn) and ``WHERE company_id = $1 ORDER BY
-- created_at DESC`` (the threads-list endpoint).
--
-- Apply order: after 0012. RLS scopes reads + writes to the requester's
-- company via ``company_users`` membership, matching the pattern used
-- by 0009 ``helixis_activity_log``. Service-role connections (the
-- asyncpg pool the orchestrator uses) bypass RLS, which is correct —
-- the orchestrator is a trusted server-side writer.

create table if not exists public.helixis_chat_messages (
    id          uuid        primary key default gen_random_uuid(),
    session_id  text        not null,
    company_id  uuid        not null
        references public.companies(id) on delete cascade,
    user_id     uuid        references auth.users(id) on delete set null,
    role        text        not null
        check (role in ('user', 'assistant', 'tool')),
    content     text        not null,
    tool_name   text,        -- non-null only when role='tool'
    tool_result jsonb,       -- non-null only when role='tool'
    created_at  timestamptz not null default now()
);

create index if not exists helixis_chat_messages_session_idx
    on public.helixis_chat_messages (session_id, created_at);

create index if not exists helixis_chat_messages_company_idx
    on public.helixis_chat_messages (company_id, created_at desc);

alter table public.helixis_chat_messages enable row level security;

drop policy if exists "chat_messages_company_read"   on public.helixis_chat_messages;
drop policy if exists "chat_messages_company_insert" on public.helixis_chat_messages;

create policy "chat_messages_company_read"
    on public.helixis_chat_messages for select
    using (
        exists (
            select 1 from public.company_users cu
             where cu.company_id = helixis_chat_messages.company_id
               and cu.user_id    = auth.uid()
        )
    );

create policy "chat_messages_company_insert"
    on public.helixis_chat_messages for insert
    with check (
        exists (
            select 1 from public.company_users cu
             where cu.company_id = helixis_chat_messages.company_id
               and cu.user_id    = auth.uid()
        )
    );
