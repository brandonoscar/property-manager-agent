/**
 * The admin sees who has not accepted yet (migration 0112).
 *
 * Someone who already had an Occupella account is NOT on the team until they accept,
 * and gets no email. The Team screen lists them apart from the members, says so, and
 * lets an admin withdraw the invite.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

const withdrawInvite = vi.fn();
let myRole = 'admin';

vi.mock('../services/company', async (importOriginal) => ({
  ...((await importOriginal()) as object),
  getMyRole: () => Promise.resolve({ role: myRole }),
  listTeam: () =>
    Promise.resolve({
      members: [
        {
          user_id: 'u1',
          email: 'owner@example.com',
          display_name: 'Owner',
          role: 'admin',
          is_primary_admin: true,
          is_self: true,
        },
      ],
      pendingInvites: [
        {
          id: 'inv-1',
          email: 'waiting@example.com',
          role: 'manager',
          invited_at: '2026-11-11T12:00:00+00:00',
          expires_at: '2026-11-25T12:00:00+00:00',
        },
      ],
    }),
  withdrawInvite: (id: string) => withdrawInvite(id),
}));
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() } }));

import { useStore } from '../stores/store';
import { SettingsPage } from './SettingsPage';

beforeEach(() => {
  withdrawInvite.mockReset().mockResolvedValue(undefined);
  myRole = 'admin';
  useStore.setState({ settingsSection: 'team', companyRole: 'admin' });
});

describe('pending invites on the Team screen', () => {
  it('lists the invite apart from the members, as pending, with no seat', async () => {
    render(<SettingsPage />);
    const row = await screen.findByTestId('pending-invite');
    expect(within(row).getByText('waiting@example.com')).toBeInTheDocument();
    expect(within(row).getByText('Pending')).toBeInTheDocument();
    expect(within(row).getByText(/Invited as Manager\. They join when they accept/)).toBeInTheDocument();
    // Not a member row: no role picker for them.
    expect(screen.queryByLabelText('Role for waiting@example.com')).toBeNull();
  });

  it('an admin withdraws it after confirming', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<SettingsPage />);
    const row = await screen.findByTestId('pending-invite');
    fireEvent.click(within(row).getByRole('button', { name: 'Withdraw' }));
    await waitFor(() => expect(withdrawInvite).toHaveBeenCalledWith('inv-1'));
  });

  it('a manager sees it but cannot withdraw it', async () => {
    myRole = 'manager';
    useStore.setState({ companyRole: 'manager' });
    render(<SettingsPage />);
    const row = await screen.findByTestId('pending-invite');
    expect(within(row).queryByRole('button', { name: 'Withdraw' })).toBeNull();
  });
});
