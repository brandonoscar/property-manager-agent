-- 0098 — a company's own timezone, because "today" is a per-company fact.
--
-- WHAT WAS WRONG
--
-- The product answered "what day is it" in three different places and only one
-- of them was about the customer:
--
--   * `agents/clock_prompt` — a LOCAL date, hardcoded `America/New_York`, with
--     the reason written into its own docstring: "A property manager's 'today'
--     is local: a move-out dated 2026-09-30 is a different lease month
--     depending on which side of midnight you compute it from."
--   * `reminders/producers` — `date.today()`, the SERVER's clock. No `TZ` is
--     set in render.yaml, the Dockerfile or any workflow, so that is UTC.
--   * `comms/local_time` — per RECIPIENT, derived from the phone number's NPA,
--     for the TCPA quiet-hours window.
--
-- So the rule was written down in the first and never reached the second —
-- gotcha 25's shape ("fix it at every layer that reads it"), and the FIFTH
-- instance of gotchas 68/69/72/73's class: a deployment-global value that is
-- really a per-company fact, which only becomes visibly wrong once a second
-- customer exists in a different zone.
--
-- MEASURED, not argued. Hours per day on which the server's date is not the
-- manager's, computed across the US zones:
--
--     America/New_York      4h/day   (17% of every day)
--     America/Chicago       5h/day   (21%)
--     America/Denver        6h/day   (25%)
--     America/Los_Angeles   7h/day   (29%)
--     America/Anchorage     8h/day   (33%)
--     Pacific/Honolulu     10h/day   (42%)
--
-- What that buys, on the three REGISTERED producers:
--
--   * `bills_due`  — `overdue = due < date.today()`. A bill due TODAY is
--     reported OVERDUE for the whole of that window, every evening. A false
--     claim about money, which is this product's worst failure mode (gotcha 26)
--     pointed at the one subject a property manager will act on immediately.
--   * `tasks_overdue` — `days_over` reads one day higher than the manager's
--     calendar, and `severity = "high" if days_over >= 7` therefore flips a day
--     early.
--   * `renewal_window` — the 90/60/30 escalation ladder flips a day early.
--
-- Two more are DARK and are hardening rather than an incident:
-- `deposit_return_clock` (a STATUTORY deadline, the sharpest of the set) runs
-- only when listed in HELIXIS_REMINDERS__ENABLED_IDS, and `rent_missing` is
-- deliberately unregistered (its `month_key` would roll to the next month on
-- the last evening of every month).
--
-- WHY NOT NULL WITH THIS DEFAULT
--
-- There is no decline-to-assert option here (gotcha 55): a date must be
-- produced, so "unknown" cannot mean "skip". `America/New_York` is chosen
-- because it is ALREADY the value `clock_prompt` uses, so this migration
-- changes nobody's behaviour on the day it applies — it makes the existing
-- answer explicit and per-company, and the second answer stops disagreeing with
-- the first. A company moves off it by setting the column.
--
-- NOT VALIDATED IN SQL, DELIBERATELY. A CHECK constraint cannot reference
-- `pg_timezone_names`, and a hand-typed allowlist of IANA names in a migration
-- is a list that goes stale the next time the tz database moves a city. The
-- API validates against the live zone set the way `api/routes/sms.py` already
-- does for the consent ledger's zone, and `common/company_clock.py` falls back
-- to the default rather than raising — a bad zone must cost the customisation,
-- never the sweep.

alter table public.companies
    add column if not exists timezone text not null default 'America/New_York';

comment on column public.companies.timezone is
    'IANA zone for this company''s "today". Reminder date math and the agent''s '
    'current-date prompt both read it; the server clock is UTC and is not any '
    'customer''s calendar. Validated against the live IANA set in the API, not '
    'by a CHECK constraint — see 0098''s header.';
