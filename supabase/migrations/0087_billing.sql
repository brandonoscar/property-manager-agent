-- 0087_billing.sql — Stripe subscription state + trial, one row per company.
--
-- Pricing decided 2026-09-02, against MEASURED cost (343 priced runs,
-- 2026-08-26..09-02): mean $0.2143 per chat turn, p50 $0.1068, p99 $1.0884.
-- The mean is 2x the median, so the fat tail is what capacity planning has to
-- pay for.
--
--   trial      14 days, no card
--   starter    $199 / seat / month, 400 turns per seat
--   pro        $500 / account / month, 1200 turns pooled
--
-- ⚠ "Unlimited" is deliberately NOT sold. At $0.2143/turn a flat $500 breaks
-- even around 2,300 turns/month, and an unlimited plan is adversely selected
-- by exactly the customers who would exceed it. `pro` is marketed as
-- unlimited and carries a ceiling nobody normal reaches.
--
-- ⚠ Limits live in CODE (billing/plans.py), not in this table. A cap stored
-- per row is a cap that drifts per customer with no audit trail; a plan NAME
-- stored per row resolves to today's limit for everyone at once. The one
-- override is `turn_limit_override`, which exists for the deal you close by
-- hand and is NULL for everybody else.
--
-- ⚠ There is NO usage counter here on purpose. A billable turn is already a
-- row in orchestrator_runs, and a second counter would be a second source of
-- truth that can disagree with the first — the class of bug gotchas 25/53
-- keep recording. The index at the bottom is what makes counting them cheap.

create table if not exists helixis_billing (
    company_id              uuid primary key references companies(id) on delete cascade,

    plan                    text        not null default 'trial',
    -- Mirrors Stripe's own subscription status so the gate never has to infer
    -- one. 'trialing' is OUR trial (no Stripe object exists yet) as well as
    -- Stripe's; both mean the same thing to the gate.
    status                  text        not null default 'trialing',

    stripe_customer_id      text,
    stripe_subscription_id  text,

    -- Seats BILLED, which is the Stripe subscription quantity — not a live
    -- count of company_users. They diverge the moment somebody is invited,
    -- and billing must follow what Stripe agreed to charge, not what the
    -- team page happens to show.
    seats                   integer     not null default 1,

    trial_ends_at           timestamptz,
    -- The metering window. For a live subscription these track Stripe's
    -- invoice period so usage resets when the customer is actually billed;
    -- during the trial they are the trial's own span.
    period_start            timestamptz not null default now(),
    period_end              timestamptz,

    turn_limit_override     integer,

    -- Set when an invoice fails. The gate keeps serving through it — see
    -- billing/gate.py; losing a card must not read as losing the product.
    past_due_since          timestamptz,

    created_at              timestamptz not null default now(),
    updated_at              timestamptz not null default now(),

    constraint helixis_billing_plan_known
        check (plan in ('trial', 'starter', 'pro')),
    constraint helixis_billing_status_known
        check (status in ('trialing', 'active', 'past_due', 'canceled', 'incomplete')),
    constraint helixis_billing_seats_positive
        check (seats >= 1)
);

-- Stripe sends the customer/subscription id and nothing else on some events,
-- so the webhook resolves the company by these. UNIQUE because two companies
-- sharing one Stripe customer would make that resolution ambiguous, and the
-- database refusing to represent it is better than the webhook picking one
-- (the same reasoning as helixis_twilio_numbers' unique phone_number).
create unique index if not exists helixis_billing_stripe_customer_idx
    on helixis_billing (stripe_customer_id) where stripe_customer_id is not null;
create unique index if not exists helixis_billing_stripe_subscription_idx
    on helixis_billing (stripe_subscription_id) where stripe_subscription_id is not null;

alter table helixis_billing enable row level security;

-- Service-role only, like every other helixis_* table: the app reads through
-- FastAPI on the service key and no client touches PostgREST directly
-- (gotcha 80). RLS on with no policy is fail-CLOSED, which is the safe
-- direction for the table that decides who has paid.

-- ── Metering index ────────────────────────────────────────────────────────
-- Counts billable turns for a company inside a period. Without it the gate
-- scans orchestrator_runs on every chat turn.
create index if not exists orchestrator_runs_company_started_idx
    on orchestrator_runs (company_id, started_at);

-- ── Backfill ──────────────────────────────────────────────────────────────
-- Every existing company gets a trial dated from NOW, not from its signup
-- date. Back-dating would expire the founder's own accounts the moment this
-- lands and lock everyone out of a product they are mid-demo on.
--
-- ⚠ Count-gate this before running it (gotcha 59): the row count here must
-- equal `select count(*) from companies`, and a re-run must insert 0.
insert into helixis_billing (company_id, plan, status, trial_ends_at, period_start, period_end)
select c.id, 'trial', 'trialing', now() + interval '14 days', now(), now() + interval '14 days'
from companies c
on conflict (company_id) do nothing;
