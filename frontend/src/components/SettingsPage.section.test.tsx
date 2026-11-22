/**
 * Which Settings pane opens — the store's answer, not a local one.
 *
 * ⚠ This is a live bug's regression, not a refactor's. `settingsSection` was
 * WRITTEN by `openSettings` and by the `g a` nav chord, and read by NOTHING:
 * SettingsPage kept its own `useState("account")`. Measured in a real browser,
 * `g a` opened Settings and showed Account while the rail listed Team Activity
 * — so the ⌘K "Team Activity" command and the chord both silently did half
 * their job. The file's own comment on that row claimed the opposite ("⌘K and
 * the `g a` chord still reach it, now by deep-linking this section"), which is
 * gotcha 48: a comment cannot fail, so it went on being wrong.
 *
 * Reading the store is also what makes `/settings/<section>` addressable at
 * all — the URL sets the store, and the store is what the pane follows.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

// Spread the real module rather than replacing it: a bare object mock makes
// every OTHER export undefined, and a panel that reaches for one throws an
// unhandled rejection unrelated to what is under test.
vi.mock('../services/company', async (importOriginal) => ({
  ...((await importOriginal()) as object),
  getMyRole: () => Promise.resolve({ role: 'admin' }),
  listMembers: () => Promise.resolve([]),
  // The Team panel reads the roster and the invites still waiting in one call.
  listTeam: () => Promise.resolve({ members: [], pendingInvites: [] }),
}));

import { useStore } from '../stores/store';
import { SettingsPage } from './SettingsPage';

beforeEach(() => {
  useStore.setState({ settingsSection: 'account', companyRole: 'admin' });
});

describe('SettingsPage follows the store', () => {
  it('opens the section the store names, not always Account', async () => {
    useStore.setState({ settingsSection: 'billing' });
    render(<SettingsPage />);
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Billing' })).toBeInTheDocument(),
    );
  });

  it('follows a section change while it is already open', async () => {
    // The `g a` case exactly: you are on Settings, you press the chord, and
    // the pane has to move. A component holding its own initial state would
    // pass the test above and fail this one.
    //
    // ⚠ Driven with `team` rather than `activity` for harness reasons only:
    // ActivityPage needs IntersectionObserver and its own service mocks, and
    // standing those up here would be testing ActivityPage. The wiring under
    // test is the same for every section — one store read.
    render(<SettingsPage />);
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Account' })).toBeInTheDocument(),
    );
    useStore.setState({ settingsSection: 'team' });
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Team & Permissions' })).toBeInTheDocument(),
    );
  });

  it('writes the section back when the rail is clicked', async () => {
    // The other direction — without it the URL would never follow a click on
    // the rail, and only chords and links could change the address bar.
    render(<SettingsPage />);
    await waitFor(() => expect(screen.getByText('AI & Memory')).toBeInTheDocument());
    screen.getByText('AI & Memory').click();
    await waitFor(() => expect(useStore.getState().settingsSection).toBe('ai'));
  });
});
