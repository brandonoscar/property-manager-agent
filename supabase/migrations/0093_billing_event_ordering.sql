-- Stop a REDELIVERED Stripe event from resurrecting a cancelled subscription.
--
-- THE FAILURE THIS PREVENTS, which is silent and permanent:
--
--   1. `customer.subscription.updated` is delivered and our endpoint is down
--      (a deploy, a 500, a Render restart). Stripe queues a retry.
--   2. The customer cancels. `customer.subscription.deleted` arrives and
--      succeeds — the row goes status='canceled', plan='trial'. Correct.
--   3. An hour later Stripe retries step 1. It applies cleanly, because every
--      handler is an unconditional UPDATE, and the row goes back to
--      status='active', plan='pro'.
--
-- The customer now has a full paid account for nothing, forever, and no error
-- or log line anywhere says so — every statement did exactly what it was told.
-- Stripe retries with backoff for up to three days, so the window for this is
-- not theoretical, and `_warn_if_nothing_matched` cannot see it either: the
-- update matched a row, which is what it checks for.
--
-- ⚠ ORDERING, NOT DEDUPLICATION, IS THE THING THAT MATTERS. An exact
-- redelivery of the same event is harmless here — every handler is idempotent
-- and re-applying it writes the same values. It is an OLD event landing after
-- a NEWER one that corrupts the row. So this is one timestamp column and not
-- a table of processed event ids: the smaller mechanism is the one that
-- addresses the actual defect, and an event-id ledger would need its own
-- retention story on the busiest table Stripe writes to.
--
-- ⚠ NULL means "no event has been applied to this row yet" and must be read as
-- ALLOW, never as stale. Every company created by the 0088 trigger starts here,
-- so reading NULL as "older than everything" would refuse the very first
-- subscription event a new customer ever produces — turning a paid signup into
-- a permanently trialling account, which is this defect pointed the other way.
--
-- Comparison is STRICTLY greater-than in the application, so two events sharing
-- one `created` second both apply. Stripe routinely emits `invoice.paid` and
-- `customer.subscription.updated` in the same second off one charge, and
-- dropping the second of those would leave the metering window unrolled — the
-- costliest miss the webhook handler names.

alter table public.helixis_billing
    add column if not exists last_stripe_event_at timestamptz;

comment on column public.helixis_billing.last_stripe_event_at is
    'Stripe `event.created` of the newest webhook event applied to this row. '
    'An event older than this is a delayed retry and is ignored, so a queued '
    'subscription.updated cannot land after a subscription.deleted and hand '
    'back a cancelled plan. NULL = nothing applied yet, which ALLOWS.';
