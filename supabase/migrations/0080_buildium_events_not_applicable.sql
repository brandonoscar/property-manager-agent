-- buildium_events: a terminal planner/summary status for events the Inbox
-- allowlist deliberately excludes.
--
-- WHY
-- The receiver gates enrichment and routing on ``surfaces_in_inbox``
-- (webhooks/router.py), but the row landed at the column default 'pending'
-- and NEITHER claim query carries an allowlist — so the worker planned and
-- summarized every non-inbox event anyway.
--
-- Live-measured 2026-08-29 (global — pipeline health is cross-tenant):
--   565 rows total
--   400 in non-inbox families (LeaseTransaction.*, OwnershipAccountTransaction.*)
--   484 at planner_status='failed', of which **437** read
--       "no enrichment handler registered" — i.e. 90% of the one status an
--       operator would check is events that were never eligible, wearing the
--       same word as the ~47 that are genuinely broken.
--
-- Two costs. The spend is the obvious one: background LLM calls producing
-- output no surface renders, including summaries that SUCCEEDED for
-- owner-ledger bookkeeping nobody can read. The worse one is the signal —
-- a real planner failure is indistinguishable from an ineligible event.
--
-- ⚠ WHY A NEW VALUE RATHER THAN LEAVING THEM 'pending'.
-- 'pending' means "waiting to be planned". Nothing will ever plan these, so
-- it is the same false statement one word further along — and it would keep
-- them in both partial indexes forever. 'not_applicable' is terminal by
-- construction: the planner claim matches pending/in_progress/timed_out and
-- the summary claim matches pending/retrying, so neither can select it. No
-- claim-query change was needed.
--
-- ⚠ NOT user-visible. The Inbox query filters on ``inbox_like_patterns()``
-- (api/routes/buildium.py), so a row carrying this status is one the frontend
-- never fetches. The TypeScript union is widened for accuracy, not because
-- anything renders it.
--
-- ⚠ Idempotent and non-destructive: the constraints are dropped and re-added
-- with the wider value set, and the backfill touches ONLY rows that are both
-- non-inbox AND not already succeeded — a successfully planned row keeps its
-- history even if its family is ineligible today.

-- ── 1. widen both CHECK constraints ──────────────────────────────────
alter table public.buildium_events
    drop constraint if exists buildium_events_planner_status_check;
alter table public.buildium_events
    add constraint buildium_events_planner_status_check
    check (planner_status in (
        'pending', 'in_progress', 'succeeded', 'timed_out', 'failed',
        'not_applicable'
    ));

alter table public.buildium_events
    drop constraint if exists buildium_events_summary_status_check;
alter table public.buildium_events
    add constraint buildium_events_summary_status_check
    check (summary_status in (
        'pending', 'succeeded', 'failed', 'retrying',
        'not_applicable'
    ));

-- ── 2. backfill the rows that were never eligible ────────────────────
-- The event-name prefixes MUST match inbox_filter.INBOX_EVENT_PREFIXES.
-- Written as a NOT-LIKE-ANY over the same list rather than naming the two
-- families found today, so a third ineligible family is covered without a
-- second migration.
--
-- ⚠ `NOT LIKE ALL`, NOT `NOT LIKE ANY`. This shipped as ANY first and was
-- applied to helixis-test before the error was caught. `x NOT LIKE ANY (arr)`
-- is true when x fails to match *at least one* pattern — which every row does,
-- since no event name matches all twelve prefixes. The result: all 484
-- non-succeeded rows were stamped, including 83 inbox-eligible ones whose
-- genuine 'failed' status (and error text) were overwritten. Repaired in place
-- by restoring rows with planner_attempts > 0; the error strings were not
-- recoverable and carry a marker saying so. ALL is the operator that means
-- "matches none of these".
update public.buildium_events
   set planner_status = 'not_applicable',
       planner_error  = null
 where planner_status not in ('succeeded', 'not_applicable')
   and event_name not like all (array[
        'Task.%', 'WorkOrder.%', 'TenantRequest.%', 'ResidentRequest.%',
        'Lease.%', 'LeaseRenewal.%', 'Applicant.%', 'ApplicantApplication.%',
        'Email.%', 'Helixis.%', 'Risk.%', 'Comms.%'
   ]);

update public.buildium_events
   set summary_status = 'not_applicable',
       summary_error  = null
 where summary_status not in ('succeeded', 'not_applicable')
   and event_name not like all (array[
        'Task.%', 'WorkOrder.%', 'TenantRequest.%', 'ResidentRequest.%',
        'Lease.%', 'LeaseRenewal.%', 'Applicant.%', 'ApplicantApplication.%',
        'Email.%', 'Helixis.%', 'Risk.%', 'Comms.%'
   ]);
