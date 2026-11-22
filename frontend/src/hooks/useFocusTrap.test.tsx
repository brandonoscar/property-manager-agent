/**
 * The trap has to be exercised, not read.
 *
 * ⚠ A focus trap is invisible to static analysis in both directions: the source
 * of a working one and a broken one look identical, and the only way to tell is
 * to press Tab and see where focus lands (gotcha 71's rule — a behaviour whose
 * failure is silent needs a test that RUNS it). So every assertion here fires a
 * real `keydown` and reads `document.activeElement`.
 *
 * ⚠ jsdom does not implement layout, so `offsetParent` is null for everything
 * and the hook's visibility filter would reject every control. `getClientRects`
 * is the second half of that check and is stubbed here to report a box — which
 * is exactly the shape a real browser gives a visible element.
 */

import { useRef } from 'react';
import { render, cleanup } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it } from 'vitest';

import { useFocusTrap } from './useFocusTrap';

beforeAll(() => {
  // Make every element read as laid out. Without this the hook filters all
  // candidates away and the tests measure the stub, not the trap.
  Object.defineProperty(HTMLElement.prototype, 'getClientRects', {
    configurable: true,
    value() {
      return [{ width: 10, height: 10 }] as unknown as DOMRectList;
    },
  });
});

afterEach(cleanup);

/**
 * Press Tab and hand back the event.
 *
 * ⚠ The event is the point, not a convenience. jsdom does not move focus on
 * Tab, so "the browser was stopped from tabbing away" is invisible as a focus
 * change — `defaultPrevented` is the only observable form of it. A test that
 * only reads `activeElement` scores a hook that never calls `preventDefault`
 * as correct, which a mutation proved before this returned anything.
 */
function tab(shift = false): KeyboardEvent {
  const event = new KeyboardEvent('keydown', {
    key: 'Tab',
    shiftKey: shift,
    bubbles: true,
    cancelable: true,
  });
  document.dispatchEvent(event);
  return event;
}

function Dialog({ active = true, empty = false }: { active?: boolean; empty?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useFocusTrap(active, ref);
  return (
    <div>
      <button data-testid="outside">outside</button>
      <div ref={ref} role="dialog">
        {empty ? (
          <p>nothing to focus</p>
        ) : (
          <>
            <button data-testid="first">first</button>
            <input data-testid="middle" />
            <button data-testid="last">last</button>
          </>
        )}
      </div>
    </div>
  );
}

describe('useFocusTrap', () => {
  it('moves focus into the dialog when it opens', () => {
    const { getByTestId } = render(<Dialog />);
    expect(document.activeElement).toBe(getByTestId('first'));
  });

  it('wraps forward from the last control back to the first', () => {
    const { getByTestId } = render(<Dialog />);
    getByTestId('last').focus();
    tab();
    expect(document.activeElement).toBe(getByTestId('first'));
  });

  it('wraps backward from the first control to the last', () => {
    const { getByTestId } = render(<Dialog />);
    getByTestId('first').focus();
    tab(true);
    expect(document.activeElement).toBe(getByTestId('last'));
  });

  it('leaves Tab alone in the middle of the dialog', () => {
    // The trap must not fight the browser's own Tab handling — only the two
    // boundaries are its business. jsdom does not move focus on Tab, so the
    // observable property is that the hook did NOT redirect it.
    const { getByTestId } = render(<Dialog />);
    getByTestId('middle').focus();
    const event = tab();
    expect(event.defaultPrevented).toBe(false);
    expect(document.activeElement).toBe(getByTestId('middle'));
  });

  it('pulls focus back when it has escaped the dialog entirely', () => {
    // A backdrop click, or any stray programmatic focus, leaves the user one
    // Tab away from walking through the whole page behind the overlay.
    const { getByTestId } = render(<Dialog />);
    getByTestId('outside').focus();
    tab();
    expect(document.activeElement).toBe(getByTestId('first'));
  });

  // ⚠ There is deliberately no test that restores focus to a button INSIDE the
  // rendered tree. Unmounting takes that button with it, so the only assertion
  // available is `activeElement is not null` — which passes whether the
  // restore ran or not, and would read as coverage of the property it cannot
  // see. The opener below is created outside the tree for exactly that reason.

  it('gives focus back to whatever opened it', () => {
    const opener = document.createElement('button');
    document.body.appendChild(opener);
    opener.focus();
    expect(document.activeElement).toBe(opener);

    const { unmount } = render(<Dialog />);
    expect(document.activeElement).not.toBe(opener);
    unmount();
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });

  it('does nothing at all when inactive', () => {
    const { getByTestId } = render(<Dialog active={false} />);
    const outside = getByTestId('outside');
    outside.focus();
    tab();
    expect(document.activeElement).toBe(outside);
  });

  it('holds focus in a dialog with no focusable controls', () => {
    // Otherwise Tab escapes an "empty" dialog on the very first press — the
    // case a trap written only for the happy path misses.
    const { getByTestId } = render(<Dialog empty />);
    const dialog = getByTestId('outside').parentElement!.querySelector('[role="dialog"]');
    expect(document.activeElement).toBe(dialog);
    // `defaultPrevented` is the assertion that bites: jsdom would leave focus
    // on the dialog either way, so reading `activeElement` alone cannot tell a
    // held Tab from an escaped one.
    expect(tab().defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(dialog);
  });
});
