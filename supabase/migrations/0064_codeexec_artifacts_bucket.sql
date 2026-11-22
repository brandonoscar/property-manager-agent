-- 0064: private storage bucket for agent code-execution output artifacts
-- (ADR 0008). Same model as company_documents (0033): service-role access
-- only, no storage.objects policies — the backend uploads with the service
-- key and hands out short-lived signed URLs (codeexec/storage_io.py).
--
-- APPLIED to helixis-test 2026-08-15 via the Supabase MCP
-- (migration name: codeexec_artifacts_bucket) — this file is the repo
-- record of that change per the no-runner convention (CLAUDE.md §4 /
-- gotcha 8). Re-running is a no-op (on conflict do nothing).

begin;

insert into storage.buckets (id, name, public)
values ('codeexec_artifacts', 'codeexec_artifacts', false)
on conflict (id) do nothing;

commit;
