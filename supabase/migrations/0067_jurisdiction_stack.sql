-- 0067_jurisdiction_stack.sql
--
-- Jurisdiction intelligence — WHICH landlord-tenant law governs each property.
--
-- Two pieces:
--
--   (a) The resolved jurisdiction stack, persisted on ``helixis_properties``:
--       state / county / incorporated-place FIPS codes, resolved ONCE per
--       geocoded property from the Census geographies endpoint (the same
--       keyless service family as the geocoder) by the intel backfill
--       (geo/intel_backfill.py). The LEGAL place comes from Census
--       incorporated-place boundaries, NEVER the postal city — a mailing
--       address of "Atlanta, GA" routinely sits outside Atlanta's city
--       limits, and it's the boundary that decides which ordinances apply.
--
--   (b) ``jurisdiction_notes`` — a service-role override/correction table.
--       This is NOT where the rules live: the rules snapshot is the bundled,
--       versioned JSON (src/helixis/geo/data/jurisdiction_rules.json — the
--       source of truth, shipped and reviewed with the code). A row HERE,
--       when present, WINS over the JSON for its (jurisdiction, param) — the
--       precedence is enforced in the lookup function
--       (geo/jurisdiction.py::lookup, overrides layer) — so a statute change
--       can be corrected in production without waiting for a code deploy.
--
-- APPLIED to helixis-test 2026-08-17 via the Supabase MCP; verified by
-- effect (4 FIPS columns present; jurisdiction_notes rowsecurity=true with
-- zero policies).

-- ── (a) Resolved jurisdiction stack on the property mirror ────────────

alter table public.helixis_properties
    add column if not exists state_fips  text,
    add column if not exists county_fips text,
    add column if not exists place_fips  text,
    add column if not exists jurisdiction_resolved_at timestamptz;

comment on column public.helixis_properties.place_fips is
    'Census incorporated-place GEOID. NULL after resolution '
    '(jurisdiction_resolved_at set) means the property is in unincorporated '
    'territory — that is an ANSWER (county/state law governs), not an error. '
    'The legal place comes from Census incorporated-place boundaries, never '
    'the postal city on the mailing address.';

comment on column public.helixis_properties.jurisdiction_resolved_at is
    'When the FIPS stack was resolved. NULL = not yet resolved (the intel '
    'backfill retries it on the next pass).';

-- ── (b) Rule overrides / corrections (service-role only) ──────────────

create table if not exists public.jurisdiction_notes (
    jurisdiction_key text        not null,  -- 'US' | 'state:GA' | 'county:13121' | 'place:1304000'
    param            text        not null,  -- dotted path into the state block, e.g. 'deposit.cap_months'
    value            jsonb       not null,  -- replaces the bundled-JSON value (number/string/null all valid)
    citation         text,
    last_verified    date,
    updated_at       timestamptz not null default now(),
    primary key (jurisdiction_key, param)
);

alter table public.jurisdiction_notes enable row level security;
-- No permissive policy: this is shared reference data (statute parameters),
-- not tenant data — only the service-role pool reads/writes it, and service
-- role bypasses RLS. Enabling RLS with no policy denies all anon/auth access
-- by default (same treatment as helixis_gmail_seen, migration 0024).
