/**
 * Keep keyboard focus inside an open modal, and give it back when it closes.
 *
 * ⚠ **Without this a modal is only visually modal.** The card covers the page
 * and Tab walks straight out of it into the sidebar, the composer and every
 * link behind the overlay — which are still focusable, still activatable, and
 * completely invisible to somebody navigating by keyboard or reading with a
 * screen reader. They tab into a form they cannot see and cannot get back to
 * the dialog. `aria-modal` is a promise to that user that the rest of the page
 * is inert; four of the five dialogs here made that promise and none kept it.
 *
 * ⚠ **Returning focus is the half people skip, and it is the one you feel.**
 * When a dialog closes, focus lands on `document.body` unless something puts it
 * back — so the next Tab restarts at the top of the page, and a screen reader
 * announces nothing at all. Opening a dialog and closing it should leave you
 * exactly where you were.
 *
 * The one dialog that does NOT need this is `CommandPalette`, which is built on
 * `@radix-ui/react-dialog` and traps focus itself. Wrapping it again would be
 * two traps fighting over the same Tab.
 */

import { useEffect, type RefObject } from 'react';

/**
 * Anything a browser will focus with Tab. `[tabindex="-1"]` is deliberately
 * excluded: it means "focusable by script, not by Tab", so including it would
 * put stops in the cycle a real Tab never visits — which reads as the trap
 * being broken.
 */
const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

function focusable(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    // A control inside a collapsed section is in the DOM and cannot be
    // reached, so tabbing to it would strand the user on nothing.
    (el) => el.offsetParent !== null || el.getClientRects().length > 0,
  );
}

/**
 * @param active  whether the dialog is open. False is a no-op, so a component
 *                that renders its dialog conditionally can call this
 *                unconditionally.
 * @param ref     the dialog container. Everything focusable inside it is in
 *                the cycle.
 */
export function useFocusTrap(active: boolean, ref: RefObject<HTMLElement | null>): void {
  useEffect(() => {
    if (!active) return;
    const container = ref.current;
    if (!container) return;

    // Remember who opened it BEFORE moving focus, or we hand focus back to
    // the dialog's own first field after the dialog is gone.
    const opener = document.activeElement as HTMLElement | null;

    // Some dialogs already `autoFocus` a field. Only take over when nothing
    // inside has focus yet — stealing it would move the caret out of the one
    // input the author deliberately pointed at.
    if (!container.contains(document.activeElement)) {
      const first = focusable(container)[0];
      if (first) first.focus();
      else {
        // A dialog with no controls at all still has to receive focus, or the
        // screen reader stays on the page behind it.
        container.tabIndex = -1;
        container.focus();
      }
    }

    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Tab') return;
      const stops = focusable(container);
      if (stops.length === 0) {
        // Nothing to move to — hold focus rather than letting Tab escape.
        e.preventDefault();
        return;
      }
      const first = stops[0];
      const last = stops[stops.length - 1];
      const current = document.activeElement;

      // Focus outside the dialog entirely (a click on the backdrop, a stray
      // programmatic focus) — pull it back rather than letting Tab continue
      // through the page behind.
      if (!container.contains(current)) {
        e.preventDefault();
        first.focus();
        return;
      }
      if (e.shiftKey && current === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && current === last) {
        e.preventDefault();
        first.focus();
      }
    };

    // Capture phase: a field inside the dialog may stop propagation of its own
    // key handling, and the trap has to see Tab regardless.
    document.addEventListener('keydown', onKeyDown, true);
    return () => {
      document.removeEventListener('keydown', onKeyDown, true);
      // Only restore if the opener is still on the page — a dialog opened from
      // a row that the dialog then deleted has nowhere to go back to, and
      // focusing a detached node throws focus to body anyway.
      if (opener && document.contains(opener)) opener.focus();
    };
  }, [active, ref]);
}
