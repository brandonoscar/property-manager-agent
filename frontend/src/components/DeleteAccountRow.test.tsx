/**
 * Asking to delete the account — and the two claims that row makes.
 *
 * ⚠ NOTHING HERE DELETES ANYTHING, and that is the design. Deletion is an
 * operator-run script with five manual legs it does not cover, never yet
 * exercised end to end (`docs/runbooks/account-deletion.md`). The row exists
 * because the documented path was "email us" with the address written nowhere
 * in the product — so from inside the app there was no exit at all, the same
 * defect the billing portal link had.
 *
 * Two properties are worth pinning:
 *   1. It says Buildium is untouched. People assume deleting here wipes their
 *      real books, and someone who believes that will never ask.
 *   2. It does not offer a member a button that deletes their colleagues'
 *      data — that is an admin request.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../services/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../services/auth')>()),
  demoMode: false,
}));

import { DeleteAccountRow } from './SettingsPage';
import { useStore } from '../stores/store';

const asRole = (role: 'admin' | 'member') => {
  useStore.getState().setCompanyRole(role);
  render(<DeleteAccountRow />);
};

const link = () => screen.getByRole('link', { name: /request deletion/i });

beforeEach(() => {
  vi.clearAllMocks();
});

describe('the exit is findable from inside the app', () => {
  it('offers a way to ask at all', () => {
    asRole('admin');
    expect(link()).toBeTruthy();
  });

  it('carries the support address in the link, not only in the prose', () => {
    // A description someone has to read and retype is a worse exit than a
    // link. The mailto is the whole mechanism the runbook depends on: it
    // requires the request to come from the address on file, and a person's
    // own mail client is what supplies that.
    asRole('admin');
    expect(link().getAttribute('href')).toContain('mailto:team@occupella.com');
  });
});

describe('what it promises is what actually happens', () => {
  it('does not claim the click deletes anything', () => {
    // The row REQUESTS deletion. A button labelled as if it executes one, over
    // a cascade nobody has run end to end, is a promise the product cannot
    // keep (gotcha 70's stale claim, pointed at a customer).
    asRole('admin');
    expect(screen.queryByRole('button', { name: /^delete( account)?$/i })).toBeNull();
    expect(link().textContent).toMatch(/request/i);
  });

  it('asks support to confirm the scope before anything is removed', () => {
    // ⚠ RE-POINTED, not dropped (2026-09-08). Two assertions here pinned a
    // paragraph on the settings page — "your data in Buildium itself is
    // untouched", "this cannot be undone" — which the founder cut along with
    // every other explanatory sub-line on that pane.
    //
    // The reassurance was doing real work: somebody who thinks this wipes
    // their actual books will not ask at all. It has not been deleted, it has
    // MOVED to where it is answered rather than asserted — the mailto asks
    // support to confirm the scope first, and support's reply is what states
    // it. That sentence is the mechanism now, so it is what gets pinned.
    asRole('admin');
    const href = decodeURIComponent(link().getAttribute('href') ?? '');
    expect(href).toMatch(/confirm what will be removed before you start/i);
  });
});

describe('a member is not handed the whole workspace', () => {
  // ⚠ INVERTED 2026-09-04 (founder direction), not deleted. These two cases
  // asserted that a member saw softer copy AND still got a live mailto, and
  // they were right on their own terms when written. The founder's call is
  // that a member should not see the row at all.
  //
  // The argument the old second case made — "so they are not left with no
  // route" — does not survive contact with what the row actually does: it
  // requests deletion of EVERYONE's data, which was never a member's exit.
  // A member who wants out asks their admin to remove them, which is a
  // different action this row has never performed. Keeping these pointed the
  // other way is what makes a silent revert fail; deleting them would leave
  // that revert green.
  it('renders nothing at all for a member', () => {
    asRole('member');
    expect(screen.queryByRole('link', { name: /request deletion/i })).toBeNull();
    expect(screen.queryByText(/cannot be undone/i)).toBeNull();
    expect(screen.queryByText(/it's an admin request/i)).toBeNull();
  });

  it('renders nothing while the role is still unknown', () => {
    // `companyRole` is null until the probe resolves, and a row that flashes
    // in and then disappears is the same leak with worse timing. Hiding on
    // null is the fail-closed direction — the one a gate should default to.
    useStore.getState().setCompanyRole(null);
    render(<DeleteAccountRow />);
    expect(screen.queryByRole('link', { name: /request deletion/i })).toBeNull();
  });
});

describe('the link does not navigate the app', () => {
  it('is an anchor to mail, not a router push', () => {
    // A mailto rendered as a <button> with an onClick would be swallowed by
    // the SPA and do nothing — the exit would look present and be inert.
    asRole('admin');
    const el = link();
    expect(el.tagName).toBe('A');
    fireEvent.click(el);
    expect(el.getAttribute('href')?.startsWith('mailto:')).toBe(true);
  });
});
