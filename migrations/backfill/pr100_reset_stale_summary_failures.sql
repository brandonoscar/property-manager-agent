-- PR #100 — Reset summaries that failed under the over-aggressive
-- sanitizer (PR #99 V2 anchor + pre-#100 sanitizer combo).
--
-- After this UPDATE runs and the patched sanitizer ships, the
-- worker's claim_summary_batch (PR #99) picks these rows up on its
-- next tick and re-summarizes them with V2.
--
-- v1-succeeded rows would auto-heal via PR #99's `summary_prompt_version
-- IS DISTINCT FROM <current>` claim predicate even without this script,
-- but we still reset their `summary_attempts` so they get a fresh
-- retry budget rather than burning past V1-era attempts toward the
-- max-attempts cap.
--
-- Run manually in Supabase SQL editor after PR #100 merges and the
-- web service + worker redeploy. Recommended workflow:
--
--   BEGIN;
--     <paste the UPDATE below>
--     -- inspect the RETURNING output: row count + event names
--     -- should look sane (single-digit count for current prod).
--   COMMIT;     -- or ROLLBACK; if anything looks off.
--
-- Schema notes:
--   * `summary_status` is NOT NULL with CHECK constraint allowing
--     ('pending', 'succeeded', 'failed', 'retrying') — we reset to
--     'pending', not NULL.
--   * `summary_last_attempted_at` is the cooldown gate. Clearing it
--     to NULL lets the worker pick the row up immediately on its
--     next tick rather than waiting for the cooldown window.

UPDATE buildium_events
   SET summary_status            = 'pending',
       summary_attempts          = 0,
       summary_error             = NULL,
       summary_prompt_version    = NULL,
       summary_last_attempted_at = NULL
 WHERE
       -- Only reset rows that were not summarized under the current
       -- (v2) prompt. Anything that already succeeded under v2 stays
       -- untouched.
       (summary_prompt_version IS DISTINCT FROM 'v2')
   AND (
            summary_status = 'failed'
         OR summary_status = 'succeeded'  -- v1-succeeded → re-summarize to v2
         OR summary_text IS NULL
       )
RETURNING id, event_name, summary_status, summary_attempts;
