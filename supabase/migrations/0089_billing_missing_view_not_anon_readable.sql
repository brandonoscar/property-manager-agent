-- 0089_billing_missing_view_not_anon_readable.sql — close a hole 0088 opened.
--
-- 0088 added `helixis_billing_missing` as an operator check. A view in `public`
-- is owned by `postgres`, and `postgres` carries `rolbypassrls`, so WITHOUT
-- `security_invoker` the view executes with the OWNER's rights — RLS on the
-- base tables simply does not apply to it. Postgres also grants SELECT on a new
-- view to `anon` and `authenticated` in this project's default setup.
--
-- ⚠ THAT IS AN RLS BYPASS, and the anon key is public: it ships in the deployed
-- frontend bundle (`frontend/src/services/auth.ts`). Measured before the fix —
-- `companies` and `helixis_billing` both have RLS ON, so anon reads ZERO rows
-- from either directly; the view was the one route around that, exposing every
-- company's id, name and signup date to anybody who opened DevTools. It was the
-- only app-owned view in `public` (the other two are PostGIS system views owned
-- by supabase_admin).
--
-- It reads 0 rows TODAY only because every company has a billing row. It starts
-- returning data the moment 0088's deliberately-swallowed trigger failure
-- fires — i.e. exactly when something has already gone wrong.
--
-- ⚠ The general rule, because this will recur: **every helixis_* TABLE in this
-- project is RLS-fail-closed, and adding a VIEW over them silently opts out of
-- that.** `enable row level security` on the table protects the table, not a
-- view someone later defines on top of it. Any future view in `public` needs
-- BOTH lines below, or it belongs in a non-exposed schema.
--
-- Two lines rather than one, deliberately — they fail independently:
--   * security_invoker makes the CALLER's RLS apply, so even a re-granted
--     SELECT returns nothing to a stranger;
--   * the REVOKE removes the grant, so even a Postgres default that changes
--     under us cannot re-expose it.
-- The view is for operators reading through the service key (which bypasses
-- both by design); no browser role ever needs it.

alter view public.helixis_billing_missing set (security_invoker = true);

revoke all on public.helixis_billing_missing from anon;
revoke all on public.helixis_billing_missing from authenticated;

-- ── 0088's second exposure: the trigger function itself ───────────────────
-- A function in `public` is published by PostgREST as
-- `/rest/v1/rpc/grant_billing_trial`, and this one is SECURITY DEFINER, so the
-- advisor flags it EXTERNAL for both `anon` and `authenticated`.
--
-- MEASURED, not assumed: it is NOT exploitable. Calling it raises
-- `0A000 — trigger functions can only be called as triggers`, because a
-- `returns trigger` function cannot be invoked outside a trigger context.
-- This is tidiness, and tidiness on a security advisor is worth having: noise
-- there is how the next REAL finding gets scrolled past.
--
-- ⚠ REVOKE FROM `public`, NOT JUST `anon` — that is migration 0026's exact
-- mistake, corrected by 0027. Postgres grants EXECUTE to the built-in PUBLIC
-- role by default and `anon` INHERITS it, so revoking from `anon` alone is a
-- no-op and the advisor warning does not move.
--
-- No grant back to `authenticated`: unlike the RLS helper functions 0027 had to
-- preserve, a trigger function fires as its owner regardless of caller EXECUTE,
-- so removing the grant cannot break signup. 0027 already records that reasoning
-- for `handle_new_user`, which is why that one is not flagged and this one was.
revoke execute on function public.grant_billing_trial() from public, anon, authenticated;
