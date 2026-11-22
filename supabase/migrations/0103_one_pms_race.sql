-- `assert_one_pms_per_company` is a cross-table check-then-act, and two
-- concurrent connects both pass it. ADR 0010 Decision (a).
--
-- WHY THIS IS NOT A TIDINESS FIX
-- Migration 0100 states the stakes itself: both systems' record ids live in the
-- SAME `buildium_id` column under `UNIQUE (company_id, buildium_id)`, so if a
-- company ever held both, a colliding id "would SILENTLY OVERWRITE one with the
-- other — no error, no log, one row where there were two". That whole design is
-- safe only while the one-PMS invariant HOLDS. It does not hold under
-- concurrency: the trigger reads the other credentials table, finds nothing
-- because the other transaction has not committed yet, and allows the insert.
-- Classic write skew, and READ COMMITTED — which is what this product runs — is
-- exactly the isolation level that permits it.
--
-- ⚠ MEASURED, NOT ARGUED. Probed against real PostgreSQL 16.13 at
-- `default_transaction_isolation = read committed`, driving it with the same
-- asyncpg 0.31.0 the product uses, with the trigger function EXTRACTED FROM
-- 0100 rather than retyped (a hand-copied probe tests the transcription).
--
--   AS SHIPPED
--     CONTROL (sequential):  REFUSED — "company … is already connected to
--                            Buildium"      <- the trigger does work, so the
--                                              race result below means something
--     RACE (concurrent):     before A commits, B's insert is: completed
--                            2/2 commits landed, company holds BOTH PMSes: TRUE
--     CONSEQUENCE:           two distinct properties inserted at the same
--                            record id, mirror holds 1 row:
--                            buildium_id=4610 name='Garden Row (Rentvine)'
--                            — the Buildium property is gone, silently.
--   WITH THIS FIX
--     CONTROL (sequential):  REFUSED (unchanged — the fix does not work by
--                            breaking the path that already worked)
--     RACE (concurrent):     before A commits, B's insert is: still blocked
--                            1/2 landed, company holds BOTH PMSes: FALSE
--
-- The CONTROL is what makes the race evidence rather than a coincidence: a
-- trigger that had stopped firing altogether would produce "0 refusals" on the
-- race too, and read identically.
--
-- ⚠ THE FIRST PROBE WAS NOT A RACE AND REPORTED THE OPPOSITE. It committed each
-- side before starting the other, so it re-ran the sequential control twice and
-- said the invariant held. What makes it a race is that BOTH existence checks
-- must run before EITHER transaction commits. A probe that does not do what its
-- name says reports a wrong finding at full confidence.
--
-- ⚠ WHY AN ADVISORY LOCK AND NOT `SELECT … FROM companies FOR UPDATE`, WHICH IS
-- THE OBVIOUS SPELLING. Both serialise the two connects; they differ in what
-- ELSE they block, and that was measured too. Every mirror table has a foreign
-- key to `companies`, so inserting ANY mirror row takes a FOR KEY SHARE lock on
-- that company row — which FOR UPDATE conflicts with:
--
--     FOR UPDATE on companies blocks an unrelated sync insert: TRUE
--     advisory xact lock blocks an unrelated sync insert:      FALSE
--
-- So the row lock would stall a customer's running sync for the length of a
-- connect, and a long sync would stall a connect. The advisory lock lives in its
-- own namespace and conflicts only with the other connect, which is the whole
-- and only thing it needs to do.
--
-- ⚠ It cannot be a UNIQUE INDEX. Exclusivity spans TWO tables and no index does
-- — 0100 already records that naming a mechanism which cannot exist is how an
-- invariant ends up enforced by nothing.
--
-- ⚠ `hashtext` is int4, so two different company ids can collide and serialise
-- against each other. That is harmless: the cost is that two unrelated connects
-- take turns, and a connect is a rare interactive operation.
--
-- ⚠ TRANSACTION-SCOPED (`_xact_`), never session-scoped. A session-scoped
-- advisory lock outlives the transaction and must be released by hand; a
-- connection returned to the pool still holding it would block every later
-- connect for that company until the process restarted — trading a rare race
-- for a permanent stall.
--
-- The lock is taken BEFORE the existence check and that order is the entire
-- fix; taken after, it serialises nothing that matters. `tests/unit/
-- test_one_pms_race.py` asserts the order rather than the presence, because a
-- lock in the wrong place reads identically in a grep.
--
-- Nothing else changes: same function name, same signature, same triggers, same
-- error, same fail-closed behaviour. This is `create or replace`, so it is
-- idempotent and needs no trigger churn.

begin;

create or replace function public.assert_one_pms_per_company()
returns trigger
language plpgsql
as $$
declare
    other_pms text;
begin
    -- ⚠ FIRST, and that is the fix. Serialises concurrent connects for THIS
    -- company only. Without it the existence check below reads a snapshot that
    -- cannot see the other transaction's uncommitted row, both connects pass,
    -- and the company ends up holding two PMSes whose record ids then overwrite
    -- each other in the mirror with no error and no log (migration 0100).
    perform pg_advisory_xact_lock(hashtext('one_pms:' || new.company_id::text));

    -- Which table is the OTHER one depends on which table fired us, so the
    -- function is written once and bound to both.
    if tg_table_name = 'company_rentvine_credentials' then
        if exists (
            select 1 from public.company_buildium_credentials
            where company_id = new.company_id
        ) then
            other_pms := 'Buildium';
        end if;
    elsif tg_table_name = 'company_buildium_credentials' then
        if exists (
            select 1 from public.company_rentvine_credentials
            where company_id = new.company_id
        ) then
            other_pms := 'Rentvine';
        end if;
    end if;

    if other_pms is not null then
        raise exception using
            errcode = 'unique_violation',
            message = format(
                'company %s is already connected to %s; one PMS per company '
                '(ADR 0010 a1). Disconnect the existing PMS first.',
                new.company_id, other_pms
            );
    end if;
    return new;
end;
$$;

commit;

-- ---------------------------------------------------------------------------
-- NOT APPLIED. There is no migration runner (CLAUDE.md §4) and no database was
-- reachable from the session that wrote this, so a file in the repo is NOT
-- proof it is live (gotcha 8). `migration_check.py` CANNOT see this one — it
-- verifies migrations by matching `create table` / `add column` against the
-- live catalog, and this adds neither, so the schema sweep reports green
-- whether or not it was ever applied (gotchas 53/59).
--
-- The standing check is BEHAVIOURAL, because a catalog read can show the
-- function's source and cannot show that it serialises anything:
--
--     select prosrc like '%pg_advisory_xact_lock%'
--       from pg_proc where proname = 'assert_one_pms_per_company';
--
-- proves only that this file was applied. Proving the race is closed needs the
-- two-connection probe above, re-run against the live database.
-- ---------------------------------------------------------------------------
