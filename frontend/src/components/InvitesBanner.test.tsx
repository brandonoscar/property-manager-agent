/**
 * The invitee's answer to an invite (migration 0112): nothing of theirs changes
 * until they press Accept, and Accept is theirs alone.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../services/company', async (importOriginal) => {
  const real = await importOriginal<typeof import('../services/company')>();
  return {
    ...real,
    getMyInvites: vi.fn(),
    acceptInvite: vi.fn(),
    declineInvite: vi.fn(),
  };
});
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() } }));

import { toast } from 'sonner';
import {
  acceptInvite,
  declineInvite,
  getMyInvites,
  type MyInvite,
} from '../services/company';
import { useStore } from '../stores/store';
import { InvitesBanner, inviteSentence, pageReload } from './InvitesBanner';
// Vite's ?raw gives the file's text without needing node types in this tsconfig.
import appSource from '../App.tsx?raw';

const INVITE: MyInvite = {
  id: 'inv-1',
  company_id: 'c-acme',
  company_name: 'Acme Property Co',
  role: 'manager',
  invited_by_email: 'owner@example.com',
  expires_at: '2026-11-25T12:00:00+00:00',
};

function signIn() {
  useStore.setState({ user: { id: 'u-1' } as never });
}

beforeEach(() => {
  vi.mocked(getMyInvites).mockReset();
  vi.mocked(acceptInvite).mockReset();
  vi.mocked(declineInvite).mockReset();
  vi.mocked(toast.success).mockReset();
  vi.mocked(toast.error).mockReset();
});

afterEach(() => {
  useStore.setState({ user: null });
  vi.restoreAllMocks();
});

describe('inviteSentence', () => {
  it('names who asked, the company and the role', () => {
    expect(inviteSentence(INVITE)).toBe(
      'owner@example.com invited you to join Acme Property Co as Manager.',
    );
  });

  it('still reads when the inviter is gone', () => {
    expect(inviteSentence({ ...INVITE, invited_by_email: null })).toBe(
      'An admin invited you to join Acme Property Co as Manager.',
    );
  });
});

describe('InvitesBanner', () => {
  it('renders nothing when nothing waits, and asks nothing while signed out', async () => {
    vi.mocked(getMyInvites).mockResolvedValue([]);
    const { container, rerender } = render(<InvitesBanner />);
    expect(getMyInvites).not.toHaveBeenCalled();
    signIn();
    rerender(<InvitesBanner />);
    await waitFor(() => expect(getMyInvites).toHaveBeenCalledTimes(1));
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when the read fails, rather than an error across every screen', async () => {
    vi.mocked(getMyInvites).mockRejectedValue(new Error('down'));
    signIn();
    const { container } = render(<InvitesBanner />);
    await waitFor(() => expect(getMyInvites).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it('accepting joins, then reloads so every screen moves to the new company', async () => {
    vi.mocked(getMyInvites).mockResolvedValue([INVITE]);
    vi.mocked(acceptInvite).mockResolvedValue({
      company_id: 'c-acme',
      company_name: 'Acme Property Co',
      role: 'manager',
    });
    const reload = vi.spyOn(pageReload, 'now').mockImplementation(() => undefined);
    signIn();
    render(<InvitesBanner />);
    fireEvent.click(await screen.findByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
    expect(acceptInvite).toHaveBeenCalledWith('inv-1');
    expect(toast.success).toHaveBeenCalledWith('You joined Acme Property Co.');
  });

  it("a refused accept shows the server's words and stays put", async () => {
    vi.mocked(getMyInvites).mockResolvedValue([INVITE]);
    vi.mocked(acceptInvite).mockRejectedValue(
      new Error('You already belong to Blue Ridge Properties. Leave that one first.'),
    );
    const reload = vi.spyOn(pageReload, 'now').mockImplementation(() => undefined);
    signIn();
    render(<InvitesBanner />);
    fireEvent.click(await screen.findByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(vi.mocked(toast.error).mock.calls[0][0]).toContain('Blue Ridge Properties');
    expect(reload).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Accept' })).not.toBeDisabled();
  });

  it('declining removes the invite and changes nothing else', async () => {
    vi.mocked(getMyInvites).mockResolvedValue([INVITE]);
    vi.mocked(declineInvite).mockResolvedValue(undefined);
    const reload = vi.spyOn(pageReload, 'now').mockImplementation(() => undefined);
    signIn();
    render(<InvitesBanner />);
    fireEvent.click(await screen.findByRole('button', { name: 'Decline' }));
    await waitFor(() => expect(screen.queryByTestId('invites-banner')).toBeNull());
    expect(declineInvite).toHaveBeenCalledWith('inv-1');
    expect(acceptInvite).not.toHaveBeenCalled();
    expect(reload).not.toHaveBeenCalled();
  });

  it('is mounted in the app shell (a banner nothing renders is gotcha 62)', () => {
    expect(appSource).toMatch(/<InvitesBanner\s*\/>/);
  });
});
