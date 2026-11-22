-- A2P 10DLC registration, submitted BY US on the customer's behalf.
--
-- White-label means the customer never opens a carrier account, never sees a
-- vendor name, and never leaves Occupella. They fill in their business details
-- here; we run the ISV pipeline against our own messaging account:
--
--   secondary customer profile -> end-user + supporting-document objects
--   -> A2P trust-product bundle -> brand registration -> messaging service
--   -> campaign -> buy a number -> attach it
--
-- Each of those returns a SID we must keep, because the pipeline is resumable:
-- a campaign that fails vetting is re-submitted against the SAME brand, and a
-- brand that already exists must never be created twice (it costs money and
-- TCR rejects duplicates).
--
-- ⚠ TIMELINE IS THE PRODUCT CONSTRAINT, not a detail. Brand vetting returns in
-- minutes; CAMPAIGN vetting is currently 10-15 days, and per-number carrier
-- approval takes several more days after that. Nothing we build shortens it.
-- The value of submitting at signup is that the clock starts then instead of
-- when somebody remembers to do it.

CREATE TABLE IF NOT EXISTS public.helixis_a2p_registrations (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- One registration per company. A second brand for the same business is
    -- what TCR rejects as a duplicate, so the database refuses to represent it
    -- rather than leaving the application to remember.
    company_id              uuid NOT NULL UNIQUE
                            REFERENCES public.companies(id) ON DELETE CASCADE,

    -- ── what the customer typed ────────────────────────────────────
    legal_business_name     text NOT NULL,
    -- ⚠ Nullable ON PURPOSE. A business WITHOUT an EIN is a Sole Proprietor
    -- brand and one WITH an EIN must register Standard / Low-Volume Standard;
    -- TCR rejects the wrong kind. Absence is therefore meaningful data, not a
    -- missing field, and must never be defaulted to ''.
    ein                     text,
    business_type           text NOT NULL,
    business_vertical       text NOT NULL,
    website                 text,
    street                  text NOT NULL,
    city                    text NOT NULL,
    region                  text NOT NULL,
    postal_code             text NOT NULL,
    iso_country             text NOT NULL DEFAULT 'US',

    -- The authorized representative TCR emails for 2FA attestation. Getting
    -- this wrong stalls the brand silently, so it is required.
    rep_first_name          text NOT NULL,
    rep_last_name           text NOT NULL,
    rep_email               text NOT NULL,
    rep_phone               text NOT NULL,
    rep_job_position        text NOT NULL,

    -- ── campaign ───────────────────────────────────────────────────
    use_case                text NOT NULL DEFAULT 'MIXED',
    campaign_description    text NOT NULL,
    -- Carriers reject campaigns whose samples do not match the described use
    -- case, so these are the customer's real messages, not boilerplate.
    sample_messages         jsonb NOT NULL DEFAULT '[]'::jsonb,
    opt_in_description      text NOT NULL,

    -- ── what the carrier side gave back ────────────────────────────
    customer_profile_sid    text,
    trust_product_sid       text,
    brand_sid               text,
    messaging_service_sid   text,
    campaign_sid            text,

    -- ── where it has got to ────────────────────────────────────────
    -- draft -> submitted -> brand_pending -> brand_approved
    --       -> campaign_pending -> approved | failed
    status                  text NOT NULL DEFAULT 'draft',
    -- The carrier's own words. Kept verbatim: a paraphrased rejection reason
    -- is unactionable, and "address could not be validated" is the single
    -- most common one.
    failure_reason          text,
    submitted_at            timestamptz,
    brand_approved_at       timestamptz,
    campaign_approved_at    timestamptz,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now()
);

-- The status poller walks everything not yet finished.
CREATE INDEX IF NOT EXISTS helixis_a2p_registrations_status_idx
    ON public.helixis_a2p_registrations (status)
    WHERE status NOT IN ('approved', 'failed');

-- Inbound status callbacks arrive keyed by the resource SID, not by company.
CREATE INDEX IF NOT EXISTS helixis_a2p_registrations_brand_sid_idx
    ON public.helixis_a2p_registrations (brand_sid) WHERE brand_sid IS NOT NULL;
CREATE INDEX IF NOT EXISTS helixis_a2p_registrations_campaign_sid_idx
    ON public.helixis_a2p_registrations (campaign_sid) WHERE campaign_sid IS NOT NULL;

ALTER TABLE public.helixis_a2p_registrations ENABLE ROW LEVEL SECURITY;

-- Company-scoped read, matching every other helixis_* table (gotcha 47). The
-- service role writes; a member may only ever see their own company's row.
DROP POLICY IF EXISTS a2p_registrations_company_read ON public.helixis_a2p_registrations;
CREATE POLICY a2p_registrations_company_read
    ON public.helixis_a2p_registrations FOR SELECT
    USING (
        company_id IN (
            SELECT company_id FROM public.company_users WHERE user_id = auth.uid()
        )
    );

COMMENT ON TABLE public.helixis_a2p_registrations IS
    'A2P 10DLC brand + campaign registration submitted on a customer''s behalf. '
    'The customer never touches the carrier: they fill this in, we run the ISV '
    'pipeline. Campaign vetting takes 10-15 days and is not shortenable.';
