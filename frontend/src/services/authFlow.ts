/**
 * Detects whether the app was opened from a Supabase **invite** or
 * **password-recovery** email link.
 *
 * Supabase-js (`detectSessionInUrl: true`) parses the URL hash on load,
 * establishes the session, and then STRIPS the hash — asynchronously,
 * shortly after page load. So by the time a React effect runs the hash is
 * usually gone. We snapshot `window.location.hash` once at module import
 * (earlier than the strip) and read the flow type from that snapshot.
 *
 * The hash looks like:
 *   #access_token=…&refresh_token=…&expires_at=…&type=invite
 * (or `type=recovery`). Both flows land the user with a live session but
 * no chosen password, so the app must show a "set your password" screen
 * instead of dropping them straight into the product.
 */

export type PasswordFlow = 'invite' | 'recovery';

/** Pure parser — exported so it can be unit-tested without a real URL. */
export function parsePasswordFlow(hash: string): PasswordFlow | null {
  const h = hash || '';
  if (/(?:^|[#&?])type=invite(?:&|$)/.test(h)) return 'invite';
  if (/(?:^|[#&?])type=recovery(?:&|$)/.test(h)) return 'recovery';
  return null;
}

// Snapshot at import time — before Supabase consumes + clears the hash.
const initialHash = typeof window !== 'undefined' ? window.location.hash : '';

/** The pending invite/recovery flow for this page load, if any. */
export function pendingPasswordFlow(): PasswordFlow | null {
  return parsePasswordFlow(initialHash);
}
