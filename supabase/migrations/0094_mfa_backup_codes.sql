-- Backup codes for two-factor authentication.
--
-- ⚠ WHAT THESE ACTUALLY DO, because the obvious reading is wrong. Supabase
-- owns the MFA protocol end to end, and there is no way to hand it a backup
-- code in place of a TOTP code — `mfa.verify` accepts a challenge against an
-- enrolled factor and nothing else. So a code here is NOT a second way to
-- sign in. It is a way to REMOVE the factor: spend a code, 2FA switches off,
-- and the password alone gets you back in.
--
-- That is the honest design rather than a compromise. The threat model is
-- unchanged — an attacker still needs the password AND a code — and it is
-- what "backup codes" means to a person in practice: the way back when the
-- phone is gone.
--
-- ⚠ Keyed on auth.users, NOT on a company. A person's second factor belongs
-- to them and follows them across every workspace they are a member of, so
-- there is deliberately no `company_id` here and the tenant-scoping corpus
-- guard should not expect one. Scoping this by company would mean somebody
-- with two workspaces had two sets of codes for one authenticator.
--
-- ⚠ SHA-256 with a per-row salt, and no bcrypt/scrypt. That is a considered
-- choice, not a shortcut: these codes are SERVER-GENERATED at 100 bits of
-- entropy, so unlike a human password there is no dictionary and no
-- meaningful offline search. A slow hash defends low-entropy secrets; it buys
-- nothing here and costs a dependency. If the generator is ever changed to
-- produce shorter or human-chosen codes, this reasoning expires with it —
-- `mfa_backup_codes.py` states the entropy it relies on.

create table if not exists public.helixis_mfa_backup_codes (
    id           uuid primary key default gen_random_uuid(),
    user_id      uuid not null references auth.users (id) on delete cascade,
    -- hex sha256 of (salt || code). The code itself is shown ONCE, at
    -- generation, and is unrecoverable afterwards by construction.
    code_hash    text not null,
    salt         text not null,
    created_at   timestamptz not null default now(),
    -- Stamped on redemption. SINGLE USE is the whole security property: a
    -- code that still works after being spent is a permanent 2FA bypass
    -- sitting in whatever the customer wrote it down on.
    used_at      timestamptz
);

comment on table public.helixis_mfa_backup_codes is
    'Single-use recovery codes that disable a user''s TOTP factor. Keyed on '
    'auth.users, not on a company — a second factor belongs to the person.';
comment on column public.helixis_mfa_backup_codes.used_at is
    'Non-null once spent. Rows are KEPT rather than deleted so "you have 7 of '
    '10 codes left" is answerable and a spent code can never be re-issued.';

create index if not exists helixis_mfa_backup_codes_user_idx
    on public.helixis_mfa_backup_codes (user_id);

-- Unspent codes only — this is the index the redemption path uses, and
-- keeping it partial means the lookup does not widen as spent codes pile up.
create index if not exists helixis_mfa_backup_codes_unused_idx
    on public.helixis_mfa_backup_codes (user_id)
    where used_at is null;

-- ⚠ RLS ON WITH NO POLICY, deliberately — the fail-CLOSED direction. Every
-- read and write goes through FastAPI on the service key, which bypasses RLS;
-- adding a permissive policy would only create a way for a client to reach
-- these rows directly, and there is no reason a browser should ever see a
-- code hash. Six other tables in this schema are in the same state and the
-- Supabase advisor reports it as the safe posture (gotcha 80).
alter table public.helixis_mfa_backup_codes enable row level security;
