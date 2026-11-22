-- Property geocoding — foundation for the Maps / places / geolocation features.
--
-- Split of concerns (see the geo module docstrings):
--   Lane A — "which of MY properties are near X / closest / within R / cluster"
--            runs as PostGIS spatial SQL over this mirror. NO external API call.
--   Lane B — external places/directions (find a vendor near a property) hits the
--            Maps provider and is not stored here.
--
-- Addresses are geocoded ONCE on sync (deterministic + cached) and stored as a
-- point; the LLM never emits coordinates. A property with no geocode yet
-- (geog IS NULL) is simply excluded from spatial results.

create extension if not exists postgis;

alter table public.helixis_properties
    add column if not exists geog        geography(Point, 4326),
    add column if not exists geocoded_at timestamptz;

-- GIST index makes ST_DWithin (within-radius) and KNN (<->, nearest-first)
-- queries fast without scanning the table.
create index if not exists helixis_properties_geog_gix
    on public.helixis_properties using gist (geog);
