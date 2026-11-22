/**
 * Changing your password from Settings, and the guard that makes it safe.
 *
 * Until this shipped there was no way to change a password while signed in —
 * you signed out and used "Forgot password?". That works but reads as a gap,
 * and every product a customer compares this to has it in Account.
 *
 * ⚠ THE LOAD-BEARING TEST IS `requires the current password`.
 * `supabase.auth.updateUser({ password })` asks for nothing but a live
 * session, so a settings form that called it directly would let anyone at an
 * unlocked laptop change the password and lock the real owner out of their
 * company's books. `changePassword` verifies first; these tests exist so a
 * future simplification that drops that step fails loudly instead of shipping
 * a form that looks identical and is not.
 *
 * ⚠ `vi.mock` is hoisted above every top-level binding, so a spy shared with
 * a factory must be created inside `vi.hoisted()` (gotcha 44).
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// ⚠ The error CLASS lives inside vi.hoisted() too. `vi.mock` is hoisted above
// every top-level binding, so a `class` declared at module scope and named in
// the factory hits the temporal dead zone — `Cannot access
// 'FakeCurrentPasswordWrong' before initialization`, and vitest reports
// `Tests  no tests`, which scans exactly like a pass (gotchas 44 and 64).
const h = vi.hoisted(() => {
  class FakeCurrentPasswordWrong extends Error {
    constructor() {
      super('That current password is incorrect.');
      this.name = 'CurrentPasswordWrong';
    }
  }
  return {
    changePassword: vi.fn(),
    updateUser: vi.fn(),
    toastSuccess: vi.fn(),
    FakeCurrentPasswordWrong,
  };
});

// Spread the REAL module and override only what this test drives. SettingsPage
// pulls in a wide transitive tree (services/api imports getAccessToken from
// here), so a hand-written mock has to re-declare exports that have nothing to
// do with passwords — and each omission fails far from its cause.
//
// ⚠ `updateUser` is reached through the `supabase` CLIENT, not as an export of
// this module — so a bare `updateUser: h.updateUser` here would wire the spy to
// NOTHING and `never calls updateUser directly` could never fail, whatever the
// component did (gotcha 83: a guard on the contents of a declaration nothing
// reads). It is proxied onto the real client instead, so the assertion has a
// reachable target. The Proxy (rather than a hand-built stub) keeps every other
// method on the real GoTrueClient — they live on the prototype, so a spread
// would silently drop them and break unrelated callers in the render tree.
vi.mock('../services/auth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/auth')>();
  return {
    ...actual,
    demoMode: false,
    changePassword: h.changePassword,
    CurrentPasswordWrong: h.FakeCurrentPasswordWrong,
    supabase: new Proxy(actual.supabase, {
      get(target, prop, receiver) {
        if (prop !== 'auth') return Reflect.get(target, prop, receiver);
        return new Proxy(target.auth, {
          get(authTarget, authProp) {
            if (authProp === 'updateUser') return h.updateUser;
            const value = Reflect.get(authTarget, authProp);
            return typeof value === 'function' ? value.bind(authTarget) : value;
          },
        });
      },
    }),
  };
});

vi.mock('sonner', () => ({
  toast: { success: h.toastSuccess, error: vi.fn() },
}));

import { ChangePasswordRow } from './SettingsPage';

const open = () =>
  fireEvent.click(screen.getByRole('button', { name: /change password/i }));
const field = (label: RegExp) => screen.getByLabelText(label);
const save = () => screen.getByRole('button', { name: /save password/i });

/**
 * ⚠ EVERY FIXTURE PASSWORD HERE MUST CLEAR THE REAL FLOOR (8 characters, one
 * capital, one number) unless the test is specifically about a password that
 * does not. The nine tests in this file were written against a 6-character
 * floor with no composition rule, and the moment the floor moved they all
 * failed at once with `expected "vi.fn()" to be called at least once` — the
 * service was never reached because Save was disabled. That is the fixture
 * lesson from gotchas 67/72/73: a fixture has to say which state it MEANS,
 * and `brand-new-one` silently meant "rejected" after the change.
 */
