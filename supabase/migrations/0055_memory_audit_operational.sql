-- 0055: memory_audit — allow OPERATIONAL (system-derived) writes (#534 Stage B).
--
-- Stage B derives advisory operational signals (late-payer, balance) in the
-- webhook worker with NO human user attached. The memory_audit trail should
-- still capture these (SOC2 traceability), but today user_id is NOT NULL and
-- the action CHECK omits the system case, so a user-less write can't be logged
-- (write_audit early-returns on a missing user_id, gotcha in 0002-era design).
--
-- Two minimal, backward-compatible relaxations:
--   1. user_id becomes nullable — a system/operational write has no auth.users
--      row to attribute. Existing human writes still pass a UUID unchanged.
--   2. the action CHECK gains 'operational' — the distinct verb for a
--      deterministic worker-derived signal write.
--
-- Both are widening changes: every existing row + every existing code path
-- remains valid. Reads/RLS are untouched.

alter table public.memory_audit
    alter column user_id drop not null;

alter table public.memory_audit
    drop constraint if exists memory_audit_action_check;

alter table public.memory_audit
    add constraint memory_audit_action_check
    check (action in ('add', 'search', 'delete', 'clear', 'delete_company', 'operational'));
