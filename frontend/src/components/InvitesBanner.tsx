/**
 * "Acme invited you to join" — and the two answers to it.
 *
 * ⚠ **Why this strip exists.** Until migration 0112, an admin who typed the email of
 * someone who already had an Occupella account added them to their company on the
 * spot, and because the active company is the sign-up company LAST, the inviter's
 * company became theirs: their chat, their books. Any account holder could do it to
 * anyone. Now such an invite waits here until the person answers it, and nothing of
 * theirs changes until they press Accept.
 *
 * ⚠ **Accept reloads the page.** The active company is `companyIds[0]`, read once at
 * sign-in by the store, the books session (`services/books.ts`) and every cached
 * screen. A reload is the one way to move all of them to the company just joined;
 * the books open for it on the way back in (`ensureBooks` makes a new exchange when
 * the live session is another company's).
 *
 * A failed read shows nothing, like `TrialBanner`: whether you have an invite is not
 * load-bearing for the app shell, and an outage already has `ServiceBanner`.
 */

import { useEffect, useState } from 'react';
import { toast } from 'sonner';

import {
  acceptInvite,
  declineInvite,
  getMyInvites,
  ROLE_LABEL,
  type MyInvite,
} from '../services/company';
import { demoMode } from '../services/auth';
import { useStore } from '../stores/store';

/** The sentence for one invite. Pure, so the words are tested without a render. */
export function inviteSentence(invite: MyInvite): string {
  const who = invite.invited_by_email ?? 'An admin';
  const company = invite.company_name || 'a workspace';
  return `${who} invited you to join ${company} as ${ROLE_LABEL[invite.role] ?? invite.role}.`;
}

/** Reload after accepting; a module seam so a test can see it without leaving. */
export const pageReload = { now: () => window.location.reload() };

export function InvitesBanner() {
  const userId = useStore((s) => s.user?.id ?? null);
  const [invites, setInvites] = useState<MyInvite[]>([]);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    if (!userId || demoMode) return;
    let cancelled = false;
    getMyInvites()
      .then((list) => {
        if (!cancelled) setInvites(list);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [userId]);

  if (invites.length === 0) return null;

  const accept = async (invite: MyInvite) => {
    setBusy(invite.id);
    try {
      const joined = await acceptInvite(invite.id);
      toast.success(`You joined ${joined.company_name || invite.company_name}.`);
      pageReload.now();
    } catch (err) {
      // The server's own words: "You already belong to …" names the way out.
      toast.error(err instanceof Error ? err.message : 'Could not accept the invite.', {
        duration: 12000,
      });
      setBusy(null);
    }
  };

  const decline = async (invite: MyInvite) => {
    setBusy(invite.id);
    try {
      await declineInvite(invite.id);
      setInvites((list) => list.filter((i) => i.id !== invite.id));
      toast.success(`Declined the invite to ${invite.company_name || 'that workspace'}.`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not decline the invite.');
    } finally {
      setBusy(null);
    }
  };

  return (
    <div
      role="status"
      data-testid="invites-banner"
      className="flex flex-col items-center gap-1.5 border-b px-4 py-2 text-[13px]"
      style={{ borderColor: 'var(--line)', background: 'var(--iris-soft)', color: 'var(--ink)' }}
    >
      {invites.map((invite) => (
        <div
          key={invite.id}
          className="flex flex-wrap items-center justify-center gap-x-3 gap-y-1.5 text-center"
        >
          <span>
            {inviteSentence(invite)} Accepting moves you to{' '}
            {invite.company_name || 'that workspace'}.
          </span>
          <span className="flex gap-2">
            <button
              type="button"
              onClick={() => void accept(invite)}
              disabled={busy !== null}
              className="h-7 px-2.5 shrink-0 rounded-[var(--r-sm)] text-[13px] font-medium disabled:opacity-60"
              style={{ background: 'var(--iris)', color: 'var(--canvas)' }}
            >
              {busy === invite.id ? 'Joining…' : 'Accept'}
            </button>
            <button
              type="button"
              onClick={() => void decline(invite)}
              disabled={busy !== null}
              className="h-7 px-2.5 shrink-0 rounded-[var(--r-sm)] border text-[13px] disabled:opacity-60"
              style={{
                borderColor: 'var(--line-strong)',
                background: 'var(--canvas)',
                color: 'var(--ink)',
              }}
            >
              Decline
            </button>
          </span>
        </div>
      ))}
    </div>
  );
}
