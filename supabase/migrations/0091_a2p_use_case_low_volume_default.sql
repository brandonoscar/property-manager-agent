-- The A2P use-case column defaulted to the EXPENSIVE tier.
--
-- 0084 shipped `use_case text NOT NULL DEFAULT 'MIXED'`. On 2026-09-02 the
-- product moved to LOW_VOLUME (`comms/a2p_defaults.USE_CASE`) as a PRICE
-- decision — under white-label the carrier fees are per CUSTOMER per month and
-- never amortise:
--
--     MIXED (Standard)   $46 one-time + $10/month
--     LOW_VOLUME          $4.50 one-time + $1.50/month
--
-- At 50 customers that is ~$425/month and ~$2,075 of one-time fees. The column
-- default was never moved with it, so the schema still names the tier the
-- change existed to stop paying for.
--
-- ⚠ LATENT, NOT LIVE — state it accurately or the next reader mis-sizes it.
-- `api/routes/a2p.py` writes `use_case` explicitly on both the INSERT and the
-- upsert's EXCLUDED list, so every registration created through the app has
-- always been LOW_VOLUME. This default governs only a row inserted by some
-- other path. It is fixed because a fallback that names the most expensive
-- option is wrong in the direction that costs money silently, not because
-- anyone has been overcharged.
--
-- ⚠ COUNT-GATED FIRST, per gotcha 59 — an idempotent-looking `update … where`
-- is a destructive statement, so its predicate was run as a SELECT before this
-- file was written. Live helixis-test, 2026-09-04:
--
--     live_column_default | total_rows | would_backfill
--     'MIXED'::text       |          0 |              0
--
-- So the UPDATE below is expected to touch ZERO rows today. It is here anyway
-- because this file may be applied to an environment that already has rows,
-- and a schema change that leaves existing data on the dear tier would be the
-- half-fix that reads as a whole one.
--
-- ⚠ `migration_check.py` CANNOT SEE THIS FILE (gotchas 53/59): it adds no
-- table and no column, so the schema sweep reports green whether or not this
-- was ever applied. The standing check is the first query below — the live
-- default must read 'LOW_VOLUME'::text.

alter table public.helixis_a2p_registrations
    alter column use_case set default 'LOW_VOLUME';

-- Any pre-existing row still on the dear tier. Scoped to rows that have NOT
-- yet been submitted: once a registration is with the carrier its use case is
-- what the carrier is vetting, and rewriting it here would make our record
-- disagree with theirs — a worse state than an expensive one.
update public.helixis_a2p_registrations
   set use_case = 'LOW_VOLUME'
 where use_case = 'MIXED'
   and status in ('draft', 'submitted');
