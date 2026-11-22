-- 0049: Cross-tenant safety — mirror uniqueness becomes (company_id, buildium_id).
--
-- The mirror tables keyed uniqueness on buildium_id ALONE, and upserts did
-- ON CONFLICT (buildium_id) DO UPDATE SET company_id = EXCLUDED.company_id.
-- If Buildium entity IDs are per-account rather than globally unique, two
-- companies' syncs would collide and silently reassign one tenant's row to
-- the other company. Latent with a single company; existential at company
-- #2. Uniqueness is now scoped to the tenancy boundary. Upserts conflict on
-- (company_id, buildium_id) from the same change.

do $$
declare
    t text;
begin
    foreach t in array array[
        'helixis_properties', 'helixis_units', 'helixis_tenants',
        'helixis_leases', 'helixis_lease_transactions', 'helixis_work_orders',
        'helixis_applicants', 'helixis_vendors', 'helixis_owners',
        'helixis_tasks', 'helixis_gl_accounts', 'helixis_bills',
        'helixis_bank_accounts'
    ]
    loop
        execute format(
            'alter table public.%I drop constraint if exists %I',
            t, t || '_buildium_id_key');
        -- helixis_entities.sql already declares this key for leases and work
        -- orders, so a new database would otherwise fail here.
        execute format(
            'alter table public.%I drop constraint if exists %I',
            t, t || '_company_buildium_key');
        execute format(
            'alter table public.%I add constraint %I unique (company_id, buildium_id)',
            t, t || '_company_buildium_key');
    end loop;
end $$;
