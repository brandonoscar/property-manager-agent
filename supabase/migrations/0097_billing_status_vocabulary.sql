-- `helixis_billing.status` could not store three of Stripe's eight subscription
-- statuses, and the write that hit the constraint discarded the whole event.
--
-- 0087 wrote the list once — trialing / active / past_due / canceled /
-- incomplete — and the code grew past it. Stripe documents EIGHT. The three
-- missing ones are the three that matter:
--
--   incomplete_expired  the first payment definitively failed. `gate.py` has
--                       carried the correct message for this the whole time
--                       ("that subscription was never completed"), and the row
--                       could never hold the status that reaches it — so a
--                       customer whose card failed at checkout was told "your
--                       bank is still confirming, this usually clears within a
--                       few minutes", forever.
--
--   unpaid              Stripe exhausted dunning and stopped collecting. The
--                       row kept `past_due`, which IS in the gate's serving
--                       set, so the product was served FREE and indefinitely
--                       to a customer nobody is billing. This is the worst of
--                       the three and it is silent by construction.
--
--   paused              collection paused; the row keeps whatever it had.
--
-- ⚠ THE BLAST RADIUS IS NOT THE STATUS COLUMN. `_apply_event` writes the
-- subscription id, the customer id, the plan, the seat count, the metering
-- window and the ordering stamp in ONE statement. A CHECK violation on one
-- column raises; `stripe_webhook` swallows it — correctly, because a raise
-- makes Stripe retry for days and then disable the endpoint — and every other
-- field in that event is lost with it. So the metering window silently stops
-- rolling for that company.
--
-- ⚠ THIS MIGRATION IS HALF THE FIX. The other half is in Python: an
-- unrecognised status is now written as NULL and COALESCEd, so a status Stripe
-- invents AFTER this file keeps the last known value instead of failing the
-- whole update again. The constraint is a guard against a typo'd literal, not
-- the thing that has to be right about the future.
--   src/helixis/billing/stripe_status.py holds the ONE list;
--   tests/unit/test_stripe_status_vocabulary.py pins this constraint against
--   it, because a vocabulary typed out in two places is exactly the drift that
--   produced the defect (gotcha 28 — read both sides).
--
-- ⚠ Purely widening: every previously-legal value stays legal, so no existing
-- row can violate the new constraint and there is nothing to back-fill. That
-- is what makes it safe to apply without a count-gate; the count below is run
-- anyway, because gotcha 59's rule is to look before writing rather than to
-- look only when you expect a surprise.
--
-- ⚠ migration_check.py CANNOT see this (gotchas 53/59): no `create table`, no
-- `add column`. Verify by effect —
--     select conname, pg_get_constraintdef(oid) from pg_constraint
--      where conrelid = 'helixis_billing'::regclass
--        and conname = 'helixis_billing_status_known';
-- must list all eight, and an insert of 'unpaid' must be accepted.

begin;

-- Look before writing (gotcha 59). Every row must already be legal under the
-- WIDER list, or something wrote a status no build recognises.
do $$
declare
    total   int;
    unknown int;
begin
    select count(*) into total from helixis_billing;
    select count(*) into unknown from helixis_billing
     where status not in ('incomplete', 'incomplete_expired', 'trialing',
                          'active', 'past_due', 'canceled', 'unpaid', 'paused');
    raise notice 'billing status vocabulary: % rows, % outside the new list', total, unknown;
    if unknown > 0 then
        raise exception 'refusing: % rows carry a status the widened constraint '
                        'still rejects — investigate before widening', unknown;
    end if;
end $$;

alter table helixis_billing drop constraint if exists helixis_billing_status_known;

alter table helixis_billing
    add constraint helixis_billing_status_known
    check (status = any (array[
        'incomplete'::text,
        'incomplete_expired'::text,
        'trialing'::text,
        'active'::text,
        'past_due'::text,
        'canceled'::text,
        'unpaid'::text,
        'paused'::text
    ]));

comment on column helixis_billing.status is
    'Stripe''s own subscription status, verbatim. All eight documented values '
    'are storable; an unrecognised one is NOT written (the column keeps its '
    'previous value) rather than clamped to a guess — see '
    'src/helixis/billing/stripe_status.py. gate.py decides which of these are '
    'served: unpaid, paused, canceled, incomplete and incomplete_expired are '
    'not.';

commit;
