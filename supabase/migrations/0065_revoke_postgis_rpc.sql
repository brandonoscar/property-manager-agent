-- 0065: revoke anon/authenticated EXECUTE on PostGIS's SECURITY DEFINER
-- st_estimatedextent overloads. Flagged by the Supabase security advisor
-- (2026-08-17 pre-release pass): the postgis extension (0053) installed
-- them in public, so PostgREST exposes /rest/v1/rpc/st_estimatedextent to
-- any anon/signed-in caller as a definer function (table-extent metadata
-- oracle). Nothing client-side calls PostGIS — the geo layer is
-- server-side via the service role, which is unaffected by these revokes.
--
-- APPLIED to helixis-test 2026-08-17 via the Supabase MCP (migration
-- name: revoke_postgis_rpc) — this file is the repo record per the
-- no-runner convention (CLAUDE.md §4 / gotcha 8).
--
-- ⚠ CORRECTION (2026-08-20 release sweep): applied but INEFFECTIVE —
-- the grants were made by supabase_admin, so this revoke (run as
-- postgres) was silently ignored and anon can still execute the RPC.
-- See 0072_revoke_postgis_rpc_public.sql for the full analysis and the
-- accepted-risk decision. Do not trust this header as proof of effect.

begin;

revoke execute on function public.st_estimatedextent(text, text)
  from anon, authenticated;
revoke execute on function public.st_estimatedextent(text, text, text)
  from anon, authenticated;
revoke execute on function public.st_estimatedextent(text, text, text, boolean)
  from anon, authenticated;

commit;
