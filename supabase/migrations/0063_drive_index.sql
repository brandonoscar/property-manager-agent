-- 0063_drive_index.sql
--
-- Static Google Drive index (#67 PR-2). When a user connects Google Drive we
-- enumerate their Drive ONCE in the background (the daily company-knowledge
-- cycle) and cache a lightweight index here — title + folder path + mime type +
-- modified time, one row per (company, connected user, Drive file). The agent
-- then knows WHAT documents exist without a live Drive API call on every
-- question; only fetching a specific file's CONTENTS still goes live.
--
-- Refresh is a full-refresh + prune per (company, user): the daily job deletes
-- the user's rows and re-inserts the current listing in one transaction, so a
-- file removed from Drive disappears from the index (same reconcile pattern as
-- the GL-account / bank-account webhook handlers — gotcha 15).
--
-- Written by the backend/worker via the app pool (service DSN, bypasses RLS).
-- RLS is enabled with a members-read policy so the table is never anon/authed
-- readable outside a company the caller belongs to.
--
-- Apply on prod (after 0062):
--   psql "$HELIXIS_SUPABASE__DB_URL" -f supabase/migrations/0063_drive_index.sql

begin;

create table if not exists public.drive_index (
    company_id       uuid        not null,
    -- The Composio OAuth scope key = the auth user id (Drive is a PERSONAL
    -- connection — one human's Google account, not the whole company's).
    composio_user_id text        not null,
    file_id          text        not null,   -- Google Drive file id
    title            text        not null,
    mime_type        text,
    path             text,                    -- folder / parent, when available
    modified_at      timestamptz,            -- Drive modifiedTime
    indexed_at       timestamptz not null default now(),
    primary key (company_id, composio_user_id, file_id)
);

alter table public.drive_index enable row level security;

drop policy if exists "members read drive index" on public.drive_index;
create policy "members read drive index"
    on public.drive_index for select
    using (
        exists (
            select 1
              from public.company_users cu
             where cu.company_id = drive_index.company_id
               and cu.user_id    = auth.uid()
        )
    );

commit;
