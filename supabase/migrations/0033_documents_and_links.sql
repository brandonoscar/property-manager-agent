-- 0033_documents_and_links.sql
--
-- Settings → Documents & Links. Three small stores behind one settings page:
--   * user_links        — per-user URLs (application, scheduling). Owner-only.
--   * company_templates — company-shared canned message templates.
--   * company_documents — company-shared lease/addendum PDFs (metadata; the
--                         bytes live in the `company_documents` storage bucket,
--                         keyed by {company_id}/{id}.pdf).
--
-- NOTE: migrations have no runner (CLAUDE.md §4 / gotcha #8). This file being
-- committed is NOT proof it's live — apply it manually to helixis-test.

begin;

-- ── Per-user links ─────────────────────────────────────────────────────────
-- "Links would be by user" — each operator keeps their own scheduling /
-- application URLs. RLS is owner-only (user_id = auth.uid()), NOT company-wide.
create table if not exists public.user_links (
    id         uuid        primary key default gen_random_uuid(),
    user_id    uuid        not null
        references auth.users(id) on delete cascade,
    -- Short label the user gives it (e.g. "Scheduling link").
    label      text        not null,
    url        text        not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists user_links_user_idx on public.user_links (user_id);

alter table public.user_links enable row level security;

drop policy if exists "user_links_owner_read"  on public.user_links;
drop policy if exists "user_links_owner_write" on public.user_links;

create policy "user_links_owner_read"
    on public.user_links for select
    using (user_id = auth.uid());

create policy "user_links_owner_write"
    on public.user_links for all
    using (user_id = auth.uid())
    with check (user_id = auth.uid());

-- ── Company message templates ──────────────────────────────────────────────
-- "Templates shared across companies" — company-scoped, every teammate draws
-- from the same set. Company-member RLS (same shape as crm_leads).
create table if not exists public.company_templates (
    id         uuid        primary key default gen_random_uuid(),
    company_id uuid        not null
        references public.companies(id) on delete cascade,
    name       text        not null,
    -- The message body, with {merge_field} / {link} tokens filled at draft time.
    body       text        not null default '',
    created_by uuid        references auth.users(id) on delete set null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists company_templates_company_idx
    on public.company_templates (company_id);

alter table public.company_templates enable row level security;

drop policy if exists "company_templates_company_read"  on public.company_templates;
drop policy if exists "company_templates_company_write" on public.company_templates;

create policy "company_templates_company_read"
    on public.company_templates for select
    using (
        exists (
            select 1 from public.company_users cu
             where cu.company_id = company_templates.company_id
               and cu.user_id    = auth.uid()
        )
    );

create policy "company_templates_company_write"
    on public.company_templates for all
    using (
        exists (
            select 1 from public.company_users cu
             where cu.company_id = company_templates.company_id
               and cu.user_id    = auth.uid()
        )
    )
    with check (
        exists (
            select 1 from public.company_users cu
             where cu.company_id = company_templates.company_id
               and cu.user_id    = auth.uid()
        )
    );

-- ── Company documents (lease/addendum PDFs) ────────────────────────────────
-- Metadata only; the PDF bytes live in the `company_documents` storage bucket
-- at {company_id}/{id}.pdf, uploaded + signed server-side with the service key.
create table if not exists public.company_documents (
    id           uuid        primary key default gen_random_uuid(),
    company_id   uuid        not null
        references public.companies(id) on delete cascade,
    name         text        not null,
    -- 'lease' | 'addendum' | 'other'.
    kind         text        not null default 'other'
        check (kind in ('lease', 'addendum', 'other')),
    -- Storage object path within the `company_documents` bucket.
    storage_path text        not null,
    content_type text        not null default 'application/pdf',
    size_bytes   bigint      not null default 0,
    uploaded_by  uuid        references auth.users(id) on delete set null,
    created_at   timestamptz not null default now()
);

create index if not exists company_documents_company_idx
    on public.company_documents (company_id);

alter table public.company_documents enable row level security;

drop policy if exists "company_documents_company_read"  on public.company_documents;
drop policy if exists "company_documents_company_write" on public.company_documents;

create policy "company_documents_company_read"
    on public.company_documents for select
    using (
        exists (
            select 1 from public.company_users cu
             where cu.company_id = company_documents.company_id
               and cu.user_id    = auth.uid()
        )
    );

create policy "company_documents_company_write"
    on public.company_documents for all
    using (
        exists (
            select 1 from public.company_users cu
             where cu.company_id = company_documents.company_id
               and cu.user_id    = auth.uid()
        )
    )
    with check (
        exists (
            select 1 from public.company_users cu
             where cu.company_id = company_documents.company_id
               and cu.user_id    = auth.uid()
        )
    );

-- Private storage bucket for the PDFs. Uploads + downloads go through the
-- backend with the service key (which bypasses storage RLS), path-scoped by
-- company_id — same pattern as the browser-screenshot bucket.
insert into storage.buckets (id, name, public)
values ('company_documents', 'company_documents', false)
on conflict (id) do nothing;

commit;
