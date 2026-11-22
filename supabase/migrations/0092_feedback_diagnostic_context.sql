-- 0092 — a bug report that can be diagnosed without a conversation.
--
-- `helixis_feedback` stored category + message + view. That is enough to know
-- somebody was unhappy and not enough to find out why: when chat fails the SSE
-- response is already a 200, the user sees "something went wrong", and the
-- traceback is Render-side only (gotcha 28 took chat out for every substantive
-- turn with 4308 tests green). The one thing that closes that gap is the
-- `orchestrator_runs` row for the turn they were complaining about.
--
-- ⚠ `run_id` is RESOLVED SERVER-SIDE from `session_id`, not sent by the client.
-- The report is most valuable exactly when the turn failed, and a failed turn
-- is the least likely to have delivered a run id to the browser — the envelope
-- that carries one is emitted only in loop mode and only with a final output.
-- Asking the client for the id would make the field reliably absent in the
-- case it exists for.
--
-- ⚠ No FK on `run_id`. `orchestrator_runs` is subject to retention, and a
-- cascade would silently delete the bug report along with the run it describes
-- — losing the report is strictly worse than holding a dangling id, because
-- the MESSAGE is the part a person wrote.

alter table public.helixis_feedback
    add column if not exists session_id  text,
    add column if not exists run_id      uuid,
    add column if not exists app_version text,
    add column if not exists user_agent  text;

comment on column public.helixis_feedback.session_id is
    'The chat thread open when the report was filed. What the client actually has.';
comment on column public.helixis_feedback.run_id is
    'Resolved server-side from session_id at submit time — never client-supplied.';
comment on column public.helixis_feedback.app_version is
    'Frontend build. A report that does not say which build it came from cannot '
    'distinguish "already fixed" from "still broken".';

-- Reports are read newest-first per company; the index makes that cheap once
-- there are more than a handful.
create index if not exists helixis_feedback_company_created_idx
    on public.helixis_feedback (company_id, created_at desc);
