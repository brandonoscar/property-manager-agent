-- 0106_rentvine_webhook_deliveries.sql — Rentvine webhooks the receiver refused,
-- kept so the signature recipe can be worked out from real deliveries.
--
-- Measured 2026-10-04 (Render request log, 18:04:02Z): Rentvine delivers to
-- POST /webhooks/rentvine/{account}?timestamp=…&signature=…&token=… — the
-- signature travels in the QUERY STRING, not a header. The receiver refuses
-- every delivery until the scheme is known, and refused deliveries were kept
-- nowhere, so deriving the scheme needed a tunnel to the founder's machine
-- (which dropped twice in one afternoon). The receiver now records each
-- refused delivery here, and `scripts/rentvine_webhook_capture.py --analyse-db`
-- derives the scheme from them with the signing key held in the operator's
-- own shell.
--
-- Bounded in code (rentvine/webhook_deliveries.py and the receiver): the newest
-- KEEP_PER_COMPANY rows per company are kept, at most RECORDS_PER_HOUR are
-- recorded per company per hour, and a body is at most
-- webhooks/rentvine_router.MAX_BODY_BYTES. The payloads ARE
-- Rentvine data, so the table is in buildium/purge.PURGE_TABLES (one mirror,
-- one purge list) and is deleted with the company.
--
-- RLS on with NO policy: only the backend's privileged pool reads or writes
-- it (the 0024/0025 pattern), so anon and authenticated roles see nothing.
--
-- Standing check (run after applying; expect 1 row, relrowsecurity = true):
--     select c.relname, c.relrowsecurity
--       from pg_class c join pg_namespace n on n.oid = c.relnamespace
--      where n.nspname = 'public'
--        and c.relname = 'helixis_rentvine_webhook_deliveries';
--
-- NOTE: migrations have no runner (supabase/migrations/CLAUDE.md) — apply by
-- hand and VERIFY by effect.

begin;

create table if not exists public.helixis_rentvine_webhook_deliveries (
    id            bigint generated always as identity primary key,
    company_id    uuid not null
        references public.companies(id) on delete cascade,
    received_at   timestamptz not null default now(),
    method        text not null,
    -- The request path and the RAW query string, exactly as they arrived:
    -- the signature can cover either, and a decoded or reordered copy would
    -- no longer match it.
    path          text not null,
    query         text not null default '',
    -- Header names and values as sent, minus cookie, authorization and
    -- proxy-authorization (rentvine/webhook_deliveries.DROPPED_HEADERS).
    headers       jsonb not null default '{}'::jsonb,
    -- The exact body bytes. A JSON round trip changes whitespace and key
    -- order, which breaks every candidate signature.
    body          bytea not null,
    -- Why the receiver refused it (SignatureUnverifiable's message).
    refused_because text not null default ''
);

-- Newest first by id: the writer prunes and the tool reads in that order.
create index if not exists helixis_rentvine_webhook_deliveries_company_newest
    on public.helixis_rentvine_webhook_deliveries (company_id, id desc);

alter table public.helixis_rentvine_webhook_deliveries enable row level security;

comment on table public.helixis_rentvine_webhook_deliveries is
    'Rentvine webhook deliveries the receiver refused, kept verbatim (the newest '
    'KEEP_PER_COMPANY per company, rentvine/webhook_deliveries.py) so '
    'scripts/rentvine_webhook_capture.py --analyse-db can derive the signature '
    'scheme. Purged with the company''s Rentvine data.';

commit;