const GOOD = 'Brand-new-one1';

function fill(current: string, next: string, confirm: string) {
  fireEvent.change(field(/current password/i), { target: { value: current } });
  fireEvent.change(field(/^new password$/i), { target: { value: next } });
  fireEvent.change(field(/confirm new password/i), { target: { value: confirm } });
}

/**
 * Submit via the keyboard.
 *
 * ⚠ Not interchangeable with clicking Save. Save is `disabled` when the
 * password fails the checklist, and `fireEvent.click` on a disabled button
 * does nothing — so a test that clicks it to prove a refusal proves only that
 * the button was disabled, whatever the handler does. Enter on the confirm
 * field is NOT gated by `disabled`, so it is the path that actually reaches
 * `submit()`'s own guards. Those guards are the ones that must hold: a
 * disabled button is a hint, and the check inside submit is the rule.
 */
const pressEnter = () =>
  fireEvent.keyDown(field(/confirm new password/i), { key: 'Enter' });

beforeEach(() => {
  vi.clearAllMocks();
  h.changePassword.mockResolvedValue(undefined);
});

describe('the control exists and is collapsed', () => {
  it('offers a way to change the password at all', () => {
    render(<ChangePasswordRow />);
    expect(screen.getByRole('button', { name: /change password/i })).toBeTruthy();
  });

  it('does not render password inputs until asked', () => {
    // An always-open password form in settings is something browsers offer to
    // autofill and people complete by accident.
    render(<ChangePasswordRow />);
    expect(screen.queryByLabelText(/current password/i)).toBeNull();
    open();
    expect(screen.getByLabelText(/current password/i)).toBeTruthy();
  });
});

