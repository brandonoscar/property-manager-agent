-- 0071: detector findings → Inbox bridge marker.
-- APPLIED to helixis-test (abcdefghijklmnopqrst) 2026-08-20.
--
-- bridged_at stamps a risk flag once it has been projected into the
-- Inbox pipeline as a Risk.FlagRaised event (detectors/inbox_bridge.py).
-- NULL = not yet bridged; the bridge selects on it so each flag row is
-- bridged exactly once per lifetime (a resolved→new reactivation does
-- NOT re-bridge — deliberate, avoids re-spamming the Inbox).
alter table helixis_risk_flags
  add column if not exists bridged_at timestamptz;

-- The bridge's per-cycle scan filters on (company_id, status, bridged_at
-- IS NULL); partial index keeps it O(unbridged) instead of O(all flags).
create index if not exists idx_risk_flags_unbridged
  on helixis_risk_flags (company_id)
  where bridged_at is null and status in ('new', 'confirmed');
