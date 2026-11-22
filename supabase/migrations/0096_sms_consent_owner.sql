-- 0096: whose phone is this — so a departing colleague stops getting alerts.
--
-- THE DEFECT
-- ----------
-- The manager digest (`reminders/sms_digest.py`) texts every live
-- `recipient_kind='manager'` consent row after each reminder sweep:
-- "3 items need attention; top: <the top card's title>". That is portfolio
-- content, and it kept going to people who had left.
--
-- `remove_company_member` deletes the `company_users` row, writes an activity
-- entry and re-syncs seats. It touches this ledger NOWHERE — and it could not,
-- because until this migration NOTHING on the row said whose number it was.
-- The table has `display_name` (free text) and `evidence->>'recorded_by'`
-- (whoever TYPED it, which for a tenant's number is an admin, not the tenant).
-- Neither is an owner.
--
-- WHY A COLUMN AND NOT A DERIVATION
-- ---------------------------------
-- The tempting shortcut is to treat `evidence->>'recorded_by'` as ownership.
-- It is not: it answers "who attested to this consent", which is exactly the
-- question the evidence column exists to answer for a TCPA defence, and
-- overloading it would break silently the next time capture changes (gotcha
-- 25/48's shape). It is also wrong for every number somebody records on
-- another person's behalf.
--
-- ⚠ **NEVER INFERRED.** This is set only when a signed-in person explicitly
-- claims the number as their own, and stays NULL otherwise. A guessed owner
-- would silence the wrong person's alerts on a colleague's departure —
-- gotcha 55's decline-to-assert, applied to a revocation. NULL means "we do
-- not know", and the product says so out loud rather than acting on a guess.
--
-- ⚠ **SET NULL, not CASCADE** — the same reasoning as 0095's memory_audit.
-- Deleting an auth user must not delete a consent row: this ledger is the
-- record that proves both the grant and the revocation, and a TCPA defence
-- needs it to survive the person. The row keeps the phone, the timestamps and
-- the evidence, and loses only the link to an account that no longer exists.
-- `tests/unit/test_fk_delete_rules.py` requires every new FK into auth.users
-- to state a rule, so this cannot be omitted by accident.

alter table public.helixis_sms_consent
    add column if not exists owner_user_id uuid
        references auth.users(id) on delete set null;

comment on column public.helixis_sms_consent.owner_user_id is
    'The Occupella account this phone belongs to, set ONLY when that person '
    'claimed it as their own. NULL means unattributed and is never guessed — '
    'staff-departure revocation acts on this column, so an inferred value '
    'would silence somebody else. See 0096.';

-- Partial: the only reader is "which manager rows belong to this departing
-- user", so the index covers exactly the rows that question can match.
create index if not exists helixis_sms_consent_owner_idx
    on public.helixis_sms_consent (owner_user_id)
    where owner_user_id is not null;

-- ⚠ NO BACKFILL, and that is the decision rather than an omission. Every
-- existing row predates the claim, so any value written here would be a guess
-- about a real person's phone — the exact thing the column forbids. They stay
-- NULL and the settings panel shows them as unattributed, which is true.
