/**
 * Two-factor authentication (TOTP).
 *
 * A password alone protects somebody else's entire portfolio — every lease,
 * every resident's contact details, every dollar figure. Occupella asks
 * property managers to put all of that in one place, so the second factor is
 * not a nicety here; it is the control that makes the ask reasonable.
 *
 * Supabase does the whole TOTP protocol — enrol, challenge, verify, unenrol —
 * so this is a thin wrapper whose real job is to make the FAILURE MODES
 * legible. Every function below returns or throws something a UI can say out
 * loud, because the worst version of this feature is one that locks a manager
 * out of their own books on a Monday morning with a stack trace.
 *
 * ⚠ **The secret IS the backup, and the UI has to say so.** Supabase issues
 * no recovery codes (measured: `auth.mfa` exposes enroll / challenge /
 * verify / unenroll / listFactors / challengeAndVerify /
 * getAuthenticatorAssuranceLevel / webauthn — and nothing else). So the one
 * thing standing between a lost phone and a locked account is whether the
 * person saved the setup key. A screen that shows a QR code and hurries past
 * the key is the version of this feature that generates support tickets.
 *
 * ⚠ **No QR dependency.** `enroll()` returns `totp.qr_code` already rendered
 * as an SVG data URI, so adding a QR library days before launch would be
 * spending a dependency on something the API hands over for free.
 */

import { apiFetch } from './api';
import { supabase } from './auth';

export interface EnrolledFactor {
  id: string;
  friendlyName: string | null;
  /** Supabase's own status — `verified` means it is actually protecting the account. */
  status: string;
}

export interface EnrolmentStart {
  factorId: string;
  /** SVG data URI, ready for an <img src>. */
  qrCode: string;
  /** The base32 setup key. THE recovery path — see the module note. */
  secret: string;
}

/**
 * Factors already on the account.
 *
 * ⚠ Returns only VERIFIED ones. An abandoned enrolment leaves an `unverified`
 * factor behind, and counting those would tell somebody they are protected
 * when they are not — the direction that matters.
 */
export async function listVerifiedFactors(): Promise<EnrolledFactor[]> {
  const { data, error } = await supabase.auth.mfa.listFactors();
  if (error) throw error;
  const all = data?.all ?? [];
  return all
    .filter((f) => f.status === 'verified')
    .map((f) => ({
      id: f.id,
      friendlyName: f.friendly_name ?? null,
      status: f.status,
    }));
}

/**
 * Begin enrolment. Nothing is protecting the account until `confirmEnrolment`
 * succeeds — this only mints a pending factor.
 *
 * ⚠ Cleans up abandoned attempts first. Supabase rejects a second enrolment
 * with the same friendly name, so somebody who opens the panel, closes it,
 * and comes back gets "factor already exists" and can never enrol again
 * without support. Sweeping unverified factors makes the panel re-openable,
 * and an unverified factor protects nothing, so nothing is lost by removing
 * one.
 */
export async function startEnrolment(friendlyName = 'Authenticator app'): Promise<EnrolmentStart> {
  const { data: existing } = await supabase.auth.mfa.listFactors();
  for (const factor of existing?.all ?? []) {
    if (factor.status !== 'verified') {
      await supabase.auth.mfa.unenroll({ factorId: factor.id }).catch(() => undefined);
    }
  }

  const { data, error } = await supabase.auth.mfa.enroll({
    factorType: 'totp',
    friendlyName,
  });
  if (error) throw error;
  const totp = (data as { totp?: { qr_code?: string; secret?: string } })?.totp;
  if (!data?.id || !totp?.qr_code || !totp?.secret) {
    // A partial response means we cannot show the key, and the key is the
    // only recovery path. Refusing beats enrolling somebody into a factor
    // they have no way back from.
    throw new Error('Could not start setup. Try again in a moment.');
  }
  return { factorId: data.id, qrCode: totp.qr_code, secret: totp.secret };
}

/** Wrong code — separate from a real failure, because the fix is different. */
export class WrongCode extends Error {
  constructor() {
    super('That code was not accepted. Check your app and try the current code.');
    this.name = 'WrongCode';
  }
}

/**
 * Finish enrolment by proving the app is set up.
 *
 * ⚠ A wrong code is by far the likeliest outcome here — codes rotate every 30
 * seconds and people type the one that just expired — so it is its own error
 * type. "Something went wrong" for a mistyped digit sends somebody to support
 * for a thing they can fix in ten seconds.
 */
