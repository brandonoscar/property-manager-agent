/**
 * Hiding a row from the Inbox, and getting it back.
 *
 * ⚠ **The X was one click, next to the timestamp, and there was no way back
 * at all.** It writes an id into `localStorage` and nothing anywhere reads
 * that ledger, so a row dismissed by a mis-click — or last week — was gone
 * from that device until somebody cleared site data. There is no server-side
 * archive behind it either (the detail pane's Archive is disabled chrome), so
 * "restore" could not be a server call even in principle.
 *
 * Two affordances, because they answer different questions. The toast rescues
 * the click you just made. The count in the scope row rescues everything
 * before it, and is the only screen anywhere that admits the ledger exists.
 *
 * ⚠ `sonner` is mocked as a CALLABLE with methods, not an object of methods.
 * The neighbouring file mocks it the second way, which is fine there because
 * nothing in it dismisses a row — a plain object would throw "toast is not a
 * function" here, and that shape of mock is exactly why the dismissal path
 * could go untested without anything looking wrong.
 */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const toastFn = vi.hoisted(() => {
  const fn = vi.fn() as ReturnType<typeof vi.fn> & Record<string, unknown>;
  for (const m of ['success', 'error', 'message', 'warning', 'info']) fn[m] = vi.fn();
  return fn;
});

vi.mock('sonner', () => ({ toast: toastFn }));

/**
 * ⚠ Demo mode is OFF under vitest — `VITE_DEMO_MODE` is unset, so
 * `resolveDemoMode` returns false and the page fetches. (The neighbouring
 * file's smoke test is named "renders without crashing in demo mode" and is
 * actually exercising the empty non-demo path; it passes either way, which is
 * why nobody noticed.) So the rows come from a mocked service.
 */
vi.mock('../services/inbox', async (orig) => {
  const actual = await orig<typeof import('../services/inbox')>();
  return { ...actual, listInboxRows: vi.fn().mockResolvedValue(ROWS) };
});

import { InboxPage } from './InboxPage';
import { useStore } from '../stores/store';

const ROWS = vi.hoisted(() =>
  ['a', 'b', 'c'].map((id, i) => ({
    id: `row-${id}`,
    event_name: 'Task.Created',
    event_time: `2026-09-0${i + 1}T10:00:00Z`,
    status: 'done' as const,
    last_error: null,
    summary_text: `Leaking tap at ${i + 1} Example St`,
    executed_action_index: null,
  })),
);

const LEDGER = 'occupella.inbox.dismissedRows';

beforeEach(() => {
  localStorage.clear();
  toastFn.mockClear();
});
afterEach(() => localStorage.clear());

/** Render and WAIT — the rows arrive from a promise, so a synchronous
 *  assertion measures the empty state and reports "no rows to dismiss". */
async function open(): Promise<number> {
  render(<InboxPage />);
  await waitFor(() => expect(screen.getAllByTestId('inbox-row-dismiss').length).toBeGreaterThan(0));
  return screen.getAllByTestId('inbox-row-dismiss').length;
}

const rowCount = () => screen.queryAllByTestId('inbox-row-dismiss').length;

/** The X on the first row. It is `hidden group-hover:inline-flex`, which is a
 *  Tailwind class jsdom never applies — so it is in the DOM and clickable
 *  here, and the hover-reveal is a visual property this cannot assert. */
const firstDismiss = () => screen.getAllByTestId('inbox-row-dismiss')[0];

