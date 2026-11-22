-- 0039_mirror_probe_promotions.sql — columns promoted after the 2026-07-05
-- sandbox probe verified 0038 against real Buildium payloads.
--
-- The probe (scripts/buildium_sandbox_probe.py, individual sandbox) flagged
-- three raw-only fields worth typing:
--   * properties.IsActive        → cockpit active-property filtering
--   * units.IsUnitOccupied       → Buildium's own occupancy flag, cross-checks
--                                  the active-lease heuristic
--   * outstanding balances aging → Balance0To30Days / 31To60 / 61To90 /
--                                  Over90Days, the "60+ days delinquent" view
--
-- Additive only; NULL for rows mirrored before this landed (the next
-- backfill/webhook upsert fills them). Applied to helixis-test 2026-07-05
-- via the Supabase migration API (also recorded in the remote ledger).

begin;

alter table public.helixis_properties
    add column if not exists is_active boolean;

alter table public.helixis_units
    add column if not exists is_occupied boolean;

alter table public.helixis_lease_balances
    add column if not exists balance_0_30    numeric(12, 2),
    add column if not exists balance_31_60   numeric(12, 2),
    add column if not exists balance_61_90   numeric(12, 2),
    add column if not exists balance_over_90 numeric(12, 2);

commit;
