-- 0099_jurisdiction_names.sql
--
-- The jurisdiction snapshot names only the STATE, so a wrong county or place
-- is invisible to the one person who could catch it.
--
-- `jurisdiction_lookup` drives DEPOSIT DEADLINES, NOTICE PERIODS, late-fee caps
-- and screening rules. Those come from the state block plus any county/place
-- overlay, and the overlay is selected by `county_fips` / `place_fips`. But
-- `format_compact` renders `"<property> — Colorado landlord-tenant snapshot:"`
-- and nothing else about WHERE, so a property geocoded a block across a city
-- line produces a confidently wrong legal answer that reads exactly like a
-- right one. Coverage item 196.
--
-- ⚠ THE NAMES WERE ALREADY BEING COMPUTED AND THROWN AWAY.
-- `geo/jurisdiction.py::resolve_stack` returns `county_name` and `place_name`
-- on the same Census response it takes the FIPS codes from;
-- `_JURISDICTION_UPDATE_SQL` persisted three columns and dropped both. That is
-- the seventh instance in this repo of the field-fetched-and-discarded shape
-- (gotchas 41 / 50 / 51 / 88 / 92 / 93), and as in every previous one the fix
-- is to keep what was already in hand rather than to go and get it again.
--
-- ⚠ NO BACKFILL IS POSSIBLE HERE, AND THAT IS THE POINT OF SAYING SO.
-- A FIPS code cannot be turned into a name without the Census gazetteer, which
-- this product does not bundle. Pre-state MEASURED first (gotcha 59): 66
-- properties, 56 geocoded, 56 with a county FIPS, 55 with a place FIPS, 1
-- genuinely unincorporated. Every one of those 56 keeps NULL names until the
-- worker re-resolves it — `intel_backfill`'s work-list is widened in the same
-- change so they drain, never-resolved rows first. Until then `format_compact`
-- renders exactly what it renders today (the state alone), because a header
-- reading "None County" would be a fresh false statement rather than a missing
-- one.
--
-- Nullable and no default, deliberately: NULL means "not resolved yet", and a
-- default would make every existing row claim a name nobody looked up.
--
-- migration_check.py CAN see this one — it adds columns.

alter table public.helixis_properties
    add column if not exists county_name text,
    add column if not exists place_name  text;

comment on column public.helixis_properties.county_name is
    'Census county NAME for the resolved point (e.g. "Denver County"). Written '
    'by geo/intel_backfill from the same resolve_stack response the FIPS codes '
    'come from. NULL = not resolved yet; it is never derived from the FIPS code, '
    'because this product bundles no gazetteer to derive it from.';

comment on column public.helixis_properties.place_name is
    'Census incorporated-place NAME for the resolved point (e.g. "Aurora"). '
    'NULL is ambiguous BY NATURE here and the reader must not collapse it: it '
    'means either "not resolved yet" or "unincorporated", and place_fips tells '
    'them apart — a NULL fips with a resolved state is genuinely unincorporated '
    '(1 of 56 rows today), while a non-NULL fips with a NULL name is simply a '
    'row written before this column existed.';