describe('dismissing a row', () => {
  it('offers an undo rather than hiding it silently', async () => {
    const before = await open();
    fireEvent.click(firstDismiss());

    expect(rowCount()).toBe(before - 1);
    expect(toastFn).toHaveBeenCalledTimes(1);
    const [message, opts] = toastFn.mock.calls[0] as [string, { action?: { label: string } }];
    expect(message).toMatch(/dismissed/i);
    // ⚠ "on this device" is load-bearing copy, not decoration: the ledger is
    // localStorage, so a colleague still sees the row and the person who
    // dismissed it sees it again on their phone.
    expect(message).toMatch(/this device/i);
    expect(opts.action?.label).toBe('Undo');
  });

  it('names the row it hid', async () => {
    // A bare "Dismissed" leaves somebody who clicked the wrong X with nothing
    // to check the offer against before it disappears.
    await open();
    fireEvent.click(firstDismiss());
    const [, opts] = toastFn.mock.calls[0] as [string, { description?: string }];
    expect(opts.description, 'the toast does not say WHICH row').toBeTruthy();
  });

  it('undo puts the row back', async () => {
    const before = await open();
    fireEvent.click(firstDismiss());

    const [, opts] = toastFn.mock.calls[0] as [string, { action: { onClick: () => void } }];
    act(() => opts.action.onClick());

    expect(rowCount()).toBe(before);
    expect(JSON.parse(localStorage.getItem(LEDGER) ?? '[]')).toEqual([]);
  });

  it('survives a reload, which is the whole point of the ledger', async () => {
    const before = await open();
    fireEvent.click(firstDismiss());
    cleanup();

    render(<InboxPage />);
    await waitFor(() => expect(rowCount()).toBe(before - 1));
  });
});

describe('restoring what was hidden earlier', () => {
  it('shows nothing when nothing is hidden', async () => {
    // A permanent "0 hidden" control is chrome that never does anything.
    await open();
    expect(screen.queryByTestId('inbox-restore-dismissed')).toBeNull();
  });

  it('counts what this device is hiding and brings it back', async () => {
    const before = await open();
    fireEvent.click(firstDismiss());
    fireEvent.click(firstDismiss());

    const restore = screen.getByTestId('inbox-restore-dismissed');
    expect(restore).toHaveTextContent('2 hidden');

    fireEvent.click(restore);
    expect(rowCount()).toBe(before);
    expect(screen.queryByTestId('inbox-restore-dismissed')).toBeNull();
    expect(JSON.parse(localStorage.getItem(LEDGER) ?? '[]')).toEqual([]);
  });

  it('says how many came back, because the control that did it disappears', async () => {
    await open();
    fireEvent.click(firstDismiss());
    toastFn.mockClear();

    fireEvent.click(screen.getByTestId('inbox-restore-dismissed'));
    expect(toastFn).toHaveBeenCalledWith('1 row restored');
  });

});

/**
 * The sidebar badge's number, published from here.
 *
 * ⚠ Only from the UNFILTERED list. `mine` is a rail chip that narrows the
 * Inbox to your own items — publishing that count would make the badge
 * under-report for anybody who had ever clicked it, and an under-reporting
 * badge is indistinguishable from a correct one. A mutation removing the
 * guard survived the hook's own tests, because nothing there renders this
 * page: the guard lives on the producer and only the producer can see it.
 */
describe('what this page tells the sidebar', () => {
  it('publishes the count it just loaded', async () => {
    useStore.setState({ inboxCount: null });
    render(<InboxPage />);
    await waitFor(() => expect(useStore.getState().inboxCount).toBe(ROWS.length));
  });

  it('does NOT publish while the list is filtered to "mine"', async () => {
    render(<InboxPage />);
    await waitFor(() => expect(useStore.getState().inboxCount).toBe(ROWS.length));

    // A narrower result set arrives for the filtered view; the badge, which
    // speaks for the whole Inbox, must not move.
    const inbox = await import('../services/inbox');
    // ⚠ The cast is the file's existing fixture shape, not laziness: ROWS
    // carries only the fields the list renders, and the module-level mock
    // above gets away with it because `vi.fn()` infers nothing. Going through
    // `vi.mocked` brings the real signature in, which is the moment an
    // incomplete fixture stops being invisible. Widening ROWS to a full
    // InboxRow would be a large unrelated edit; naming it here is honest.
    vi.mocked(inbox.listInboxRows).mockResolvedValue(
      ROWS.slice(0, 1) as unknown as Awaited<ReturnType<typeof inbox.listInboxRows>>,
    );
    fireEvent.click(screen.getByTestId('inbox-scope-mine'));
    await waitFor(() => expect(inbox.listInboxRows).toHaveBeenCalledWith(true));
    await act(async () => {
      await Promise.resolve();
    });
    expect(useStore.getState().inboxCount).toBe(ROWS.length);
  });
});
