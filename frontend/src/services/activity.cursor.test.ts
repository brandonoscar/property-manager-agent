/**
 * The Activity cursor survives the trip back to the server.
 *
 * ⚠ **AN ISO-8601 OFFSET ENDS `+00:00`, AND A RAW `+` IN A QUERY STRING
 * DECODES TO A SPACE.** The backend emits `next_cursor` as
 * `datetime.isoformat()` and now parses it as a real `AwareDatetime`, so a
 * mangled cursor is a 422 rather than the silent nonsense a string comparison
 * would have produced. `URLSearchParams` percent-encodes the `+` and the
 * round trip works; a hand-built `` `?cursor=${next}` `` does not — and it
 * looks identical in the diff.
 *
 * This file exists because that correctness lives in ONE character of
 * `fetchActivity`'s implementation and nothing pinned it. The backend half is
 * `tests/unit/test_temporal_bind_parameters.py` (gotcha 28 — a value crossing
 * a producer/consumer boundary needs a test that reads BOTH sides; here the
 * boundary is the URL encoder between them).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.hoisted(() => vi.fn());
vi.mock('./api', () => ({ apiFetch }));

import { fetchActivity } from './activity';

/** What the backend actually puts in `next_cursor`. */
const CURSOR = '2026-08-27T07:46:34.797181+00:00';

describe('the Activity cursor is encoded, not concatenated', () => {
  beforeEach(() => {
    apiFetch.mockReset();
    apiFetch.mockResolvedValue({
      json: async () => ({ rows: [], next_cursor: null }),
    });
  });

  it('percent-encodes the offset so the server reads the instant we sent', async () => {
    await fetchActivity({}, CURSOR);
    const url: string = apiFetch.mock.calls[0][0];

    // The mangling this guards against, stated as the thing that must NOT
    // appear: a bare "+" between the seconds and the offset.
    expect(url).not.toContain('797181+00:00');
    expect(url).toContain('797181%2B00%3A00');

    // And the decisive check — parse it the way a server does and get the
    // instant back. Asserting on the encoded spelling alone would pass
    // against any other escaping scheme that happens to hide the "+".
    const parsed = new URL(url, 'https://example.test').searchParams.get('cursor');
    expect(parsed).toBe(CURSOR);
  });

  it('sends no cursor at all on the first page', async () => {
    await fetchActivity({}, null);
    const url: string = apiFetch.mock.calls[0][0];
    expect(new URL(url, 'https://example.test').searchParams.has('cursor')).toBe(false);
  });

  it('keeps the other filters on the same encoder', async () => {
    // A second call site that built its own query string would reopen the
    // hole for whichever param it carried.
    await fetchActivity({ scope: 'mine', category: 'crm', actor: 'a b' }, CURSOR);
    const params = new URL(apiFetch.mock.calls[0][0], 'https://example.test').searchParams;
    expect(params.get('scope')).toBe('mine');
    expect(params.get('category')).toBe('crm');
    expect(params.get('actor')).toBe('a b');
    expect(params.get('cursor')).toBe(CURSOR);
  });
});
