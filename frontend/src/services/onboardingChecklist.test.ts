/**
 * Getting-started checklist — pure derivation, fail-open probes, and the
 * dismissal flag. The card must tick itself from real state and treat
 * any failed probe as "not done yet", never an error.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./auth', () => ({ demoMode: false, getAccessToken: vi.fn().mockResolvedValue('tkn') }));
vi.mock('./chat', () => ({ listChatSessions: vi.fn() }));
// ⚠ NETWORK ONLY (gotcha 88). `pmsLastSyncAt` is the pure helper this
// file now depends on; stubbing it alongside `listConnectors` would make every
// assertion below agree with whatever the stub said.
vi.mock('./connectors', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./connectors')>()),
  listConnectors: vi.fn(),
}));
vi.mock('./company', () => ({ listMembers: vi.fn() }));
vi.mock('./rentvine', () => ({ fetchRentvineCredentialStatus: vi.fn() }));
vi.mock('./reports', () => ({ fetchFirstScanSynced: vi.fn() }));

import { listChatSessions } from './chat';
import { listConnectors } from './connectors';
import { listMembers } from './company';
import { fetchFirstScanSynced } from './reports';
import { fetchRentvineCredentialStatus } from './rentvine';
import type { PmsId } from '../lib/pms';
import {
  computeChecklist,
  dismissChecklist,
  fetchChecklist,
  isChecklistDismissed,
} from './onboardingChecklist';

// Default for the two probes added for Rentvine: no Rentvine keys, an empty
// mirror. Each test that is ABOUT them overrides it.
beforeEach(() => {
  vi.mocked(fetchRentvineCredentialStatus).mockResolvedValue({ configured: false, lastTestedAt: null });
  vi.mocked(fetchFirstScanSynced).mockResolvedValue(false);
});

describe('computeChecklist', () => {
  it('orders the five steps and counts completions', () => {
    const state = computeChecklist({
      pmsOptions: ['buildium'],
      pmsConnected: true,
      portfolioSynced: true,
      firstQuestionAsked: false,
      gmailConnected: false,
      teamInvited: false,
    });
    expect(state.steps.map((s) => s.key)).toEqual(['pms', 'sync', 'ask', 'gmail', 'team']);
    expect(state.doneCount).toBe(2);
    expect(state.allDone).toBe(false);
  });

  it('reports allDone when every step is ticked', () => {
    const state = computeChecklist({
      pmsOptions: ['rentvine'],
      pmsConnected: true,
      portfolioSynced: true,
      firstQuestionAsked: true,
      gmailConnected: true,
      teamInvited: true,
    });
    expect(state.allDone).toBe(true);
  });
});

describe('fetchChecklist', () => {
  it('derives ticks from live state', async () => {
    vi.mocked(listConnectors).mockResolvedValue([
      { id: 'buildium', apiConnected: true, lastSyncAt: 1_700_000_000_000 },
      { id: 'gmail', apiConnected: false },
    ] as never);
    vi.mocked(listChatSessions).mockResolvedValue({ rows: [{ id: 's1' }] } as never);
    vi.mocked(listMembers).mockResolvedValue([{ user_id: 'me' }] as never);

    const state = await fetchChecklist();
    const byKey = Object.fromEntries(state.steps.map((s) => [s.key, s.done]));
    expect(byKey).toEqual({ pms: true, sync: true, ask: true, gmail: false, team: false });
  });

  // THE DEFECT. `portfolioSynced` asked whether the COCKPIT had rows, so a
  // company whose Buildium account genuinely holds no rental properties could
  // never tick "Sync your portfolio" — the card sat on their screen forever
  // telling them to run a sync they had already run. Ticking it is what a
  // completed sync means; finding nothing is a fact about their books.
  it('ticks the sync step for a synced account that owns no properties', async () => {
    vi.mocked(listConnectors).mockResolvedValue([
      { id: 'buildium', apiConnected: true, lastSyncAt: 1_700_000_000_000 },
    ] as never);
    vi.mocked(listChatSessions).mockResolvedValue({ rows: [] } as never);
    vi.mocked(listMembers).mockResolvedValue([{ user_id: 'me' }] as never);

    const state = await fetchChecklist();
    expect(state.steps.find((s) => s.key === 'sync')?.done).toBe(true);
  });

  // The other direction, so the fix cannot be satisfied by ticking always:
  // connected and never synced is the state "Run Sync data" is written for.
  it('leaves the sync step unticked when Buildium has never synced', async () => {
    vi.mocked(listConnectors).mockResolvedValue([
      { id: 'buildium', apiConnected: true },
    ] as never);
    vi.mocked(listChatSessions).mockResolvedValue({ rows: [] } as never);
    vi.mocked(listMembers).mockResolvedValue([{ user_id: 'me' }] as never);

    const state = await fetchChecklist();
    expect(state.steps.find((s) => s.key === 'pms')?.done).toBe(true);
    expect(state.steps.find((s) => s.key === 'sync')?.done).toBe(false);
  });

  // ⚠ THE DISTINGUISHING INPUT, and a mutation battery is what found it
  // missing: every other fixture here gives Buildium the only `lastSyncAt`, so
  // "find the first row with one" passes them all. The backend stamps every
  // OTHER connected card with a page-load placeholder, so a company that
  // connected Gmail and never synced Buildium would tick "Sync your portfolio"
  // off somebody else's fake timestamp.
  it('does not read another connector\'s placeholder as a Buildium sync', async () => {
    vi.mocked(listConnectors).mockResolvedValue([
      { id: 'gmail', apiConnected: true, lastSyncAt: 1_700_000_000_000 },
      { id: 'buildium', apiConnected: true },
    ] as never);
    vi.mocked(listChatSessions).mockResolvedValue({ rows: [] } as never);
    vi.mocked(listMembers).mockResolvedValue([{ user_id: 'me' }] as never);

    const state = await fetchChecklist();
    expect(state.steps.find((s) => s.key === 'sync')?.done).toBe(false);
  });

  it('treats a failed probe as not-done, never an error', async () => {
    vi.mocked(listConnectors).mockRejectedValue(new Error('503'));
    vi.mocked(listChatSessions).mockRejectedValue(new Error('401'));
    vi.mocked(listMembers).mockRejectedValue(new Error('403 member'));

    const state = await fetchChecklist();
    expect(state.doneCount).toBe(0);
    expect(state.allDone).toBe(false);
  });
});

// ⚠ THE RENTVINE DEFECT. The checklist only ever asked about Buildium, so a
// Rentvine company was told "Connect Buildium" and "Sync your portfolio"
// forever, whatever it had done.
describe('a Rentvine company', () => {
  beforeEach(() => {
    vi.mocked(listConnectors).mockResolvedValue([{ id: 'buildium', apiConnected: false }] as never);
    vi.mocked(listChatSessions).mockResolvedValue({ rows: [] } as never);
    vi.mocked(listMembers).mockResolvedValue([{ user_id: 'me' }] as never);
  });

  it('counts tested Rentvine keys as connected, and names Rentvine', async () => {
    vi.mocked(fetchRentvineCredentialStatus).mockResolvedValue({
      configured: true,
      lastTestedAt: '2026-09-30T00:00:00Z',
    });
    const step = (await fetchChecklist()).steps.find((s) => s.key === 'pms');
    expect(step?.done).toBe(true);
    expect(step?.label).toBe('Connect Rentvine');
  });

  // Re-pointed (rule 19): this listed the Buildium card beside saved Rentvine
  // keys, which the server never does (a workspace holding keys is shown its
  // own card alone), and pinned the fixed "Buildium or Rentvine" label.
  it('does not count saved keys that never passed a test, and names Rentvine', async () => {
    vi.mocked(listConnectors).mockResolvedValue([{ id: 'rentvine', apiConnected: false }] as never);
    vi.mocked(fetchRentvineCredentialStatus).mockResolvedValue({ configured: true, lastTestedAt: null });
    const step = (await fetchChecklist()).steps.find((s) => s.key === 'pms');
    expect(step?.done).toBe(false);
    expect(step?.label).toBe('Connect Rentvine');
  });

  it('ticks connect and sync from mirror rows, which is also how a member sees it', async () => {
    // A member's Rentvine credentials read is a 403.
    vi.mocked(fetchRentvineCredentialStatus).mockRejectedValue(new Error('403'));
    vi.mocked(fetchFirstScanSynced).mockResolvedValue(true);
    const byKey = Object.fromEntries((await fetchChecklist()).steps.map((s) => [s.key, s.done]));
    expect(byKey.pms).toBe(true);
    expect(byKey.sync).toBe(true);
  });
});

describe('dismissal flag', () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => localStorage.clear());

  it('round-trips through localStorage', () => {
    expect(isChecklistDismissed()).toBe(false);
    dismissChecklist();
    expect(isChecklistDismissed()).toBe(true);
  });
});

describe('the connect step names the system from lib/pms.ts', () => {
  const base = {
    pmsConnected: false,
    portfolioSynced: false,
    firstQuestionAsked: false,
    gmailConnected: false,
    teamInvited: false,
  };
  const label = (pmsOptions: PmsId[]) =>
    computeChecklist({ ...base, pmsOptions }).steps.find((s) => s.key === 'pms')?.label;

  it('keeps the Buildium and Rentvine wording', () => {
    expect(label(['buildium'])).toBe('Connect Buildium');
    expect(label(['rentvine'])).toBe('Connect Rentvine');
    expect(label(['buildium', 'rentvine'])).toBe('Connect Buildium or Rentvine');
  });

  it('names a third system by its customer-facing name', () => {
    expect(label(['rentmanager'])).toBe('Connect Rent Manager');
    expect(label(['propertyware'])).toBe('Connect Propertyware');
    expect(label(['buildium', 'rentvine', 'rentmanager'])).toBe(
      'Connect Buildium, Rentvine or Rent Manager',
    );
  });

  it('still says what to do with no system to name', () => {
    expect(label([])).toBe('Connect your property management system');
  });

  it('never tells a Rent Manager customer to find an API key', () => {
    const detail = computeChecklist({ ...base, pmsOptions: ['rentmanager'] }).steps.find(
      (s) => s.key === 'pms',
    )?.detail;
    expect(detail).toBe('Save your connection details so Occupella can see your portfolio.');
  });
});

// ⚠ THE RENT MANAGER / PROPERTYWARE DEFECT. The step asked about Buildium and
// Rentvine by name, so a Rent Manager or Propertyware company was told
// "Connect Buildium or Rentvine", and its connected card did not tick the step.
describe('a Rent Manager or Propertyware company', () => {
  beforeEach(() => {
    vi.mocked(listChatSessions).mockResolvedValue({ rows: [] } as never);
    vi.mocked(listMembers).mockResolvedValue([{ user_id: 'me' }] as never);
  });

  const pmsStep = async () => (await fetchChecklist()).steps.find((s) => s.key === 'pms');

  it('counts a connected Rent Manager card, and names Rent Manager', async () => {
    vi.mocked(listConnectors).mockResolvedValue([{ id: 'rentmanager', apiConnected: true }] as never);
    const step = await pmsStep();
    expect(step?.done).toBe(true);
    expect(step?.label).toBe('Connect Rent Manager');
  });

  it('names Rent Manager for saved details that are not proven yet, and leaves the step open', async () => {
    vi.mocked(listConnectors).mockResolvedValue([{ id: 'rentmanager', apiConnected: false }] as never);
    const step = await pmsStep();
    expect(step?.done).toBe(false);
    expect(step?.label).toBe('Connect Rent Manager');
  });

  it('ticks connect and sync for a synced Propertyware card', async () => {
    vi.mocked(listConnectors).mockResolvedValue([
      { id: 'propertyware', apiConnected: true, lastSyncAt: 1_700_000_000_000 },
    ] as never);
    const state = await fetchChecklist();
    const byKey = Object.fromEntries(state.steps.map((s) => [s.key, s.done]));
    expect(byKey.pms).toBe(true);
    expect(byKey.sync).toBe(true);
    expect(state.steps.find((s) => s.key === 'pms')?.label).toBe('Connect Propertyware');
  });

  // The Rentvine test above lists a Buildium card beside tested Rentvine keys;
  // this is the same shape for a card the server says is connected.
  it('names the connected card even beside another listed one', async () => {
    vi.mocked(listConnectors).mockResolvedValue([
      { id: 'buildium', apiConnected: false },
      { id: 'rentmanager', apiConnected: true },
    ] as never);
    expect((await pmsStep())?.label).toBe('Connect Rent Manager');
  });

  it("does not tick connect from another connector's card", async () => {
    vi.mocked(listConnectors).mockResolvedValue([
      { id: 'gmail', apiConnected: true },
      { id: 'rentmanager', apiConnected: false },
    ] as never);
    expect((await pmsStep())?.done).toBe(false);
  });
});

// Before a workspace holds keys, the server lists every system this
// deployment offers, and the step names exactly those.
describe('a workspace on no system yet', () => {
  beforeEach(() => {
    vi.mocked(listChatSessions).mockResolvedValue({ rows: [] } as never);
    vi.mocked(listMembers).mockResolvedValue([{ user_id: 'me' }] as never);
  });

  it('names the systems the server offers, in its order, and nothing else', async () => {
    vi.mocked(listConnectors).mockResolvedValue([
      { id: 'buildium', apiConnected: false },
      { id: 'rentvine', apiConnected: false },
      { id: 'appfolio', apiConnected: false },
      { id: 'rentmanager', apiConnected: false },
      { id: 'propertyware', apiConnected: false },
      { id: 'gmail', apiConnected: true },
    ] as never);
    const step = (await fetchChecklist()).steps.find((s) => s.key === 'pms');
    expect(step?.label).toBe('Connect Buildium, Rentvine, Rent Manager or Propertyware');
    expect(step?.done).toBe(false);
  });

  it('keeps "Buildium or Rentvine" where those are the two offered', async () => {
    vi.mocked(listConnectors).mockResolvedValue([
      { id: 'buildium', apiConnected: false },
      { id: 'rentvine', apiConnected: false },
    ] as never);
    const step = (await fetchChecklist()).steps.find((s) => s.key === 'pms');
    expect(step?.label).toBe('Connect Buildium or Rentvine');
  });

  it('names no system it could not read', async () => {
    vi.mocked(listConnectors).mockRejectedValue(new Error('503'));
    const step = (await fetchChecklist()).steps.find((s) => s.key === 'pms');
    expect(step?.label).toBe('Connect your property management system');
    expect(step?.done).toBe(false);
  });
});
