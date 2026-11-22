/**
 * The Operations page picks its Re-sync (Buildium backfill or Rentvine sync)
 * from `pms` on `GET /mirror/freshness`. The page's own tests mock this
 * client, so the mapping from the wire is pinned here.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('./api', () => ({ apiFetch: vi.fn() }));

import { apiFetch } from './api';
import { fetchMirrorFreshness } from './mirrorFreshness';

const fetchMock = vi.mocked(apiFetch);
function serves(body: unknown) {
  fetchMock.mockResolvedValue({ ok: true, json: async () => body } as unknown as Response);
}
afterEach(() => fetchMock.mockReset());

describe('fetchMirrorFreshness', () => {
  it('keeps which system the workspace syncs from', async () => {
    serves({ as_of: null, has_data: false, pms: 'rentvine', pms_name: 'Rentvine' });
    expect(await fetchMirrorFreshness()).toMatchObject({ pms: 'rentvine', pmsName: 'Rentvine' });
  });

  it('reads an older backend without the field as unknown, not Buildium', async () => {
    serves({ as_of: null, has_data: false });
    expect(await fetchMirrorFreshness()).toMatchObject({ pms: null, pmsName: null });
  });

  it('keeps a third system the server names', async () => {
    serves({ as_of: null, has_data: false, pms: 'rentmanager', pms_name: 'Rent Manager' });
    expect(await fetchMirrorFreshness()).toMatchObject({ pms: 'rentmanager', pmsName: 'Rent Manager' });
  });

  it('reads a system this build does not know as unknown', async () => {
    serves({ as_of: null, has_data: false, pms: 'appfolio', pms_name: 'AppFolio' });
    expect(await fetchMirrorFreshness()).toMatchObject({ pms: null });
  });
});
