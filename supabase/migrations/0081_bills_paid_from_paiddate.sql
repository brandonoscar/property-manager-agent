-- Bills: backfill is_paid from raw PaidDate.
--
-- WHY
-- Buildium sends ``IsPaid`` as a KEY on every production bill and NULL on
-- every one of them; ``PaidDate`` is the field that carries the answer.
-- ``buildium/money.py::bill_is_paid`` was fixed for exactly this in #855
-- (merged 2026-08-21T19:56Z) and handles every bill from that point on.
--
-- The already-mirrored rows were never re-upserted. Live-verified
-- 2026-08-30: ``raw->>'PaidDate'`` is populated on **32 of 131** bills while
-- ``is_paid`` is true on **0** — and those 32 were all written in one batch
-- at 2026-08-21T18:01:19Z, roughly two hours BEFORE #855 merged. So the
-- ingest is correct and this is purely the historical rows, the same shape
-- as 0073 (bill totals) and 0077 (bill line property).
--
-- Consequence while it stood: ``unpaid_bills`` filters ``is_paid IS NOT
-- TRUE``, so 32 paid bills were being reported as outstanding money — and
-- ``_paid_bill_count`` saw zero paid bills, which pins the
-- ``_NO_PAYMENT_HISTORY`` caveat ON. That caveat is COMPUTED (gotcha 57), so
-- it turns itself off once these rows land; nothing else has to change.
--
-- ⚠ NULL is left alone deliberately. ``is_paid IS NOT TRUE`` already treats
-- NULL as unpaid, so writing FALSE across the other 99 rows buys no
-- behaviour and asserts "definitely unpaid" about rows whose paid status was
-- never re-derived. Unknown must not collapse into a claim (gotcha 57).
--
-- ⚠ Idempotent and one-directional: only rows that are NOT already true and
-- that carry a real PaidDate are touched, so re-running converges and this
-- can never mark a bill unpaid.
--
-- ⚠ Before applying, run the WHERE clause as a count and check it against
-- what you expect to change — an idempotent-looking UPDATE is a destructive
-- statement (gotcha 59, where a wrong operator stamped 484 rows):
--
--     SELECT count(*) FROM helixis_bills
--      WHERE is_paid IS NOT TRUE AND raw->>'PaidDate' IS NOT NULL;   -- expect 32
UPDATE helixis_bills
   SET is_paid    = true,
       updated_at = now()
 WHERE is_paid IS NOT TRUE
   AND raw->>'PaidDate' IS NOT NULL;
