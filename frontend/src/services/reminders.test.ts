/**
 * Reminders service — the wrong-shape guard.
 *
 * A 200 from /api/v1/reminders whose body isn't {reminders: [...]}
 * used to resolve `undefined` into RemindersStrip state; the next
 * render read `.length` on it and unmounted the ENTIRE Inbox tree —
 * live webhook cards became unreachable ("webhooks aren't clickable",
 * 2026-07-07). fetchReminders must degrade to [] on any shape.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./auth', () => ({ demoMode: false }));
vi.mock('./api', () => ({ apiFetch: vi.fn() }));

import { apiFetch } from './api';
import { fetchReminders } from './reminders';

const apiFetchMock = vi.mocked(apiFetch);

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  apiFetchMock.mockClear();
});

describe('fetchReminders', () => {
  it('returns the reminders array on the expected shape', async () => {
    const reminder = { id: 'r1', title: 'Renewal window' };
    apiFetchMock.mockResolvedValue(jsonResponse({ reminders: [reminder] }));
    await expect(fetchReminders()).resolves.toEqual([reminder]);
  });

  it.each([
    ['bare array', []],
    ['unrelated object', { detail: 'boom' }],
    ['null body', null],
    ['reminders not an array', { reminders: 'nope' }],
  ])('degrades to [] on a 200 with %s', async (_name, body) => {
    apiFetchMock.mockResolvedValue(jsonResponse(body));
    await expect(fetchReminders()).resolves.toEqual([]);
  });
});
