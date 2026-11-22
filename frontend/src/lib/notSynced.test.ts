/**
 * The wording helpers for "this system doesn't send it" (lib/notSynced.ts),
 * and the fetchers that must keep the backend's null instead of turning it
 * into `[]`.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../services/api', () => ({ apiFetch: vi.fn(), ApiError: class extends Error {} }));
vi.mock('../services/auth', () => ({ demoMode: false }));

import { apiFetch } from '../services/api';
import { fetchTeamOps, fetchTenantOps, fetchVendorOps } from '../services/operations';
import { fetchCockpitOverview } from '../services/properties';
import { delinquencyTile, listOrNotSynced, notSynced, NotSyncedRows, sumKnown } from './notSynced';

const money = (n: number) => `$${n}`;

describe('notSynced', () => {
  it('names the system when it is known', () => {
    expect(notSynced('Rentvine', 'rent balances')).toBe(
      "Occupella doesn't sync rent balances from Rentvine yet.",
    );
  });

  it('never claims a system when the backend could not say which', () => {
    expect(notSynced(null, 'rent balances')).toBe("We can't read rent balances right now.");
  });
});

describe('delinquencyTile', () => {
  it('shows unknown, never $0 or "Everyone current", for a null count', () => {
    const tile = delinquencyTile(null, null, 'Rentvine', money);
    expect(tile.value).toBe('—');
    expect(tile.sub).toContain('Rentvine');
    expect(tile.sub).not.toContain('current');
  });

  it('still says everyone is current when the system says so', () => {
    expect(delinquencyTile(0, 0, 'Buildium', money)).toEqual({
      value: '$0',
      sub: 'Everyone current',
    });
    expect(delinquencyTile(2, 900, 'Buildium', money)).toEqual({
      value: '$900',
      sub: '2 leases behind',
    });
  });
});

describe('listOrNotSynced', () => {
  it('keeps the ordinary empty copy for an empty list', () => {
    expect(listOrNotSynced([], 'No tasks.', 'Rentvine', 'tasks')).toEqual({
      items: [],
      empty: 'No tasks.',
    });
  });

  it('says why a declined list is empty', () => {
    expect(listOrNotSynced(null, 'No tasks.', 'Rentvine', 'tasks').empty).toBe(
      "Occupella doesn't sync tasks from Rentvine yet.",
    );
  });
});

describe('sumKnown', () => {
  it('is unknown when any row is unknown', () => {
    expect(sumKnown([{ v: 1 }, { v: null }], (r) => r.v)).toBeNull();
    expect(sumKnown([{ v: 1 }, { v: 2 }], (r) => r.v)).toBe(3);
  });
});

// ── the fetchers ────────────────────────────────────────────────────

const fetchMock = vi.mocked(apiFetch);
function serves(body: unknown) {
  fetchMock.mockResolvedValue({ json: async () => body } as unknown as Response);
}
afterEach(() => fetchMock.mockReset());

describe('the Operations fetchers keep null', () => {
  it('turns a declined tenants list into NotSyncedRows, not []', async () => {
    serves({ tenants: null, pms_name: 'Rentvine' });
    const out = await fetchTenantOps();
    expect(out).toBeInstanceOf(NotSyncedRows);
    expect((out as NotSyncedRows).pmsName).toBe('Rentvine');
  });

  it('turns a declined team list into NotSyncedRows', async () => {
    serves({ members: null, pms_name: 'Rentvine' });
    expect(await fetchTeamOps()).toBeInstanceOf(NotSyncedRows);
  });

  it('keeps a real empty list empty', async () => {
    serves({ tenants: [], pms_name: 'Buildium' });
    expect(await fetchTenantOps()).toEqual([]);
  });

  it('copies the system name onto vendor and property rows', async () => {
    serves({ vendors: [{ buildium_id: 1, spend_12m: null }], pms_name: 'Rentvine' });
    expect((await fetchVendorOps())[0]).toMatchObject({ spend_12m: null, pms_name: 'Rentvine' });
    serves({ properties: [{ buildium_id: 7, delinquent_total: null }], pms_name: 'Rentvine' });
    expect((await fetchCockpitOverview())[0]).toMatchObject({
      delinquent_total: null,
      pms_name: 'Rentvine',
    });
  });
});
