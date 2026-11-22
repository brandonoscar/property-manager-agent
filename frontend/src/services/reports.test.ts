/**
 * Morning brief service — demo items, API mapping, per-day dismissal.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./auth', () => ({ demoMode: false }));
vi.mock('./api', () => ({ apiFetch: vi.fn() }));

import { apiFetch } from './api';
import {
  dismissBriefForToday,
  dismissRiskFlag,
  fetchMorningBrief,
  isBriefDismissedToday,
} from './reports';

const apiFetchMock = vi.mocked(apiFetch);

beforeEach(() => {
  localStorage.clear();
  apiFetchMock.mockClear();
  apiFetchMock.mockResolvedValue(
    new Response(JSON.stringify({ items: [] }), {
      headers: { 'Content-Type': 'application/json' },
    }),
  );
});

describe('fetchMorningBrief', () => {
  it('returns the backend items', async () => {
    apiFetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          generated_at: 'now',
          items: [
            {
              key: 'delinquency',
              count: 3,
              amount: 900,
              label: '$900 owed across 3 leases',
              detail: 'Ask who is behind.',
              view: 'chat',
            },
          ],
        }),
        { headers: { 'Content-Type': 'application/json' } },
      ),
    );

    const items = await fetchMorningBrief();
    expect(apiFetchMock).toHaveBeenCalledWith('/api/v1/reports/morning-brief');
    expect(items).toHaveLength(1);
    expect(items[0].view).toBe('chat');
  });

  it('treats a missing items array as empty', async () => {
    apiFetchMock.mockResolvedValue(
      new Response(JSON.stringify({}), { headers: { 'Content-Type': 'application/json' } }),
    );
    expect(await fetchMorningBrief()).toEqual([]);
  });
});

describe('dismissal', () => {
  it('is per-day: dismissing today hides today only', () => {
    expect(isBriefDismissedToday()).toBe(false);
    dismissBriefForToday();
    expect(isBriefDismissedToday()).toBe(true);
    // yesterday's dismissal no longer applies
    localStorage.setItem('helixis.morningBrief.dismissed', '2020-01-01');
    expect(isBriefDismissedToday()).toBe(false);
  });
});

describe('dismissRiskFlag', () => {
  it('PATCHes the flag to dismissed on the risk-flag ledger', async () => {
    apiFetchMock.mockResolvedValue(new Response(JSON.stringify({ id: 'f1', status: 'dismissed' })));
    await dismissRiskFlag('f1');
    expect(apiFetchMock).toHaveBeenCalledWith('/api/v1/reports/risk-flags/f1', {
      method: 'PATCH',
      body: JSON.stringify({ status: 'dismissed' }),
    });
  });
});
