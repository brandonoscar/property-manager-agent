-- 0088_billing_row_on_company_create.sql — every new company gets a trial row.
--
-- 0087 backfilled the companies that existed on 2026-09-02 and nothing gives a
-- row to a company created after it. That is invisible rather than loud,
-- because `billing/gate.py` treats a missing row as ALLOW — deliberately, so a
-- database blip can never lock a paying customer out (its rule 1). The two
-- correct decisions compose into a wrong one: with HELIXIS_BILLING__ENFORCE
-- true, every signup after the backfill would be an unmetered, unlimited,
-- free account, and no test or alert would say so.
--
-- ⚠ A TRIGGER ON `companies`, NOT AN INSERT AT EACH CALLER. Five paths create
-- a company today — the `handle_new_user` signup trigger (0012),
-- `api/routes/auth.py`'s bootstrap self-heal, `evals/live/seeder.py`,
-- `scripts/seed.py` and `scripts/create_user.py` — and adding the insert to
-- each is the fan-out gotcha 25 exists to stop: a sixth caller added later
-- re-opens the hole silently, which is exactly how this gap was born. All five
-- end at one `insert into public.companies`, so the rule lives there once and
-- cannot be missed by construction.
--
-- ⚠ IT SWALLOWS ITS OWN FAILURE, and that is the deliberate direction. A
-- company with no billing row is a KNOWN, HANDLED state (the gate allows and
-- logs); a signup that 500s is unrecoverable for that person and takes out the
-- front door of the product. So a billing failure must never be able to fail a
-- company insert — same asymmetry the gate itself is built on. The warning is
-- the signal; `helixis_billing_missing` below is how you find it after the
-- fact.

create or replace function public.grant_billing_trial()
returns trigger
language plpgsql
security definer
set search_path to 'public'
as $function$
begin
    -- 14 days mirrors billing.plans.TRIAL_DAYS. It is a literal because SQL
    -- cannot read the Python constant; `test_billing_trial_provisioning.py`
    -- pins the two together so the interval here cannot drift from the value
    -- the product quotes to the customer.
    insert into public.helixis_billing
        (company_id, plan, status, trial_ends_at, period_start, period_end)
    values
        (NEW.id, 'trial', 'trialing', now() + interval '14 days', now(),
         now() + interval '14 days')
    on conflict (company_id) do nothing;

    return NEW;
exception when others then
    -- See the header: a missing billing row degrades, a failed signup does not.
    raise warning 'grant_billing_trial: no billing row for company % (%)',
        NEW.id, sqlerrm;
    return NEW;
end;
$function$;

drop trigger if exists companies_grant_billing_trial on public.companies;

create trigger companies_grant_billing_trial
    after insert on public.companies
    for each row
    execute function public.grant_billing_trial();

-- ── Backfill the window ───────────────────────────────────────────────────
-- Any company created between 0087 and this migration. Same statement 0087
-- used, so it is idempotent and a re-run inserts 0.
--
-- ⚠ Count-gate it first (gotcha 59), and note that `migration_check.py` CANNOT
-- see this half: it verifies migrations by matching `create table` /
-- `add column` against the live DB, and a data-only statement adds neither, so
-- the schema sweep reports green whether this ran or not (gotchas 53/59). The
-- check that actually works is the view below reading 0 before AND after.
insert into helixis_billing (company_id, plan, status, trial_ends_at, period_start, period_end)
select c.id, 'trial', 'trialing', now() + interval '14 days', now(), now() + interval '14 days'
from companies c
on conflict (company_id) do nothing;

-- ── The standing check ────────────────────────────────────────────────────
-- Names the failure this migration exists to prevent, so finding it later is
-- one query rather than a join somebody has to think of. Must read 0 rows.
create or replace view helixis_billing_missing as
select c.id as company_id, c.name, c.created_at
from companies c
left join helixis_billing b on b.company_id = c.id
where b.company_id is null;
