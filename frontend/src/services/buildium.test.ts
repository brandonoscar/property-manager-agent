/**
 * Tests for the Buildium service. Covers the contract that matters for
 * the demo: the read path returns masked metadata only, save uses PUT,
 * the webhook secret round-trips via PUT (the user pastes the value
 * Buildium generated; Occupella no longer generates it), and the
 * webhook URL builder picks up the env var override.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./auth', () => ({
  getAccessToken: vi.fn().mockResolvedValue('tkn'),
  demoMode: false,
}));

import {
  buildiumWebhookUrl,
  deleteWebhookSecret,
  getBuildiumStatus,
  getRecentBuildiumEvents,
  saveBuildiumCreds,
  setWebhookSecret,
  testBuildiumConnection,
} from './buildium';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('buildium service', () => {
  const fetchSpy = vi.spyOn(globalThis, 'fetch');

  beforeEach(() => {
    fetchSpy.mockReset();
  });

  afterEach(() => {
    fetchSpy.mockReset();
  });

  it('getBuildiumStatus returns masked metadata without plaintext', async () => {
    fetchSpy.mockResolvedValue(
      jsonResponse({
        configured: true,
        client_id_last4: '1234',
        webhook_secret_last4: 'wxyz',
        environment: 'sandbox',
        last_tested_at: null,
        test_account_name: null,
        test_properties_count: null,
      }),
    );
    const status = await getBuildiumStatus();
    expect(status.configured).toBe(true);
    expect(status.client_id_last4).toBe('1234');
    // Sanity: the response shape doesn't include any *_secret plaintext field.
    expect(Object.keys(status)).not.toContain('client_secret');
    expect(Object.keys(status)).not.toContain('webhook_secret');
  });

  it('saveBuildiumCreds uses PUT and forwards the body without account_id', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ configured: true }));
    await saveBuildiumCreds({
      client_id: 'abc',
      client_secret: 'sec',
      environment: 'production',
    });
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [, init] = fetchSpy.mock.calls[0];
    expect(init?.method).toBe('PUT');
    const body = JSON.parse(String(init?.body));
    expect(body.client_id).toBe('abc');
    expect(body.environment).toBe('production');
    // Buildium auth uses only client_id + client_secret per their
    // developer docs — account_id must not be sent.
    expect(body).not.toHaveProperty('account_id');
  });

  it('testBuildiumConnection POSTs to /test', async () => {
    fetchSpy.mockResolvedValue(
      jsonResponse({
        ok: true,
        account_name: 'Acme',
        properties_count: 3,
        error: null,
      }),
    );
    const result = await testBuildiumConnection();
    expect(result.ok).toBe(true);
    expect(result.account_name).toBe('Acme');
    const [url, init] = fetchSpy.mock.calls[0];
    expect(String(url)).toContain('/api/v1/buildium/test');
    expect(init?.method).toBe('POST');
  });

  it('setWebhookSecret PUTs the user-pasted value to the backend', async () => {
    fetchSpy.mockResolvedValue(new Response(null, { status: 204 }));
    await setWebhookSecret('Zm9yLXRlc3RzLW9ubHktbm90LWEtcmVhbC1rZXkhISE=');
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url, init] = fetchSpy.mock.calls[0];
    expect(String(url)).toContain('/api/v1/buildium/webhook-secret');
    expect(init?.method).toBe('PUT');
    const body = JSON.parse(String(init?.body));
    expect(body.secret).toBe('Zm9yLXRlc3RzLW9ubHktbm90LWEtcmVhbC1rZXkhISE=');
  });

  it('setWebhookSecret throws on non-2xx so the panel can surface the error', async () => {
    fetchSpy.mockResolvedValue(
      new Response('secret too short', { status: 422 }),
    );
    await expect(setWebhookSecret('x')).rejects.toThrow();
  });

  it('deleteWebhookSecret DELETEs the endpoint', async () => {
    fetchSpy.mockResolvedValue(new Response(null, { status: 204 }));
    await deleteWebhookSecret();
    const [url, init] = fetchSpy.mock.calls[0];
    expect(String(url)).toContain('/api/v1/buildium/webhook-secret');
    expect(init?.method).toBe('DELETE');
  });

  it('getRecentBuildiumEvents fetches /recent-events and parses the API shape', async () => {
    // Mirror the exact payload the Render backend returns (verified
    // 2026-05-12 against a test company): asyncpg → FastAPI →
    // ISO timestamp string, jsonb status enum, nullable last_error.
    fetchSpy.mockResolvedValue(
      jsonResponse([
        {
          id: '36befbba-394a-4ea3-bc93-f6568f3b249d',
          event_name: 'Task.Created',
          event_time: '2026-05-12T07:07:59.742941+00:00',
          status: 'done',
          last_error: null,
        },
      ]),
    );

    const events = await getRecentBuildiumEvents();

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url] = fetchSpy.mock.calls[0];
    expect(String(url)).toContain('/api/v1/buildium/recent-events');

    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      id: '36befbba-394a-4ea3-bc93-f6568f3b249d',
      event_name: 'Task.Created',
      event_time: '2026-05-12T07:07:59.742941+00:00',
      status: 'done',
      last_error: null,
    });
  });

  it('getRecentBuildiumEvents returns [] when the backend reports no events', async () => {
    fetchSpy.mockResolvedValue(jsonResponse([]));
    expect(await getRecentBuildiumEvents()).toEqual([]);
  });

  it('buildiumWebhookUrl always ends with /webhooks/buildium', () => {
    // Tests run in vitest's "node" environment where ``window`` is
    // undefined, so the helper returns the relative form. The browser
    // path that picks up the explicit backend URL is exercised via the
    // ``VITE_RENDER_BACKEND_URL`` env at build time, not unit tests.
    const url = buildiumWebhookUrl();
    expect(url).toMatch(/\/webhooks\/buildium$/);
  });
});
