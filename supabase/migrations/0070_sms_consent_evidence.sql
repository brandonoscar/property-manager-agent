-- Consent EVIDENCE — the proof behind each helixis_sms_consent row.
--
-- APPLIED to helixis-test 2026-08-19 via the Supabase MCP; verified by
-- effect (evidence jsonb column present). No runner exists (CLAUDE.md §4 /
-- gotcha 8) — future DBs apply manually via psql.
--
-- The ledger row (0068) proves THAT consent exists; this column preserves
-- HOW it was captured, so we can produce "visual proof of the complete
-- opt-in sequence" for an A2P vetting appeal (rejection code 30555) and a
-- contemporaneous record for TCPA defense (class actions up 283% YoY as of
-- Sept 2025 — the evidence trail is the defense).
--
-- Shape (server-stamped at capture, never client-supplied):
--   {
--     "method":           "app_settings_form",
--     "consent_language": "<the exact disclosure text shown at capture>",
--     "language_version": 1,
--     "recorded_by":      "<auth.users id of the manager who recorded it>",
--     "recorded_by_email": "<their email>",
--     "ip":               "<request origin, best-effort>",
--     "user_agent":       "<browser UA, best-effort>"
--   }
--
-- NULL for rows captured before this column existed, and for consent that
-- arrives via non-form routes (e.g. a START webhook restoring a revoked
-- row keeps the ORIGINAL grant evidence — restoration is recorded by the
-- cleared revoked_at, not by rewriting history).
--
-- Apply to helixis-test manually (no runner — CLAUDE.md §4 / gotcha 8).

alter table public.helixis_sms_consent
    add column if not exists evidence jsonb;

comment on column public.helixis_sms_consent.evidence is
    'Server-stamped capture evidence: method, exact consent language shown, recorder identity, ip/user-agent. Never client-supplied.';
