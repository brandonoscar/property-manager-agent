-- Backfill helixis_bill_lines.gl_account_id from the NESTED GLAccount object.
--
-- WHY
-- ---
-- Buildium sends both keys on every bill line and populates only one of them.
-- Live-verified across all 131 mirrored lines (2026-08-28):
--
--     GLAccountId       present 131/131, populated   0/131
--     GLAccount.Id      present 131/131, populated 131/131
--
-- ``upsert_bill`` wrote the flat field straight through, so every row in this
-- table has a NULL category and ``spending_report``'s GL grouping renders one
-- bucket, "Uncategorized", for the entire payables book.
--
-- This is the SECOND column on THIS TABLE with exactly this defect — 0077 did
-- the same for property_id, from AccountingEntity.Id. The code fix
-- (``BillLine.resolved_gl_account_id``) covers rows synced from now on; this
-- covers everything already mirrored.
--
-- ⚠ APPLY BY HAND. There is no migration runner (gotcha 8) — a file in this
-- repo is not proof it is live. Until it runs, already-synced lines stay
-- uncategorised and the code fix changes nothing a manager can see:
--
--     psql "$env:HELIXIS_SUPABASE__DB_URL" -f supabase/migrations/0078_bill_lines_gl_from_nested.sql
--
-- ⚠ NO entity-type guard, deliberately — unlike 0077. A GL account is a GL
-- account whether the line is charged to a rental or to company overhead; the
-- Rental-only rule in 0077 exists because a Company id is not a property id,
-- and there is no equivalent confusion for a ledger code.

UPDATE helixis_bill_lines bl
   SET gl_account_id = sub.gl_id
  FROM (
        SELECT b.company_id,
               b.buildium_id                              AS bill_id,
               (ln.ordinality - 1)::int                   AS line_index,
               (ln.value -> 'GLAccount' ->> 'Id')::bigint AS gl_id
          FROM helixis_bills b
         CROSS JOIN LATERAL jsonb_array_elements(b.raw -> 'Lines')
                         WITH ORDINALITY AS ln(value, ordinality)
         WHERE jsonb_typeof(b.raw -> 'Lines') = 'array'
           AND ln.value -> 'GLAccount' ->> 'Id' IS NOT NULL
       ) AS sub
 WHERE bl.company_id = sub.company_id
   AND bl.bill_id    = sub.bill_id
   AND bl.line_index = sub.line_index
   -- Only fill what is genuinely empty. If Buildium ever starts populating the
   -- flat key, the value already stored is the authoritative one and must not
   -- be overwritten by this backfill on a later re-run.
   AND bl.gl_account_id IS NULL;

-- Verification (run after; expect populated == total for every company):
--   SELECT company_id,
--          COUNT(*)                                        AS lines,
--          COUNT(*) FILTER (WHERE gl_account_id IS NOT NULL) AS categorised
--     FROM helixis_bill_lines
--    GROUP BY company_id;
