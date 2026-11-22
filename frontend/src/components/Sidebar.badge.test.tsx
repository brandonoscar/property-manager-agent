/**
 * The nav badge's ACCESSIBLE name, which the badge itself nearly destroyed.
 *
 * ⚠ Found by a Playwright click timing out, not by review. Rendering the count
 * inside the button changed its accessible name from "Inbox" to "Inbox 04" —
 * and 04 is a zero-padded DISPLAY string, so a screen reader announces "Inbox
 * zero four". A person navigating by name could no longer find the button at
 * all. That is the same thing the timeout was: a name-based lookup failing.
 *
 * So the number is decoration (`aria-hidden`) and the button carries an
 * explicit name that says it in words. The collapsed rail matters more, not
 * less: it is icon-only, its label is the ONLY thing a screen reader gets, and
 * its badge is a 6px dot that conveys nothing at all without it.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('../services/auth', () => ({
  demoMode: true,
  performSignOut: vi.fn(),
}));
vi.mock('../services/chat', () => ({
  listChatSessions: () => Promise.resolve({ rows: [] }),
  loadChatMessages: () => Promise.resolve({ rows: [] }),
}));
vi.mock('../services/analytics', () => ({ captureThreadCreated: vi.fn() }));

import { useStore } from '../stores/store';
import { Sidebar } from './Sidebar';

beforeEach(() => {
  useStore.setState({ inboxCount: null, sidebarCollapsed: false, view: 'chat' });
});

describe('the Inbox badge', () => {
  it('shows the count when something is waiting, as a plain number', () => {
    // Not zero-padded ("04") and not mono: the site sets counts in the UI face.
    useStore.setState({ inboxCount: 4 });
    render(<Sidebar />);
    expect(screen.getByTestId('nav-badge-inbox').textContent).toBe('4');
  });

  it('says it in WORDS to a screen reader, not as a padded number', () => {
    useStore.setState({ inboxCount: 4 });
    render(<Sidebar />);
    expect(screen.getByRole('link', { name: 'Inbox, 4 waiting' })).toBeInTheDocument();
    // And the visible number must not reach the accessible name a second time.
    expect(screen.getByTestId('nav-badge-inbox')).toHaveAttribute('aria-hidden', 'true');
  });

  it('is still called plain "Inbox" when nothing is waiting', () => {
    useStore.setState({ inboxCount: 0 });
    render(<Sidebar />);
    expect(screen.getByRole('link', { name: 'Inbox' })).toBeInTheDocument();
    expect(screen.queryByTestId('nav-badge-inbox')).toBeNull();
  });

  // ⚠ `badge={inboxCount ?? undefined}` vs `?? 0` is NOT a behavioural
  // distinction and a mutation swapping them correctly survived: the badge
  // renders only above 0, so both draw nothing. `undefined` is the honest
  // TYPE for "unknown" and stays for that reason, but pinning the difference
  // would assert something the code cannot express (gotcha 58). What IS
  // load-bearing is the `> 0` guard, which the next two tests cover.
  it('shows NOTHING when the count is unknown', () => {
    // null is unknown, not zero. A badge is a claim about state; a failed
    // fetch has not earned one, in either direction.
    useStore.setState({ inboxCount: null });
    render(<Sidebar />);
    expect(screen.queryByTestId('nav-badge-inbox')).toBeNull();
    expect(screen.getByRole('link', { name: 'Inbox' })).toBeInTheDocument();
  });

  it('reaches the collapsed rail, where the label is all there is', () => {
    // The dot is 6px of colour and conveys nothing to a screen reader, so
    // without the name a collapsed-rail user has no indication at all.
    useStore.setState({ inboxCount: 2, sidebarCollapsed: true });
    render(<Sidebar />);
    expect(screen.getByRole('link', { name: 'Inbox, 2 waiting' })).toBeInTheDocument();
    expect(screen.getByTestId('nav-dot-inbox')).toHaveAttribute('aria-hidden', 'true');
  });

  it("leaves every OTHER nav item's name alone", () => {
    // The badge is opt-in per item. A change that renamed all of them would
    // break every name-based lookup in the app and in both harnesses.
    useStore.setState({ inboxCount: 4 });
    render(<Sidebar />);
    // Places in the app are links (a real href, so they open in a new tab).
    for (const name of ['Chat', 'Operations', 'Leasing']) {
      expect(screen.getByRole('link', { name }), name).toBeInTheDocument();
    }
  });
});
