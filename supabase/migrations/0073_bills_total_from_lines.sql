-- Backfill helixis_bills.total_amount from the line items.
--
-- Production Buildium bills carry NO top-level TotalAmount (live-verified
-- 2026-08-21: 0 of 131 mirrored bills had one), so every row landed NULL and
-- the mirror-backed `unpaid_bills` tool reported the entire payables book as
-- $0.00 — a confident, wrong number in front of a property manager.
--
-- upsert_bill now derives the total from Lines[].Amount on ingest, and the
-- read path COALESCEs to the lines as a second line of defence. This fixes
-- the rows already mirrored, so the correction lands without waiting for a
-- resync or a webhook touch on each bill.
--
-- Only fills NULLs: a bill that genuinely has a Buildium-supplied total keeps
-- it, and a bill with no lines and no total stays NULL, because NULL means
-- "unknown" and 0.00 would launder a missing amount into a real-looking
-- figure.

UPDATE helixis_bills b
   SET total_amount = s.line_total,
       updated_at   = now()
  FROM (
        SELECT company_id, bill_id, SUM(amount) AS line_total
          FROM helixis_bill_lines
         WHERE amount IS NOT NULL
         GROUP BY company_id, bill_id
       ) s
 WHERE b.company_id  = s.company_id
   AND b.buildium_id = s.bill_id
   AND b.total_amount IS NULL;
