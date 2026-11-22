-- 0030_crm_lead_draft.sql
--
-- CRM lead AI assist — cached "what Helixis noticed" signals + drafted reply.
--
-- When a lead arrives, Helixis reads the conversation and produces (a) a few
-- short "signals" (what it noticed — intent, questions asked, move-in date)
-- and (b) a drafted first reply the PM approves/edits/sends. Both are
-- generated once (an LLM call) and cached on the lead so the board doesn't
-- re-run the model on every view.
--
-- Shape notes:
-- * ``signals`` jsonb — an array of short strings (mirrors the Inbox
--   "What Helixis noticed" shape). NULL = not generated yet.
-- * ``draft_reply`` text — the suggested reply. NULL = not generated yet.
-- * ``draft_generated_at`` timestamptz — when the assist was produced; NULL
--   until first generation. Lets a future pass regenerate when stale.
--
-- All nullable + additive — no backfill, no RLS change (inherits crm_leads'
-- policies). Apply after 0029.
--
-- NOTE: migrations have no runner (gotcha #8) — apply manually to
-- helixis-test.

begin;

alter table public.crm_leads
    add column if not exists signals             jsonb,
    add column if not exists draft_reply         text,
    add column if not exists draft_generated_at  timestamptz;

commit;