export async function confirmEnrolment(factorId: string, code: string): Promise<void> {
  const { data: challenge, error: challengeError } = await supabase.auth.mfa.challenge({
    factorId,
  });
  if (challengeError) throw challengeError;

  const { error } = await supabase.auth.mfa.verify({
    factorId,
    challengeId: challenge.id,
    code: code.replace(/\s/g, ''),
  });
  if (error) throw new WrongCode();
}

/**
 * Turn 2FA off.
 *
 * ⚠ Requires the CURRENT PASSWORD, and that is the whole point. Supabase's
 * `unenroll` needs only a live session, so a form that called it directly
 * would let anyone at an unlocked laptop strip the second factor off the
 * account in one click — which is precisely the attack 2FA exists to stop.
 * Same guard, and same reasoning, as `changePassword` and `changeEmail`.
 */
export async function disableTwoFactor(factorId: string, currentPassword: string): Promise<void> {
  const email = (await supabase.auth.getUser()).data.user?.email;
  if (!email) throw new Error('You are not signed in.');

  const { error: verifyError } = await supabase.auth.signInWithPassword({
    email,
    password: currentPassword,
  });
  if (verifyError) {
    const { CurrentPasswordWrong } = await import('./auth');
    throw new CurrentPasswordWrong();
  }

  const { error } = await supabase.auth.mfa.unenroll({ factorId });
  if (error) throw error;
}

// ── Backup codes ─────────────────────────────────────────────────────
//
// ⚠ THE MODULE NOTE ABOVE IS NOW OUT OF DATE IN ONE RESPECT, and this is the
// correction rather than a second opinion: it says the setup key is "the one
// thing standing between a lost phone and a locked account". Backup codes are
// now that thing as well, and they are the better one — a key somebody saved
// re-creates the SAME factor on a new phone, while a code removes it.
//
// Supabase still issues none of this: it will not accept a backup code in
// place of a TOTP code, so a code here does not sign you in. It takes the
// factor OFF, and the password alone works again. The threat model is
// unchanged (password AND a code) and it matches what people mean by the
// phrase. See `helixis/auth/mfa_backup_codes.py` and migration 0094.

export interface BackupCodeStatus {
  remaining: number;
  /** How many a fresh batch issues — read from the server so "3 of 10 left"
   *  cannot drift from what the generator actually does. */
  issuedPerBatch: number;
}

export async function fetchBackupCodeStatus(): Promise<BackupCodeStatus> {
  const res = await apiFetch('/api/v1/auth/mfa/backup-codes');
  if (!res.ok) throw new Error('Could not check your backup codes.');
  const wire: { remaining?: number; issued_per_batch?: number } = await res.json();
  return {
    remaining: wire.remaining ?? 0,
    issuedPerBatch: wire.issued_per_batch ?? 10,
  };
}

/**
 * Issue a fresh set. Returns the plaintext codes — THE ONLY TIME THEY EXIST.
 *
 * ⚠ Two things the caller must honour, because nothing downstream can undo
 * getting them wrong. There is no second fetch that returns these, so a UI
 * that shows them briefly and moves on has handed somebody a recovery path
 * they do not have. And regenerating KILLS the previous sheet including
 * unspent codes, so the button that calls this has to say so.
 */
export async function generateBackupCodes(): Promise<string[]> {
  const res = await apiFetch('/api/v1/auth/mfa/backup-codes', { method: 'POST' });
  if (!res.ok) throw new Error('Could not create backup codes.');
  const wire: { codes?: string[] } = await res.json();
  if (!wire.codes?.length) {
    // An empty list would render an empty box that looks like a finished
    // step. Refusing beats telling somebody they are covered when they are
    // holding nothing.
    throw new Error('Could not create backup codes.');
  }
  return wire.codes;
}

/** A code that the server would not accept. Separate because the fix is the
 *  user's (check the sheet), not ours. */
export class BadBackupCode extends Error {
  constructor() {
    super("That backup code wasn't accepted. Check the sheet and try again.");
    this.name = 'BadBackupCode';
  }
}

/**
 * Spend a code and turn 2FA off.
 *
 * ⚠ A 400 is a WRONG CODE and anything else is a real failure, and the
 * difference matters more here than it looks: somebody at this screen has
 * lost their phone and is holding a finite sheet of paper. Reporting a server
 * outage as "wrong code" makes them burn a second code, then a third.
 */
export async function redeemBackupCode(code: string): Promise<void> {
  const res = await apiFetch('/api/v1/auth/mfa/backup-codes/redeem', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code }),
  });
  if (res.status === 400) throw new BadBackupCode();
  if (!res.ok) throw new Error('Could not use that backup code. Try again in a moment.');
}
