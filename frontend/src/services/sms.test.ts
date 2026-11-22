/**
 * SMS consent service — wire shapes + the wrong-shape guard.
 *
 * Same contract class as reminders.test.ts: a 200 from /api/v1/sms/consent
 * whose body isn't {consents: [...]} must degrade to [] rather than poison
 * the settings card's state, and the write calls must hit the right
 * endpoints with the right verbs/bodies.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./auth', () => ({ demoMode: false }));
vi.mock('./api', () => ({ apiFetch: vi.fn() }));

import { apiFetch } from './api';
import {
  addSmsConsent,
  fetchSmsStatus,
  listSmsConsent,
  revokeSmsConsent,
} from './sms';

const apiFetchMock = vi.mocked(apiFetch);

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  apiFetchMock.mockClear();
});

describe('listSmsConsent', () => {
  it('returns the consents array on the expected shape', async () => {
    const row = { id: 'c1', phone: '+15551234567', revoked_at: null };
    apiFetchMock.mockResolvedValue(jsonResponse({ consents: [row] }));
    await expect(listSmsConsent()).resolves.toEqual([row]);
    expect(apiFetchMock).toHaveBeenCalledWith('/api/v1/sms/consent');
  });

  it.each([
    ['bare array', []],
    ['unrelated object', { detail: 'boom' }],
    ['null body', null],
    ['consents not an array', { consents: 'nope' }],
  ])('degrades to [] on a 200 with %s', async (_name, body) => {
    apiFetchMock.mockResolvedValue(jsonResponse(body));
    await expect(listSmsConsent()).resolves.toEqual([]);
  });
});

describe('fetchSmsStatus', () => {
  it('hits the status endpoint and returns its body', async () => {
    const status = { enabled: false, configured: true, consent_count: 2, revoked_count: 1 };
    apiFetchMock.mockResolvedValue(jsonResponse(status));
    await expect(fetchSmsStatus()).resolves.toEqual(status);
    expect(apiFetchMock).toHaveBeenCalledWith('/api/v1/sms/status');
  });
});

describe('addSmsConsent', () => {
  it('POSTs the form body to /sms/consent', async () => {
    const created = { id: 'c1', phone: '+15551234567', source: 'app_settings' };
    apiFetchMock.mockResolvedValue(jsonResponse(created));
    await expect(
      addSmsConsent({ phone: '(555) 123-4567', display_name: 'Dana', recipient_kind: 'manager' }),
    ).resolves.toEqual(created);
    const [path, init] = apiFetchMock.mock.calls[0];
    expect(path).toBe('/api/v1/sms/consent');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toEqual({
      phone: '(555) 123-4567',
      display_name: 'Dana',
      recipient_kind: 'manager',
    });
  });
});

describe('revokeSmsConsent', () => {
  it('DELETEs the row id (revoke, not hard-delete, server-side)', async () => {
    const revoked = { id: 'c1', revoked_at: '2026-08-18T12:00:00Z' };
    apiFetchMock.mockResolvedValue(jsonResponse(revoked));
    await expect(revokeSmsConsent('c1')).resolves.toEqual(revoked);
    expect(apiFetchMock).toHaveBeenCalledWith('/api/v1/sms/consent/c1', { method: 'DELETE' });
  });
});
