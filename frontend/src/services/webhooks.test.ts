/**
 * Tests for the sidebar Webhooks page's data layer.
 *
 * Coverage: ``getRecentWebhookEvents`` maps the Buildium-scoped
 * ``/api/v1/buildium/recent-events`` payload into the demo-shaped
 * ``WebhookEvent`` the existing UI renders, stamps ``source:
 * 'buildium'`` on the way through, handles empty results, and
 * short-circuits to the demo seed when ``demoMode`` is true (so
 * investor walkthroughs still look populated).
 *
 * Component-level rendering of the page (skeleton / error / refresh
 * button) needs @testing-library/react + jsdom which aren't installed;
 * service-layer coverage stands in for the API contract.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./auth', () => ({
  getAccessToken: vi.fn().mockResolvedValue('tkn'),
  demoMode: false,
}));

import { getRecentWebhookEvents } from './webhooks';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('getRecentWebhookEvents', () => {
  const fetchSpy = vi.spyOn(globalThis, 'fetch');

  beforeEach(() => {
    fetchSpy.mockReset();
  });

  afterEach(() => {
    fetchSpy.mockReset();
  });

  it('maps the Buildium /recent-events payload to the WebhookEvent shape', async () => {
    // Mirror the known-good prod payload (Task.Created row for
    // a test company, status=done, last_error=null).
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

    const events = await getRecentWebhookEvents();

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url] = fetchSpy.mock.calls[0];
    expect(String(url)).toContain('/api/v1/buildium/recent-events');

    expect(events).toHaveLength(1);
    const [evt] = events;
    expect(evt.id).toBe('36befbba-394a-4ea3-bc93-f6568f3b249d');
    expect(evt.source).toBe('buildium');
    expect(evt.title).toBe('Task.Created');
    expect(evt.status).toBe('handled');
    expect(evt.urgency).toBe('normal');
    // Parsed timestamp survives the ISO → epoch-ms round-trip.
    expect(evt.receivedAt).toBe(
      Date.parse('2026-05-12T07:07:59.742941+00:00'),
    );
    // Original row is preserved on payload so the UI can dig into it.
    expect((evt.payload as { event_name: string }).event_name).toBe(
      'Task.Created',
    );
  });

  it('returns [] when the backend has no events for this company', async () => {
    fetchSpy.mockResolvedValue(jsonResponse([]));
    expect(await getRecentWebhookEvents()).toEqual([]);
  });

  it('maps a backend "failed" event to urgent + new with the error surfaced', async () => {
    fetchSpy.mockResolvedValue(
      jsonResponse([
        {
          id: 'f00d-1',
          event_name: 'Lease.Updated',
          event_time: '2026-05-12T08:00:00+00:00',
          status: 'failed',
          last_error: 'Buildium /v1/leases/123 returned 502',
        },
      ]),
    );

    const [evt] = await getRecentWebhookEvents();
    expect(evt.status).toBe('new');
    expect(evt.urgency).toBe('high');
    expect(evt.summary).toBe('Buildium /v1/leases/123 returned 502');
    expect((evt.payload as { last_error: string }).last_error).toBe(
      'Buildium /v1/leases/123 returned 502',
    );
  });

  it('maps "pending" / "running" to new with normal urgency', async () => {
    fetchSpy.mockResolvedValue(
      jsonResponse([
        {
          id: 'a',
          event_name: 'WorkOrder.Created',
          event_time: '2026-05-12T09:00:00+00:00',
          status: 'pending',
          last_error: null,
        },
        {
          id: 'b',
          event_name: 'WorkOrder.Updated',
          event_time: '2026-05-12T09:01:00+00:00',
          status: 'running',
          last_error: null,
        },
      ]),
    );

    const events = await getRecentWebhookEvents();
    expect(events.map((e) => e.status)).toEqual(['new', 'new']);
    expect(events.map((e) => e.urgency)).toEqual(['normal', 'normal']);
  });

  it('returns [] when the backend response is not an array', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ error: 'oops' }));
    expect(await getRecentWebhookEvents()).toEqual([]);
  });
});

describe('getRecentWebhookEvents in demo mode', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it('short-circuits to seedWebhooks without calling the backend', async () => {
    vi.doMock('./auth', () => ({
      getAccessToken: vi.fn().mockResolvedValue('tkn'),
      demoMode: true,
    }));
    const fetchSpy = vi.spyOn(globalThis, 'fetch');

    const { getRecentWebhookEvents: getDemo } = await import('./webhooks');
    const events = await getDemo();

    expect(fetchSpy).not.toHaveBeenCalled();
    // The demo seed returns a non-empty array of WebhookEvent rows.
    expect(events.length).toBeGreaterThan(0);
    fetchSpy.mockRestore();
  });
});