describe('the current password is required', () => {
  it('passes BOTH the old and the new password to the service', () => {
    // This is the assertion the whole file exists for. If a refactor calls
    // supabase.auth.updateUser directly, `changePassword` stops being invoked
    // and this fails — which is the point, because the direct call needs only
    // a live session and would let a passer-by seize the account.
    render(<ChangePasswordRow />);
    open();
    fill('old-one', GOOD, GOOD);
    fireEvent.click(save());
    return waitFor(() =>
      expect(h.changePassword).toHaveBeenCalledWith('old-one', GOOD),
    );
  });

  it('never calls updateUser directly from the component', async () => {
    render(<ChangePasswordRow />);
    open();
    fill('old-one', GOOD, GOOD);
    fireEvent.click(save());
    await waitFor(() => expect(h.changePassword).toHaveBeenCalled());
    expect(h.updateUser).not.toHaveBeenCalled();
  });

  it('says the current password is wrong, not that the change failed', async () => {
    // "Couldn't change your password" for a typo sends someone to support for
    // something they can fix in three seconds.
    h.changePassword.mockRejectedValueOnce(new h.FakeCurrentPasswordWrong());
    render(<ChangePasswordRow />);
    open();
    fill('wrong', GOOD, GOOD);
    fireEvent.click(save());
    await screen.findByText(/current password is incorrect/i);
    expect(screen.queryByText(/couldn't change your password/i)).toBeNull();
  });
});

describe('what it refuses before spending a round trip', () => {
  it('catches a mismatched confirmation without re-authenticating', async () => {
    // A typo in the confirm box must not cost a verification of the current
    // password — checked locally, before the service is touched at all.
    render(<ChangePasswordRow />);
    open();
    fill('old-one', GOOD, `${GOOD}-typo`);
    fireEvent.click(save());
    await screen.findByText(/don't match/i);
    expect(h.changePassword).not.toHaveBeenCalled();
  });

  // The three requirement refusals go through the KEYBOARD, because Save is
  // disabled for all of them — see `pressEnter`. Each one also names the ONE
  // thing missing rather than listing every rule, so somebody fixing a
  // password is told the next step, not handed a specification.
  //
  // ⚠ Match the IMPERATIVE wording ("Use at least…", "Add one…"), not the
  // checklist's ("At least…", "One…"). The checklist is on screen at the same
  // time and says the overlapping thing, so a loose matcher finds two
  // elements and fails — which is the test telling you it cannot distinguish
  // the error banner from the hint that was already there before submit.
  it('refuses a password shorter than the server will accept', async () => {
    render(<ChangePasswordRow />);
    open();
    fill('old-one', 'Ab1', 'Ab1');
    pressEnter();
    await screen.findByText(/use at least 8 characters/i);
    expect(h.changePassword).not.toHaveBeenCalled();
  });

  it('refuses a long password with no capital letter', async () => {
    render(<ChangePasswordRow />);
    open();
    fill('old-one', 'brand-new-one1', 'brand-new-one1');
    pressEnter();
    await screen.findByText(/add one capital letter/i);
    expect(h.changePassword).not.toHaveBeenCalled();
  });

  it('refuses a long password with no number', async () => {
    render(<ChangePasswordRow />);
    open();
    fill('old-one', 'Brand-new-one', 'Brand-new-one');
    pressEnter();
    await screen.findByText(/add one number/i);
    expect(h.changePassword).not.toHaveBeenCalled();
  });

  it('refuses a "change" to the same password', async () => {
    render(<ChangePasswordRow />);
    open();
    fill(GOOD, GOOD, GOOD);
    fireEvent.click(save());
    await screen.findByText(/already have/i);
    expect(h.changePassword).not.toHaveBeenCalled();
  });

  it('keeps Save disabled until all three boxes are filled', () => {
    render(<ChangePasswordRow />);
    open();
    expect(save().hasAttribute('disabled')).toBe(true);
    fill('old-one', GOOD, GOOD);
    expect(save().hasAttribute('disabled')).toBe(false);
  });

  it('keeps Save disabled while the new password misses a requirement', () => {
    // Filled in but not acceptable — the box being non-empty is not the bar.
    render(<ChangePasswordRow />);
    open();
    fill('old-one', 'brand-new-one', 'brand-new-one');
    expect(save().hasAttribute('disabled')).toBe(true);
  });

  it('shows the checklist from the first keystroke, so the disabled button is explained', () => {
    // A disabled Save with nothing saying why is the version of this that
    // generates support tickets. The requirements are on screen as soon as
    // there is a password to judge, with each one's state readable without
    // relying on colour.
    render(<ChangePasswordRow />);
    open();
    fireEvent.change(field(/^new password$/i), { target: { value: 'brand' } });
    expect(screen.getByText(/at least 8 characters/i)).toBeTruthy();
    expect(screen.getByText(/one capital letter/i)).toBeTruthy();
    expect(screen.getByText(/one number/i)).toBeTruthy();
    expect(screen.getAllByText(/not yet met/i).length).toBe(3);
  });
});

describe('after it succeeds', () => {
  it('confirms, closes, and leaves nothing typed behind', async () => {
    render(<ChangePasswordRow />);
    open();
    fill('old-one', GOOD, GOOD);
    fireEvent.click(save());
    await waitFor(() => expect(h.toastSuccess).toHaveBeenCalled());
    // Collapsed again — and the fields are cleared, so re-opening does not
    // show the previous password sitting in a box on a shared screen.
    await waitFor(() =>
      expect(screen.queryByLabelText(/current password/i)).toBeNull(),
    );
    open();
    expect((field(/current password/i) as HTMLInputElement).value).toBe('');
  });

  it('keeps the form open on failure so the attempt is not lost', async () => {
    h.changePassword.mockRejectedValueOnce(new Error('network'));
    render(<ChangePasswordRow />);
    open();
    fill('old-one', GOOD, GOOD);
    fireEvent.click(save());
    await screen.findByText(/couldn't change your password/i);
    expect(screen.getByLabelText(/current password/i)).toBeTruthy();
  });
});
