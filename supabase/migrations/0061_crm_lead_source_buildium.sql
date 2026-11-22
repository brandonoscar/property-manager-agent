-- 0061_crm_lead_source_buildium.sql
--
-- Add 'buildium' to the crm_leads.source CHECK so a Leasing lead can be
-- auto-created from a Buildium applicant webhook (Applicant.Created/Updated)
-- when no existing lead matches. Before this, applicants who applied directly
-- in Buildium — with no prior Helixis lead — never appeared in the pipeline,
-- because the applicant sync was move-only (it advanced an existing lead's
-- stage but never inserted one). The new create path tags those leads
-- source='buildium'.
--
-- The source CHECK is the named constraint from 0028, last rebuilt in 0031 to
-- add 'manual'. Same rebuild pattern here: drop + re-add with the extra value.
--
-- Migrations have NO runner (gotcha 8) — apply manually to helixis-test in
-- order, then verify the constraint accepts 'buildium'.

begin;

alter table public.crm_leads
    drop constraint if exists crm_leads_source_check;

alter table public.crm_leads
    add constraint crm_leads_source_check
        check (source in (
            'zillow', 'apartments', 'referral', 'web_form',
            'email', 'sms', 'manual', 'buildium'
        ));

commit;
