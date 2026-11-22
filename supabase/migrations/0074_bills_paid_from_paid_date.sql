-- Backfill helixis_bills.is_paid from the PaidDate the payload actually
-- carries.
--
-- Production Buildium sends ``IsPaid`` as a key on every bill and NULL as its
-- value on every one of them (live-verified across all 131 mirrored rows,
-- 2026-08-21). Storing that verbatim left is_paid NULL everywhere, so the
-- mirror's ``WHERE is_paid IS NOT TRUE`` counted all 131 bills as outstanding
-- when 32 of them were already paid — a payables figure roughly $10.9k too
-- high, on top of the $0 bug that 0073 fixed.
--
-- ``PaidDate`` is the field that carries the answer: null = unpaid, set =
-- paid. upsert_bill now derives is_paid the same way on ingest.
--
-- Only fills NULLs, so a bill Buildium ever does flag explicitly keeps that
-- value.

-- Deliberately does NOT touch updated_at. That column is the mirror's
-- freshness signal — `_freshness_line` reports it to the user as "synced N
-- minutes ago" — so a backfill stamping now() makes stale data claim to be
-- fresh. 0073 did stamp it, which is why every bill currently reads as just
-- synced; don't compound it.

UPDATE helixis_bills
   SET is_paid = (raw->>'PaidDate') IS NOT NULL
 WHERE is_paid IS NULL
   AND raw ? 'PaidDate';
