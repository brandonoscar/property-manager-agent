-- The plan ladder became Starter $50 / Pro $199 / Scale $500 (2026-09-04).
-- Every label shifted DOWN one rung, so the stored keys had to move with them
-- or `helixis_billing.plan` would say "starter" for the plan a customer reads
-- as "Pro".
--
-- ⚠ TWO SEPARATE PROBLEMS ARE FIXED HERE, and the second one is a money bug
-- that had nothing to do with the rename.
--
-- 1. THE CHECK CONSTRAINT IS A SECOND SOURCE OF TRUTH FOR THE PLAN KEYS.
--    `helixis_billing_plan_known` allows ('trial','starter','pro') and nothing
--    connected it to `billing/plans.py`. A plan added in Python — as `solo`
--    briefly was, earlier today — passes every unit test, renders in the
--    panel, and reaches Stripe fine; then the webhook writes
--    `plan = coalesce($3, plan)` with the new key, the constraint REJECTS the
--    update, and the whole `customer.subscription.created` handler fails.
--    The customer's card is charged and their plan never activates. Nothing
--    logs it as a pricing problem — it looks like a webhook error.
--    `test_billing_plans.py` now reads BOTH sides (gotcha 28), so the next
--    plan key fails a test instead of a payment.
--
-- 2. The data has to move, and ORDER MATTERS because the old and new names
--    COLLIDE. `starter` must become `pro` while `pro` is becoming `scale`; do
--    it in the wrong order and today's $199 customers land on `scale`, buying
--    a 1,200-turn allowance they never paid for. Highest rung first.
--
-- Count-gated per gotcha 59 — an idempotent-looking `update … where` is a
-- destructive statement. Live state at authoring (2026-09-04):
--     trial 14 · starter 1 (Blue Ridge, active, live subscription) · pro 0
-- So this migration must move exactly 1 row, and `would_move` below is what
-- proves it before anything is written. A re-run moves 0 (the old names no
-- longer exist), which is what makes this safe to apply twice.
--
-- ⚠ migration_check.py CANNOT see this (gotchas 53/59): it matches
-- `create table` / `add column` and this file has neither. Verify by counting.
--     select plan, count(*) from helixis_billing group by plan;
-- must read only trial / starter / pro / scale afterwards.

begin;

-- Count-gate FIRST. Raises with the numbers so a surprise stops the migration
-- rather than being discovered afterwards in the rows it already rewrote.
do $$
declare
    would_move_to_scale int;
    would_move_to_pro   int;
    unknown_keys        int;
begin
    select count(*) into would_move_to_scale from helixis_billing where plan = 'pro';
    select count(*) into would_move_to_pro   from helixis_billing where plan = 'starter';
    select count(*) into unknown_keys
      from helixis_billing
     where plan not in ('trial', 'starter', 'pro', 'scale');

    raise notice 'ladder rename: pro->scale %, starter->pro %, unknown %',
        would_move_to_scale, would_move_to_pro, unknown_keys;

    if unknown_keys > 0 then
        raise exception 'refusing: % row(s) carry a plan key outside the known set', unknown_keys;
    end if;
end $$;

-- The constraint has to come off before either update, because both the old
-- and the new vocabularies are in play mid-statement.
alter table helixis_billing drop constraint if exists helixis_billing_plan_known;

-- HIGHEST RUNG FIRST — see note 2 above. Reversing these two lines silently
-- promotes every $199 customer to the $500 allowance.
update helixis_billing set plan = 'scale' where plan = 'pro';
update helixis_billing set plan = 'pro'   where plan = 'starter';

alter table helixis_billing
    add constraint helixis_billing_plan_known
    check (plan = any (array['trial'::text, 'starter'::text, 'pro'::text, 'scale'::text]));

commit;
