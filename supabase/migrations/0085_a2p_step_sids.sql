-- Resumability for the A2P ISV pipeline.
--
-- 0084 stores the FIVE major SIDs (customer profile, trust product, brand,
-- messaging service, campaign). The chain also creates intermediate objects
-- that carry SIDs we must not recreate: two EndUsers, an Address, and the
-- SupportingDocument that wraps it, plus the entity assignments binding them
-- to the profile.
--
-- Without somewhere to keep those, a run that dies after step 3 recreates
-- them on the retry — duplicate end-users and addresses under our account,
-- attached to a profile that already has its own. That is not as expensive as
-- a duplicate BRAND (which costs a fee and is rejected by the registry) but it
-- is the same class of defect, and the pipeline's whole contract is
-- "resumable, never restarted".
--
-- ⚠ A jsonb bag rather than four columns ON PURPOSE. These SIDs are never
-- queried, filtered or joined — they are read once, by the step that would
-- otherwise recreate the object. Columns would imply a query surface that
-- does not exist, and every future step would need another migration.
ALTER TABLE public.helixis_a2p_registrations
    ADD COLUMN IF NOT EXISTS step_sids jsonb NOT NULL DEFAULT '{}'::jsonb;

-- When the runner last touched this row. The sweep uses it to back off:
-- carrier vetting takes days, so re-polling a pending campaign every cycle
-- is pure API spend against a number that cannot have changed.
--
-- ⚠ Nullable, and NULL means "never attempted" rather than "attempted long
-- ago". A default of now() would make every existing row look freshly tried
-- and delay the first real attempt by a whole backoff window.
ALTER TABLE public.helixis_a2p_registrations
    ADD COLUMN IF NOT EXISTS last_attempt_at timestamptz;

COMMENT ON COLUMN public.helixis_a2p_registrations.step_sids IS
    'Intermediate Twilio SIDs (end users, address, supporting document, entity '
    'assignments) keyed by step name. Read only to SKIP a step that already ran '
    '— the pipeline is resumable and must never recreate a paid or duplicated '
    'object.';
COMMENT ON COLUMN public.helixis_a2p_registrations.last_attempt_at IS
    'Last runner/poller touch. NULL means never attempted. Drives the sweep''s '
    'backoff — campaign vetting takes 10-15 days, so polling every cycle spends '
    'API calls on a status that cannot have moved.';
