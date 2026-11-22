/**
 * Getting-started checklist — the first-run walkthrough (BR list #6).
 *
 * A checklist card, not a guided overlay: it derives every tick from
 * REAL state the app already exposes (connectors, the mirror, chat
 * history, the team roster), so it checks itself off as the PM actually
 * does the things. No new backend surface, no persisted state — the
 * only local state is the dismissal flag in localStorage.
 *
 * Failure posture: each probe is independent (allSettled); a probe that
 * fails (403 for a member on the roster call, mirror empty) just reads
 * as "not done yet". The card never blocks or errors the page.
 */

import { listChatSessions } from './chat';
import { listConnectors, pmsLastSyncAt } from './connectors';
import { listMembers } from './company';
import { demoMode } from './auth';
import { fetchFirstScanSynced } from './reports';
import { fetchRentvineCredentialStatus } from './rentvine';
import { PMS, pmsInfo, type PmsId } from '../lib/pms';

export type ChecklistTarget = 'settings' | 'chat' | 'inbox';

export interface ChecklistStep {
  key: string;
  label: string;
  detail: string;
  done: boolean;
  target: ChecklistTarget;
}

export interface ChecklistState {
  steps: ChecklistStep[];
  doneCount: number;
  allDone: boolean;
}

export interface ChecklistInputs {
  /**
   * The systems the connect step names (`lib/pms.ts`): the one this workspace
   * is on, or, before it is on any, every one Settings → Connectors offers.
   * Empty when neither could be read.
   */
  pmsOptions: readonly PmsId[];
  pmsConnected: boolean;
  portfolioSynced: boolean;
  firstQuestionAsked: boolean;
  gmailConnected: boolean;
  teamInvited: boolean;
}

/** "Connect Rent Manager"; "Connect Buildium or Rentvine"; "Connect Buildium,
 *  Rentvine or Rent Manager". With nothing to name, the step still says what
 *  to do. */
function connectLabel(options: readonly PmsId[]): string {
  const names = options.map((id) => PMS[id].name);
  if (names.length === 0) return 'Connect your property management system';
  const last = names[names.length - 1];
  return names.length === 1 ? `Connect ${last}` : `Connect ${names.slice(0, -1).join(', ')} or ${last}`;
}

export function computeChecklist(inputs: ChecklistInputs): ChecklistState {
  const steps: ChecklistStep[] = [
    {
      // ⚠ Named for the system that is actually connected. It said "Connect
      // Buildium" to every company, so a Rentvine customer was told, forever,
      // to connect a system they do not use; then "Buildium or Rentvine" to a
      // Rent Manager or Propertyware one.
      key: 'pms',
      label: connectLabel(inputs.pmsOptions),
      // Not "API key": Rent Manager signs in with a username and password.
      detail: 'Save your connection details so Occupella can see your portfolio.',
      done: inputs.pmsConnected,
      target: 'settings',
    },
    {
      key: 'sync',
      label: 'Sync your portfolio',
      detail: 'Sync once to load your properties, leases and tenants.',
      done: inputs.portfolioSynced,
      target: 'settings',
    },
    {
      key: 'ask',
      label: 'Ask your first question',
      detail: 'Try "who’s behind on rent?" or "what’s open at my properties?"',
      done: inputs.firstQuestionAsked,
      target: 'chat',
    },
    {
      key: 'gmail',
      label: 'Connect Gmail',
      // ⚠ Said "Important tenant and owner emails land in your Inbox."
      // Automatic email ingest is gated (known Buildium contact, 120+ words)
      // and has produced one card across every company in the product's
      // lifetime — measured on live data 2026-09-02. Same claim was removed
      // from the onboarding wizard the same day; this is the second surface
      // that made it. Describe what you can ASK for, which always works.
      detail: 'Ask what you told an owner last month without leaving the chat.',
      done: inputs.gmailConnected,
      target: 'settings',
    },
    {
      key: 'team',
      label: 'Invite a teammate',
      detail: 'Teammates get their own sign-in and see the same Inbox.',
      done: inputs.teamInvited,
      target: 'settings',
    },
  ];
  const doneCount = steps.filter((s) => s.done).length;
  return { steps, doneCount, allDone: doneCount === steps.length };
}


