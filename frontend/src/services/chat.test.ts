/**
 * Chat sessions service (PR #71).
 *
 * Mirrors the existing ``inbox.test.ts`` shape — mocks ``apiFetch``
 * and asserts the URL + decoded JSON. Covers both endpoints (the
 * sidebar list + the per-thread message replay).
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';

import { listChatSessions, loadChatMessages } from './chat';

vi.mock('./api', () => ({
  apiFetch: vi.fn(),
  ApiError: class extends Error {
    constructor(
      public status: number,
      msg: string,
    ) {
      super(msg);
    }
  },
}));

import { apiFetch } from './api';

const apiFetchMock = apiFetch as unknown as ReturnType<typeof vi.fn>;

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  apiFetchMock.mockReset();
});

describe('listChatSessions', () => {
  it('hits the sessions endpoint with the documented limit', async () => {
    apiFetchMock.mockResolvedValue(
      jsonResponse({
        rows: [
          {
            session_id: 's1',
            preview: 'what leases are about to end',
            created_at: '2026-05-17T00:00:00+00:00',
          },
        ],
      }),
    );
    const page = await listChatSessions(25);
    expect(apiFetchMock).toHaveBeenCalledWith('/api/v1/chat/sessions?limit=25');
    expect(page.rows).toHaveLength(1);
    expect(page.rows[0].preview).toBe('what leases are about to end');
  });

  it('defaults to limit=50 when not specified', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({ rows: [] }));
    await listChatSessions();
    expect(apiFetchMock).toHaveBeenCalledWith('/api/v1/chat/sessions?limit=50');
  });
});

describe('loadChatMessages', () => {
  it('URL-encodes the session id and passes the limit', async () => {
    apiFetchMock.mockResolvedValue(
      jsonResponse({
        rows: [
          {
            id: 'msg-1',
            role: 'user',
            content: 'hi',
            tool_name: null,
            tool_result: null,
            created_at: '2026-05-17T00:00:00+00:00',
            user_id: 'u',
          },
        ],
      }),
    );
    const page = await loadChatMessages('thread:abc def', 200);
    expect(apiFetchMock).toHaveBeenCalledWith(
      '/api/v1/chat/sessions/thread%3Aabc%20def/messages?limit=200',
    );
    expect(page.rows[0].role).toBe('user');
  });

  it('returns the raw message list shape for the caller to render', async () => {
    apiFetchMock.mockResolvedValue(
      jsonResponse({
        rows: [
          {
            id: '1',
            role: 'assistant',
            content: 'hello',
            tool_name: null,
            tool_result: null,
            created_at: '',
            user_id: null,
          },
          {
            id: '2',
            role: 'tool',
            content: '',
            tool_name: 'buildium_list_properties',
            tool_result: [{ id: 1, name: 'Oak St' }],
            created_at: '',
            user_id: null,
          },
        ],
      }),
    );
    const page = await loadChatMessages('s1');
    expect(page.rows.map((r) => r.role)).toEqual(['assistant', 'tool']);
    expect(page.rows[1].tool_name).toBe('buildium_list_properties');
  });
});
