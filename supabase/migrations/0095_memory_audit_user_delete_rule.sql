-- memory_audit.user_id — give the FK an explicit delete rule.
--
-- WHAT WAS WRONG
-- --------------
-- 0023 declared the table with two foreign keys and only ONE of them carries a
-- delete rule:
--
--     company_id  uuid not null references public.companies(id) on delete cascade,
--     user_id     uuid not null references auth.users(id),          -- <- no rule
--
-- An omitted `on delete` is NO ACTION, which REFUSES the parent delete while a
-- child row exists. So deleting an auth user who has ever touched memory fails
-- with a foreign key violation. Measured on helixis-test 2026-09-09: 902
-- memory_audit rows, 881 carrying a user, across 7 DISTINCT users out of 15 —
-- so nearly half the accounts on the project could not be deleted at all.
--
-- It has not bitten yet only because no product code path deletes an auth user
-- (account deletion removes the company and its membership; the auth user is
-- removed by hand). The right-to-erasure path is where it lands next.
--
-- This was the ONE outlier: of the 54 foreign keys pointing at public.companies
-- or auth.users, 49 are CASCADE, 4 are a deliberate SET NULL on attribution
-- columns (uploaded_by / created_by / updated_by / chat user_id), and this was
-- the only NO ACTION. It reads as an omission rather than a decision — the
-- column beside it in the same CREATE TABLE got a rule.
--
-- WHY SET NULL AND NOT CASCADE
-- ----------------------------
-- memory_audit is the SOC2 trail for the memory subsystem, and the two
-- obligations pull in opposite directions: erasure says remove the person,
-- retention says keep the record of what happened. SET NULL satisfies both —
-- the row keeps WHAT happened, WHEN, and in WHICH company, and loses only the
-- personal identifier.
--
-- Two facts make this safe rather than a compromise:
--
--   * The RLS policies scope on `company_id`, never `user_id` (0023 lines
--     57/68 join company_users on the COMPANY). So a null user_id cannot widen
--     or break either policy — checked before choosing, not assumed.
--   * `company_id` already cascades, so deleting a COMPANY still takes its
--     audit rows with it. The trail survives a USER deletion only, which is
--     exactly the case the two obligations disagree about.
--
-- ⚠ CASCADE would have matched helixis_mfa_backup_codes (0094), and that
-- precedent does NOT transfer: a backup code is a live credential that must
-- not outlive its owner, while an audit row is a record of a past event. The
-- four existing SET NULL columns are the right precedent.
--
-- ⚠ `drop not null` is required and is not incidental — SET NULL cannot fire
-- against a NOT NULL column, so leaving the constraint would keep the delete
-- blocked while the FK rule claimed otherwise: a fix that reads as applied and
-- is not (gotcha 70's shape).
--
-- NO BACKFILL. This changes what happens on a FUTURE delete; it rewrites no
-- existing row. Gotcha 59's count-gate is satisfied trivially — the predicate
-- would match 0 rows because there is no predicate.
--
-- Idempotent: the constraint swap runs only when the live rule is not already
-- SET NULL, so a re-run is a no-op rather than a duplicate-constraint error.
--
--   psql "$HELIXIS_SUPABASE__DB_URL" -f supabase/migrations/0095_memory_audit_user_delete_rule.sql

begin;

alter table public.memory_audit alter column user_id drop not null;

do $$
begin
    -- confdeltype 'n' = SET NULL. Keyed on the LIVE rule rather than on
    -- whether the constraint exists, so this repairs a NO ACTION constraint
    -- and skips one already repaired.
    if exists (
        select 1
          from pg_constraint
         where conname = 'memory_audit_user_id_fkey'
           and conrelid = 'public.memory_audit'::regclass
           and confdeltype <> 'n'
    ) then
        alter table public.memory_audit
            drop constraint memory_audit_user_id_fkey;
    end if;

    if not exists (
        select 1
          from pg_constraint
         where conname = 'memory_audit_user_id_fkey'
           and conrelid = 'public.memory_audit'::regclass
    ) then
        alter table public.memory_audit
            add constraint memory_audit_user_id_fkey
            foreign key (user_id) references auth.users(id) on delete set null;
    end if;
end $$;

comment on column public.memory_audit.user_id is
    'Who performed the memory operation. NULLABLE and ON DELETE SET NULL: '
    'erasing the person must not be blocked by the audit trail, and the trail '
    'keeps what happened without keeping who. See 0095.';

commit;