export async function fetchChecklist(): Promise<ChecklistState> {
  if (demoMode) {
    // A believable mid-onboarding snapshot for demo/screenshots.
    return computeChecklist({
      pmsOptions: ['buildium'],
      pmsConnected: true,
      portfolioSynced: true,
      firstQuestionAsked: false,
      gmailConnected: false,
      teamInvited: false,
    });
  }
  const [connectors, sessions, members, rentvine, firstScan] = await Promise.allSettled([
    listConnectors(),
    listChatSessions(1),
    listMembers(),
    fetchRentvineCredentialStatus(),
    fetchFirstScanSynced(),
  ]);
  const rows = connectors.status === 'fulfilled' ? connectors.value : [];
  const connected = (id: string) => {
    const c = rows.find((r) => r.id === id);
    return Boolean(c?.apiConnected || c?.webhookConnected);
  };
  // Every PMS card the server listed, in its order (`pmsInfo` drops every
  // other connector). A workspace holding keys for a system is shown that
  // system's card alone; before it holds any, the list is every system this
  // deployment offers (`api/routes/connectors.py`).
  const pmsListed = rows
    .map((r) => pmsInfo(r.id)?.id)
    .filter((id): id is PmsId => id !== undefined);
  // A card counts once its keys passed a test or its data synced, within the
  // card's own proof window: the server's `isConnected`, for every system.
  const pmsCardConnected = pmsListed.find((id) => connected(id));
  // Rentvine also counts once its keys have passed a test call at all, the
  // same bar the wizard holds them to. Saved-but-untested keys are not a
  // connection.
  const rentvineTested =
    pmsCardConnected === 'rentvine' ||
    (rentvine.status === 'fulfilled' && rentvine.value.configured && rentvine.value.lastTestedAt !== null);
  // The mirror has portfolio rows: some system is connected and has synced,
  // whichever it is. This is also the member's path, since the Rentvine
  // credentials read is admin-only.
  const mirrorHasRows = firstScan.status === 'fulfilled' && firstScan.value;
  return computeChecklist({
    pmsOptions: pmsCardConnected
      ? [pmsCardConnected]
      : rentvineTested
        ? ['rentvine']
        : pmsListed,
    pmsConnected: pmsCardConnected !== undefined || rentvineTested || mirrorHasRows,
    // ⚠ "Did a sync RUN", not "did it find anything". This asked whether the
    // cockpit had rows, so a company whose Buildium account genuinely holds no
    // rental properties could NEVER tick "Sync your portfolio" — the
    // getting-started card stayed on their screen forever, one step short of
    // done, telling them to do a thing they had already done. `lastSyncAt` on
    // the Buildium card is the real per-table stamp from
    // `helixis_mirror_sync_state` and is now absent until a sync has actually
    // happened (it used to be a page-load placeholder for every connected
    // card, which would have answered "synced" for everyone).
    // The Rentvine card carries the same real stamp now; mirror rows still
    // count for a member, who cannot read either credentials route.
    portfolioSynced: pmsLastSyncAt(rows) !== undefined || mirrorHasRows,
    firstQuestionAsked: sessions.status === 'fulfilled' && sessions.value.rows.length > 0,
    gmailConnected: connected('gmail'),
    teamInvited: members.status === 'fulfilled' && members.value.length > 1,
  });
}

const DISMISS_KEY = 'helixis.gettingStarted.dismissed';

export function isChecklistDismissed(): boolean {
  try {
    return localStorage.getItem(DISMISS_KEY) === 'true';
  } catch {
    return false;
  }
}

export function dismissChecklist(): void {
  try {
    localStorage.setItem(DISMISS_KEY, 'true');
  } catch {
    /* private mode — the card just reappears next session */
  }
}
