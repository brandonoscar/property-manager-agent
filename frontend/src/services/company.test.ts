/**
 * Tests for the company / team service layer (ADR 0005).
 *
 * Mocks ``getAccessToken`` so the fetches skip the Supabase round-trip and
 * runs with ``demoMode: false`` (the real path), then spies on
 * ``globalThis.fetch`` to lock the URLs, methods, and bodies.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./auth', () => ({
  getAccessToken: vi.fn().mockResolvedValue('tkn'),
  demoMode: false,
}));

import {
  getMyRole,
  getCompanyKnowledge,
  listMembers,
  changeMemberRole,
  removeMember,
  inviteMember,
  listTeam,
  getMyInvites,
  acceptInvite,
  declineInvite,
  withdrawInvite,
  TEAM_ROLES,
  ROLE_LABEL,
  type Member,
} from './company';
import { ApiError } from './api';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const MEMBER: Member = {
  user_id: 'u1',
  email: 'a@b.co',
  display_name: 'Ada',
  role: 'admin',
  is_primary_admin: true,
  is_self: true,
};

// Default benign response: vitest 4 spies pass through to the REAL fetch
// when no implementation is set (and mockReset now detaches the impl, so
// the hooks use mockClear) — a stray background fetch during module init
// must land here, not in Node's fetch with a relative URL.
const fetchSpy = vi
  .spyOn(globalThis, 'fetch')
  .mockResolvedValue(new Response('{}', { headers: { 'Content-Type': 'application/json' } }));

beforeEach(() => fetchSpy.mockClear());
afterEach(() => fetchSpy.mockClear());

describe('role vocabulary', () => {
  it('is the reconciled admin/manager/member set (no owner)', () => {
    expect(TEAM_ROLES.map((r) => r.value)).toEqual(['admin', 'manager', 'member']);
    expect(ROLE_LABEL.admin).toBe('Admin');
    expect(Object.keys(ROLE_LABEL)).not.toContain('owner');
  });
});

describe('getMyRole', () => {
  it('GETs /company/me and returns the role payload', async () => {
    fetchSpy.mockResolvedValue(
      jsonResponse({ company_id: 'c1', role: 'manager', is_primary_admin: false }),
    );
    const me = await getMyRole();
    expect(me.role).toBe('manager');
    const url = fetchSpy.mock.calls[0][0] as string;
    expect(url).toContain('/api/v1/company/me');
  });
});

describe('listMembers', () => {
  it('unwraps the { members } envelope', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ company_id: 'c1', members: [MEMBER] }));
    const members = await listMembers();
    expect(members).toHaveLength(1);
    expect(members[0].user_id).toBe('u1');
    expect((fetchSpy.mock.calls[0][0] as string)).toContain('/api/v1/company/members');
  });

  it('propagates a 403 (member without team access) as ApiError', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ detail: 'nope' }, 403));
    await expect(listMembers()).rejects.toBeInstanceOf(ApiError);
  });
});

describe('changeMemberRole', () => {
  it('POSTs the role to the member role endpoint', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ user_id: 'u2', role: 'manager' }));
    await changeMemberRole('u2', 'manager');
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url as string).toContain('/api/v1/company/members/u2/role');
    expect((init as RequestInit).method).toBe('POST');
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({ role: 'manager' });
  });

  it('surfaces the 409 last-admin guard', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ detail: 'last admin' }, 409));
    await expect(changeMemberRole('u1', 'member')).rejects.toBeInstanceOf(ApiError);
  });
});

describe('removeMember', () => {
  it('DELETEs the member', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ user_id: 'u2', status: 'removed' }));
    await removeMember('u2');
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url as string).toContain('/api/v1/company/members/u2');
    expect((init as RequestInit).method).toBe('DELETE');
  });
});

describe('getCompanyKnowledge', () => {
  it('GETs /company/knowledge and returns the profile', async () => {
    fetchSpy.mockResolvedValue(
      jsonResponse({
        company_id: 'c1',
        content_md: '## Portfolio\n\n40 leases.',
        generated_on: '2026-07-04',
        updated_at: '2026-07-04T09:00:00Z',
      }),
    );
    const k = await getCompanyKnowledge();
    expect(k.content_md).toContain('40 leases');
    expect((fetchSpy.mock.calls[0][0] as string)).toContain('/api/v1/company/knowledge');
  });
});

describe('inviteMember', () => {
  it('POSTs email + role to /company/invite', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ email: 'x@y.co', role: 'member', status: 'ok' }));
    await inviteMember('x@y.co', 'member');
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url as string).toContain('/api/v1/company/invite');
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      email: 'x@y.co',
      role: 'member',
    });
  });
});

describe('invites that wait for an existing account (migration 0112)', () => {
  const lastCall = () => {
    const calls = fetchSpy.mock.calls;
    const [url, init] = calls[calls.length - 1] as [string, RequestInit | undefined];
    return { url: String(url), method: init?.method ?? 'GET' };
  };

  it('listTeam keeps pending invites apart from members', async () => {
    fetchSpy.mockResolvedValue(
      jsonResponse({
        company_id: 'c1',
        members: [MEMBER],
        pending_invites: [
          { id: 'i1', email: 'x@y.co', role: 'member', invited_at: 't', expires_at: 't' },
        ],
      }),
    );
    const team = await listTeam();
    expect(team.members).toEqual([MEMBER]);
    expect(team.pendingInvites.map((i) => i.id)).toEqual(['i1']);
  });

  it('an older backend with no pending_invites reads as none', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ company_id: 'c1', members: [MEMBER] }));
    expect((await listTeam()).pendingInvites).toEqual([]);
  });

  it('reads, accepts, declines and withdraws at their own endpoints', async () => {
    fetchSpy.mockResolvedValue(jsonResponse({ invites: [] }));
    expect(await getMyInvites()).toEqual([]);
    expect(lastCall()).toEqual({ url: expect.stringContaining('/api/v1/company/invites'), method: 'GET' });

    fetchSpy.mockResolvedValue(
      jsonResponse({ company_id: 'c1', company_name: 'Acme', role: 'member' }),
    );
    expect((await acceptInvite('i 1')).company_name).toBe('Acme');
    expect(lastCall()).toEqual({
      url: expect.stringContaining('/api/v1/company/invites/i%201/accept'),
      method: 'POST',
    });

    fetchSpy.mockResolvedValue(jsonResponse({ id: 'i1', status: 'declined' }));
    await declineInvite('i1');
    expect(lastCall()).toEqual({
      url: expect.stringContaining('/api/v1/company/invites/i1/decline'),
      method: 'POST',
    });

    fetchSpy.mockResolvedValue(jsonResponse({ id: 'i1', status: 'withdrawn' }));
    await withdrawInvite('i1');
    expect(lastCall()).toEqual({
      url: expect.stringContaining('/api/v1/company/invites/i1'),
      method: 'DELETE',
    });
  });

  it("an accept the server refuses throws its words", async () => {
    fetchSpy.mockResolvedValue(
      jsonResponse({ detail: 'You already belong to Blue Ridge Properties.' }, 409),
    );
    await expect(acceptInvite('i1')).rejects.toThrow(/Blue Ridge Properties/);
  });

  it('inviteMember says when the invite waits for acceptance', async () => {
    fetchSpy.mockResolvedValue(
      jsonResponse({ email: 'a@b.co', role: 'member', email_sent: false, awaiting_acceptance: true }),
    );
    expect(await inviteMember('a@b.co')).toEqual({ emailSent: false, awaitingAcceptance: true });
  });
});
