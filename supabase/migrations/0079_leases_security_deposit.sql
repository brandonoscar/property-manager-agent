-- Leases: persist AccountDetails.SecurityDeposit, and backfill from raw.
--
-- WHY
-- ``LeaseAccountDetails.security_deposit`` has been parsed off every lease
-- read since PR #74 and stored nowhere. Nothing reads it: two schema tests
-- assert the parse and that is the field's entire lifetime. So "how much
-- deposit is on this lease" — the first question of any move-out — has never
-- been answerable, while the value sat in the mirror's ``raw`` blob the whole
-- time. Same shape as the bill total (0073) and the bill-line property
-- (0077): the field was fetched and thrown away.
--
-- ⚠ ZERO IS A VALUE, NOT AN ABSENCE — and the split is what makes that
-- load-bearing. Live-verified 2026-08-29 across all 134 mirrored leases:
-- ``SecurityDeposit`` is populated on 134/134 (never null), but one account
-- reads 0.00 on every one of its 75 leases while the other two carry 26/40
-- and 6/19 nonzero, up to $1,800. So the column is NOT NULL-able-by-absence
-- the way a missing key would be; a 0.00 means Buildium was asked and
-- answered zero, and a company at zero across the board is a company that
-- does not track deposits in Buildium rather than 75 tenants who each owe
-- nothing. The read surface says which of those it is; the column just
-- carries the number faithfully.
--
-- ⚠ This is the amount ON THE LEASE AGREEMENT, not a confirmation the money
-- was received and not the balance held in trust. The ledger is the
-- authority for what is held. Every surface that renders this must say so —
-- reporting an agreed figure as an amount held is the confident-false-claim
-- class (gotchas 41/51) pointed at money somebody is owed back.
--
-- ⚠ Idempotent and non-destructive: only NULL rows are touched, so
-- re-running cannot overwrite a value the ingest has since written.
ALTER TABLE helixis_leases
    ADD COLUMN IF NOT EXISTS security_deposit numeric;

UPDATE helixis_leases
   SET security_deposit = (raw -> 'AccountDetails' ->> 'SecurityDeposit')::numeric
 WHERE security_deposit IS NULL
   AND raw -> 'AccountDetails' ->> 'SecurityDeposit' IS NOT NULL;
