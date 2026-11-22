/**
 * The workspace delete now reports a SECOND leg — Supabase Storage — and this
 * client used to read `deleted_rows` and throw the rest away.
 *
 * That matters even though nothing calls `deleteCompany` yet (`DeleteAccountRow`
 * keeps the button unwired deliberately, pending a Stripe-cancel guard). The
 * day somebody wires it, the one field that says "the erasure is unfinished"
 * must still be there — dropping it is how the backend gains an honest signal
 * and the screen keeps showing the old confident one.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';

vi.mock('./auth', () => ({
  getAccessToken: vi.fn().mockResolvedValue('tkn'),
  demoMode: false,
}));

import { deleteCompany } from './company';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('deleteCompany — the storage leg', () => {
  beforeEach(() => {
    vi.spyOn(globalThis, 'fetch');
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('carries the storage verdict through, not just the row counts', async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue(
      jsonResponse({
        company_id: 'c1',
        deleted_rows: { helixis_leases: 3 },
        storage_objects_deleted: 12,
        storage_complete: true,
        storage_note: '',
      }),
    );
    const out = await deleteCompany('Acme');
    expect(out.deletedRows).toEqual({ helixis_leases: 3 });
    expect(out.storageObjectsDeleted).toBe(12);
    expect(out.storageComplete).toBe(true);
  });

  it('surfaces the note naming the buckets still holding data', async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue(
      jsonResponse({
        company_id: 'c1',
        deleted_rows: {},
        storage_objects_deleted: 4,
        storage_complete: false,
        storage_note: 'still holding data under prefix c1/ in: browser_screenshots (list 404)',
      }),
    );
    const out = await deleteCompany('Acme');
    expect(out.storageComplete).toBe(false);
    expect(out.storageNote).toContain('browser_screenshots');
  });

  it('reads a MISSING verdict as not-complete, never as clean', async () => {
    // ⚠ The distinguishing case. An older backend, a proxy error page or a body
    // that failed to parse must read as "we do not know" — the one thing this
    // flag exists to prevent is telling somebody their files are gone when
    // nobody looked (gotchas 55/57). `?? true` and `!== false` both get this
    // wrong; `=== true` is what makes absence safe.
    vi.mocked(globalThis.fetch).mockResolvedValue(
      jsonResponse({ company_id: 'c1', deleted_rows: {} }),
    );
    const out = await deleteCompany('Acme');
    expect(out.storageComplete).toBe(false);
    expect(out.storageObjectsDeleted).toBe(0);
  });

  it('reads an unparseable body as not-complete too', async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue(
      new Response('<html>gateway</html>', {
        status: 200,
        headers: { 'Content-Type': 'text/html' },
      }),
    );
    const out = await deleteCompany('Acme');
    expect(out.storageComplete).toBe(false);
  });
});
