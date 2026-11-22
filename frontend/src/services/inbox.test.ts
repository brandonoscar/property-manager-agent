/**
 * Tests for the Inbox service layer.
 *
 * Mocks ``getAccessToken`` so the service-layer fetches don't pay the
 * Supabase round-trip, then spies on ``globalThis.fetch`` to assert
 * the right URLs, methods, and bodies go out.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./auth', () => ({
  getAccessToken: vi.fn().mockResolvedValue('tkn'),
  demoMode: false,
}));

import {
  executeInboxAction,
  listInboxRows,
  requestKindLabel,
  submitInboxFreeform,
} from './inbox';
import { ApiError } from './api';

describe('requestKindLabel', () => {
  it('labels owner / contact / association requests (case-insensitive)', () => {
    expect(requestKindLabel('RentalOwner')).toBe('Owner request');
    expect(requestKindLabel('rentalowner')).toBe('Owner request');
    expect(requestKindLabel('Owner')).toBe('Owner request');
    expect(requestKindLabel('Contact')).toBe('Contact request');
    expect(requestKindLabel('Tenant')).toBe('Resident request');
    expect(requestKindLabel('Association')).toBe('Association request');
  });

  it('returns null for absent / unknown types (generic Task card)', () => {
    expect(requestKindLabel(null)).toBeNull();
    expect(requestKindLabel(undefined)).toBeNull();
    expect(requestKindLabel('')).toBeNull();
    expect(requestKindLabel('SomethingNew')).toBeNull();
  });
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

// Canonical row shape from the Phase A.2 prompt — kept as the
// fixture for every test so the wire-format contract is locked.
const CANONICAL_ROW = {
  id: '321d5883-5844-4cc8-9c0d-65b9c0bccf2a',
  event_name: 'Task.Created',
  event_time: '2026-05-13T23:39:58Z',
  status: 'done',
  last_error: null,
  executed_action_index: null,
  action_surface: {
    summary: 'Tenant Josh Gil in Unit 1 at 1534 Cheat Lake Road reported an active leak…',
    actions: [
      {
        label: 'Set priority to High & categorize as Plumbing',
        intent: 'Raise priority to High and categorize as Plumbing',
        tool_id: 'update_buildium_task',
        parameters: { category: 'Plumbing', priority: 'High' },
      },
    ],
    freeform_placeholder: "e.g. 'Call preferred plumber directly'",
  },
  enrichment: {
    task: {
      id: 11154,
      unit_id: 58280,
      property_id: 31,
      assigned_to_user_id: 77042,
      title: 'Hello World',
      description: null,
      category: null,
      priority: null,
      status: null,
      due_date: null,
    },
    active_lease: {
      id: 45104,
      status: 'Active',
      lease_from_date: null,
      lease_to_date: null,
      tenants: [
        { id: 82336, first_name: 'Josh', last_name: 'Gil', email: null, phone: null },
      ],
    },
  },
};

describe('listInboxRows', () => {
  const fetchSpy = vi.spyOn(globalThis, 'fetch');

  beforeEach(() => fetchSpy.mockClear());
  afterEach(() => fetchSpy.mockClear());

  it('requests action_surface + enrichment and parses the canonical row', async () => {
    fetchSpy.mockResolvedValue(jsonResponse([CANONICAL_ROW]));

    const rows = await listInboxRows();

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url] = fetchSpy.mock.calls[0];
    expect(String(url)).toContain('/api/v1/buildium/recent-events');
    expect(String(url)).toContain('include=action_surface');
    expect(String(url)).toContain('limit=20');

    expect(rows).toHaveLength(1);
    expect(rows[0].action_surface?.actions[0].tool_id).toBe('update_buildium_task');
    expect(rows[0].enrichment?.task.id).toBe(11154);
    expect(rows[0].executed_action_index).toBeNull();
  });

  it('returns [] when the backend ships a non-array body', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ error: 'oops' }));
    expect(await listInboxRows()).toEqual([]);
  });

  it('adds assignee=me only for the "My work" scope (#69)', async () => {
    // Fresh Response per call — a Response body can only be read once.
    fetchSpy.mockResolvedValue(jsonResponse([CANONICAL_ROW]));
    await listInboxRows(true);
    expect(String(fetchSpy.mock.calls[0][0])).toContain('assignee=me');

    fetchSpy.mockClear();
    fetchSpy.mockResolvedValue(jsonResponse([CANONICAL_ROW]));
    await listInboxRows(false);
    expect(String(fetchSpy.mock.calls[0][0])).not.toContain('assignee=me');
  });
});

describe('executeInboxAction', () => {
  const fetchSpy = vi.spyOn(globalThis, 'fetch');

  beforeEach(() => fetchSpy.mockClear());
  afterEach(() => fetchSpy.mockClear());

  it('POSTs parameter_overrides + freeform_note to the right URL', async () => {
    fetchSpy.mockResolvedValue(
      jsonResponse({ status: 'executed', result: { buildium_id: 11154, summary: 'ok' } }),
    );

    const res = await executeInboxAction(
      'event-1',
      0,
      { priority: 'Medium' },
      'and ping the plumber',
    );

    expect(res.status).toBe('executed');
    expect(res.result.buildium_id).toBe(11154);

    const [url, init] = fetchSpy.mock.calls[0];
    expect(String(url)).toBe('/api/v1/buildium/events/event-1/actions/0/execute');
    expect(init?.method).toBe('POST');
    const body = JSON.parse(String(init?.body));
    expect(body.parameter_overrides).toEqual({ priority: 'Medium' });
    expect(body.freeform_note).toBe('and ping the plumber');
  });

  it('throws ApiError on 409 already-executed', async () => {
    fetchSpy.mockResolvedValue(
      jsonResponse({ detail: 'event already actioned via action #0' }, 409),
    );

    await expect(
      executeInboxAction('event-1', 0, null, null),
    ).rejects.toBeInstanceOf(ApiError);
  });

  it('throws ApiError on 502 handler error', async () => {
    fetchSpy.mockResolvedValue(
      jsonResponse(
        {
          detail: {
            error: 'Buildium rejected the task update',
            tool_id: 'update_buildium_task',
          },
        },
        502,
      ),
    );

    await expect(
      executeInboxAction('event-1', 0, null, null),
    ).rejects.toBeInstanceOf(ApiError);
  });
});

describe('submitInboxFreeform', () => {
  const fetchSpy = vi.spyOn(globalThis, 'fetch');

  beforeEach(() => fetchSpy.mockClear());
  afterEach(() => fetchSpy.mockClear());

  it('POSTs the note and resolves on 200', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ status: 'recorded' }));
    await submitInboxFreeform('event-1', 'Call preferred plumber directly');

    const [url, init] = fetchSpy.mock.calls[0];
    expect(String(url)).toBe('/api/v1/buildium/events/event-1/freeform');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toEqual({
      note: 'Call preferred plumber directly',
      session_id: null,
    });
  });

  it('threads the live session id so the agent replies on the watched channel', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ status: 'recorded' }));
    await submitInboxFreeform('event-1', 'reply to the tenant', 'thread:abc-123');

    const [, init] = fetchSpy.mock.calls[0];
    expect(JSON.parse(String(init?.body))).toEqual({
      note: 'reply to the tenant',
      session_id: 'thread:abc-123',
    });
  });

  it('throws ApiError on 404 event-not-found', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ detail: 'event not found' }, 404));
    await expect(submitInboxFreeform('nope', 'x')).rejects.toBeInstanceOf(ApiError);
  });
});
