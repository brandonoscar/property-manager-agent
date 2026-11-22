-- 0037_company_document_digest.sql
--
-- ADR 0006 follow-up — cache a text digest of each uploaded company document so
-- the daily knowledge generator can distill the *contents* (not just the names)
-- into the company profile.
--
-- Company knowledge is document-grounded: the facts a lead reply or chat answer
-- needs (office hours, pet policy, application fee, screening) live inside the
-- PDFs a PM uploads to Documents & Links, not in a free-text box. Extracting a
-- PDF is expensive (docling / Gemini multimodal), so we do it ONCE per document
-- and cache the result here; the generator reads the cached digests.
--
-- Both columns are nullable — a document with no digest yet simply contributes
-- its name (the pre-0037 behavior) until the next generation cycle fills it in.
--
-- NOTE: migrations have no runner (CLAUDE.md §4 / gotcha #8) — apply manually to
-- helixis-test; this file being committed is NOT proof it's live.

begin;

alter table public.company_documents
    add column if not exists text_digest        text,
    add column if not exists digest_generated_at timestamptz;

commit;
