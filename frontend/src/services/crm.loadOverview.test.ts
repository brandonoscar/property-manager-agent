/**
 * loadCrmOverview — the explicit fixture-vs-live seam (2026-08-15 audit).
 *
 * The page used to infer "this is demo data" from a caught error; the
 * service now returns the distinction (`isDemo`) so the example-data
 * banner can never drift out of sync with the data source. In its own
 * file (not crm.test.ts) because it needs './api' + './auth' mocked,
 * which the fixture-only tests deliberately avoid.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./auth', () => ({ demoMode: false }));
vi.mock('./api', async (orig) => {
  const actual = await orig<typeof import('./api')>();
  return { ...actual, apiFetch: vi.fn() };
});

import { apiFetch } from './api';
import { loadColdLeads, loadCrmOverview, loadStageLeads } from './crm';
import { demoCrmOverview } from './demoData';

const apiFetchMock = vi.mocked(apiFetch);

beforeEach(() => {
  apiFetchMock.mockReset();
});

describe('loadCrmOverview', () => {
  it('marks the live payload isDemo: false', async () => {
    apiFetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          stages: [{ key: 'new', label: 'New', count: 2, recent: [] }],
          metrics: {
            new_this_month: 2,
            new_prev_month: 1,
            replies_this_month: 0,
            active: 2,
            leased: 0,
            total: 2,
          },
          cold_count: 0,
        }),
        { headers: { 'Content-Type': 'application/json' } },
      ),
    );
    const result = await loadCrmOverview();
    expect(apiFetchMock).toHaveBeenCalledWith('/api/v1/crm/overview');
    expect(result.isDemo).toBe(false);
    expect(result.overview.stages[0].count).toBe(2);
  });

  it('falls back to the fixture preview with isDemo: true when the CRM route is dark (404)', async () => {
    apiFetchMock.mockResolvedValue(new Response('', { status: 404 }));
    const result = await loadCrmOverview();
    expect(result.isDemo).toBe(true);
    // The payload is the same fixture overview the demo build serves.
    expect(result.overview.stages.map((s) => s.key)).toEqual(
      demoCrmOverview().stages.map((s) => s.key),
    );
  });

  it('rethrows a genuine fetch failure — a real user sees an error, never unlabeled fixtures', async () => {
    apiFetchMock.mockRejectedValue(new Error('network down'));
    await expect(loadCrmOverview()).rejects.toThrow('network down');
  });
});

/**
 * The DRILL-DOWN loaders, added 2026-09-16 for the same reason.
 *
 * `loadCrmOverview` had this contract from the 2026-08-15 audit and the two
 * views underneath it never got it: both swallowed every error into an empty
 * lead list, so a 500 rendered as "no leads in this stage" and a dark CRM flag
 * rendered as an empty stage beneath demo counts the overview had just shown.
 *
 * ⚠ These live at the SERVICE layer deliberately. The component tests mock
 * these loaders, so nothing there can see the loader itself swallowing an
 * error or losing the 404 mapping — the well-tested-helper blind spot
 * (gotchas 62/66/69/103/121/136). A mutation battery found both holes.
 */
describe('loadStageLeads', () => {
  it('marks a live payload isDemo: false', async () => {
    apiFetchMock.mockResolvedValue(
      new Response(JSON.stringify({ leads: [], has_more: false }), { status: 200 }),
    );
    const r = await loadStageLeads({ stage: 'new' });
    expect(r.isDemo).toBe(false);
  });

  it('falls back to the labelled fixture when the CRM route is dark (404)', async () => {
    // `listStageLeads` was the ONE CRM reader with no 404 mapping, so this
    // arrived as a generic ApiError and the page showed a failure for a
    // feature that is merely switched off.
    apiFetchMock.mockResolvedValue(new Response('', { status: 404 }));
    const r = await loadStageLeads({ stage: 'new' });
    expect(r.isDemo).toBe(true);
  });

  it('rethrows a genuine failure rather than returning an empty list', async () => {
    // The whole defect in one assertion: an empty list is an ANSWER, and a
    // failed request has not earned one.
    apiFetchMock.mockRejectedValue(new Error('network down'));
    await expect(loadStageLeads({ stage: 'new' })).rejects.toThrow('network down');
  });

  it('rethrows a 500 too — not only transport errors', async () => {
    apiFetchMock.mockResolvedValue(new Response('', { status: 500 }));
    await expect(loadStageLeads({ stage: 'new' })).rejects.toThrow();
  });
});

describe('loadColdLeads', () => {
  it('carries the server threshold through, and marks live data', async () => {
    apiFetchMock.mockResolvedValue(
      new Response(JSON.stringify({ leads: [], count: 0, threshold_days: 7 }), { status: 200 }),
    );
    const r = await loadColdLeads();
    expect(r.isDemo).toBe(false);
    expect(r.threshold_days).toBe(7);
  });

  it('falls back to the labelled fixture when the CRM route is dark (404)', async () => {
    apiFetchMock.mockResolvedValue(new Response('', { status: 404 }));
    expect((await loadColdLeads()).isDemo).toBe(true);
  });

  it('rethrows a genuine failure rather than congratulating the user', async () => {
    apiFetchMock.mockRejectedValue(new Error('network down'));
    await expect(loadColdLeads()).rejects.toThrow('network down');
  });
});
