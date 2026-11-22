-- The Rentvine webhook signing key. ADR 0010 Decision (c), Phase 5.
--
-- Migration 0100 deliberately did NOT create this column, and said why in its
-- own header: a per-account signing key is third-party prose, the vendor's
-- OpenAPI spec contains ZERO occurrences of `webhook`, `signature` or `hmac`,
-- and a column holding a value nothing can validate is gotcha 79's shape — a
-- slot that reads as configured.
--
-- ⚠ WHAT CHANGED IS NOT THAT THE ALGORITHM BECAME KNOWN. It is still unknown.
-- What exists now is a seam that can EXECUTE it once it is derived
-- (`rentvine/signature.py`) and an instrument that derives it from one real
-- delivery (`scripts/rentvine_webhook_capture.py`). So the key has somewhere
-- to be consumed, and — the load-bearing half — a key with no derived scheme
-- still verifies NOTHING: `verify_signature` refuses before it looks at the
-- secret. Storing it cannot make the receiver fail open.
--
-- NULLABLE, and that is the ordinary state rather than an unfinished one. A
-- Rentvine customer who has not clicked through the UI's eight webhook rows
-- has no signing key, and their product works — polling is primary on Rentvine
-- (Decision (c)), so webhooks are an optimisation for four object types. A NOT
-- NULL column would make "connected" and "wired up webhooks" the same fact,
-- which they are not.
--
-- NO `last4`, for 0100's note-2 reason exactly: Rentvine shows a signing key
-- once and there is no surface on which four characters would help anybody
-- recognise it. Four characters of a secret with no use for them is exposure.

begin;

alter table public.company_rentvine_credentials
    add column if not exists encrypted_webhook_secret text;

comment on column public.company_rentvine_credentials.encrypted_webhook_secret is
    'Fernet-encrypted per-account webhook signing key, or NULL when the '
    'customer has not wired any webhooks. NULL is an ordinary state: polling '
    'is the primary freshness mechanism on Rentvine and webhooks optimise four '
    'object types. The signature ALGORITHM that consumes this is still '
    'unknown — see src/helixis/rentvine/signature.py, which refuses every '
    'delivery until scripts/rentvine_webhook_capture.py derives it.';

commit;
