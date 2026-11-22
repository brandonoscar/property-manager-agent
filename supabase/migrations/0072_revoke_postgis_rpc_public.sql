-- 0072: attempted completion of 0065 — RESULT: NOT FIXABLE BY MIGRATION.
-- Applied to helixis-test 2026-08-20 and verified by effect: the ACL is
-- UNCHANGED. Documented here so no future session re-attempts the same
-- no-op (gotcha 8: verify by effect, never by header).
--
-- Root cause, two layers deep:
-- 1. 0065 revoked FROM anon/authenticated but the PostGIS install grants
--    EXECUTE to PUBLIC (the 0026→0027 "FROM anon is a no-op while PUBLIC
--    still grants" trap) — that's what this file was written to fix.
-- 2. The deeper trap: EVERY grant on these functions was made by
--    supabase_admin (proacl shows "…=X/supabase_admin" for all entries),
--    the reserved Supabase superuser that owns the postgis extension.
--    Postgres SILENTLY IGNORES a REVOKE issued by a role that did not
--    make the grant — and migrations (MCP / SQL editor / psql) run as
--    postgres, which cannot act for supabase_admin and cannot ALTER a
--    supabase_admin-owned function. So 0065 was a silent no-op, and so
--    is this file's revoke. There is NO customer-level SQL that removes
--    these grants.
--
-- Risk accepted (pre-launch decision, 2026-08-20): the exposure is a
-- low-severity metadata oracle — anon can call
-- /rest/v1/rpc/st_estimatedextent and learn table-extent statistics for
-- PostGIS columns (bounding boxes, not row data; RLS on the tables is
-- unaffected). Real fixes, both post-release maintenance:
--   (a) ask Supabase support to revoke PUBLIC/anon/authenticated EXECUTE
--       on the st_estimatedextent overloads (they can act as
--       supabase_admin), or
--   (b) reinstall postgis into the `extensions` schema (requires
--       dropping/recreating the extension — blocked today by the
--       helixis_properties.geog dependency; needs a planned migration).
-- The Supabase security advisor will keep flagging this WARN until then;
-- treat it as known/accepted, not as a regression.

begin;

-- Kept for the record of what was attempted; silent no-op per the above.
revoke execute on function public.st_estimatedextent(text, text)
  from public, anon, authenticated;
revoke execute on function public.st_estimatedextent(text, text, text)
  from public, anon, authenticated;
revoke execute on function public.st_estimatedextent(text, text, text, boolean)
  from public, anon, authenticated;

commit;
