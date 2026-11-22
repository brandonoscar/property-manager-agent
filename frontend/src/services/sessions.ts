/**
 * Active sessions — "which devices am I signed in on, and how do I kick one
 * off?"
 *
 * Backed by `GET/DELETE /api/v1/auth/sessions`, which reads GoTrue's own
 * `auth.sessions` table. Deleting a row there genuinely ends that session
 * rather than hiding it from a list, because it is the same table GoTrue
 * consults when deciding whether a refresh token is still good.
 *
 * ⚠ That was only true of REFRESH until 2026-09-09. The access token already
 * loaded in the revoked browser is a stateless JWT and went on working until
 * it expired on its own — Supabase's default hour — so "sign my lost laptop
 * out" left the laptop signed in. The backend now checks the row on every
 * request (`api/session_liveness.py`), which is what makes the paragraph
 * above true. Revoking is effective on the revoked device's NEXT request, not
 * on an SSE stream it already has open.
 */

import { apiFetch } from './api';

export interface ActiveSession {
  id: string;
  /** Humanised, e.g. "Chrome on macOS" — or "Unknown device". Never blank. */
  device: string;
  /** ISO. Truthfully the last time GoTrue touched the row. */
  lastSeen: string;
  signedInAt: string;
  /**
   * `null` means the server could not tell which row is this browser (the
   * token carried no `session_id` claim). Render that as "we can't highlight
   * this device" — NEVER as `false`, which reads as "some other device" and
   * invites revoking the session you are sitting in.
   */
  isCurrent: boolean | null;
}

interface SessionWire {
  id: string;
  device: string;
  last_seen: string;
  signed_in_at: string;
  is_current: boolean | null;
}

export interface ActiveSessions {
  sessions: ActiveSession[];
  /** False when the backend could not identify the calling device. */
  currentIdentified: boolean;
}

export async function fetchSessions(): Promise<ActiveSessions> {
  const res = await apiFetch('/api/v1/auth/sessions');
  if (!res.ok) throw new Error('Could not load your active sessions.');
  const wire: { sessions: SessionWire[]; current_identified?: boolean } =
    await res.json();
  return {
    // ⚠ Absent degrades to FALSE — "we cannot highlight this device" — not to
    // true. An older backend that does not send the flag must not have its
    // silence read as "yes, the highlighting is trustworthy", because the
    // consequence is somebody revoking the session they are sitting in.
    currentIdentified: wire.current_identified ?? false,
    sessions: (wire.sessions ?? []).map((s) => ({
      id: s.id,
      device: s.device,
      lastSeen: s.last_seen,
      signedInAt: s.signed_in_at,
      isCurrent: s.is_current ?? null,
    })),
  };
}

export async function revokeSession(id: string): Promise<void> {
  const res = await apiFetch(`/api/v1/auth/sessions/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  });
  // 404 is "already gone or not yours" — for a revoke that is the state the
  // caller wanted, so it is not surfaced as a failure they can act on.
  if (!res.ok && res.status !== 404) {
    throw new Error('Could not end that session.');
  }
}
