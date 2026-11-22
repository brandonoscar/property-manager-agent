/**
 * What the app shows between "the tab opened" and "you are signed in".
 *
 * ⚠ **It used to be a bare 12px spinner on an empty canvas**, and it flashed
 * for whatever a session check took — often under 150ms, which is below the
 * threshold at which anything reads as a deliberate state. The effect is not
 * "fast", it is a blank white page followed by an app appearing: nobody
 * perceives a boot at all, so nothing tells them the product is doing work on
 * their behalf. Founder call, 2026-09-08: hold it long enough to be seen.
 *
 * ⚠ **The hold is a FLOOR, never a fixed delay.** `useMinimumVisible` starts
 * counting when the screen first renders and releases at the later of "auth
 * resolved" and `BOOT_MIN_MS`. A slow session check is never made slower, and
 * a fast one is not padded to some target — it is raised to the floor and
 * stops there.
 *
 * ⚠ **It cannot hold a screen that never appeared.** If auth is already
 * resolved on the first render the hook never arms, so a warm reload does not
 * gain 700ms of splash it did not previously have. That distinction is the
 * whole reason this is a hook and not a `setTimeout` in `App`.
 */

import { useEffect, useRef, useState } from 'react';
import { Wordmark } from './Wordmark';

/**
 * How long the boot screen stays up once it has appeared.
 *
 * ⚠ **Raised 700 → 1400ms (founder call, 2026-09-09): "the same as the bigger
 * companies".** 700ms cleared the flicker threshold and still passed as
 * incidental — a splash that is gone before you have focused on it reads as a
 * page loading, not as an application starting. The figure people are
 * describing when they say a product feels substantial is closer to a second
 * and a half: long enough to read the wordmark, short enough that nobody
 * waits for it.
 *
 * This is a taste decision on a number, not a measurement, and it is cheap to
 * change — one constant, and the tests read it rather than hardcoding.
 */
export const BOOT_MIN_MS = 1400;

/**
 * `true` while `active` is true, and for `minMs` after it first became true.
 *
 * Returns `false` forever if `active` was false on the first render — see the
 * warm-reload note above.
 */
export function useMinimumVisible(active: boolean, minMs: number): boolean {
  // Latches on the first `active` render and never re-arms. A second auth
  // check inside one page life (a token refresh, say) must not put the splash
  // back over a working app.
  const shownAt = useRef<number | null>(null);
  if (active && shownAt.current === null) shownAt.current = Date.now();

  const [held, setHeld] = useState(false);

  useEffect(() => {
    if (shownAt.current === null) return;
    const elapsed = Date.now() - shownAt.current;
    if (elapsed >= minMs) {
      setHeld(false);
      return;
    }
    setHeld(true);
    const t = setTimeout(() => setHeld(false), minMs - elapsed);
    return () => clearTimeout(t);
    // `active` is in the deps because the release timer must be (re)evaluated
    // when auth resolves — that is the moment `held` becomes the only thing
    // still keeping the screen up.
  }, [active, minMs]);

  return active || held;
}

export function BootScreen() {
  return (
    <div
      className="flex flex-col items-center justify-center h-screen gap-4 fade-in"
      style={{ background: 'var(--canvas)' }}
      data-testid="boot-screen"
      // A screen reader should hear one thing here, not three.
      role="status"
      aria-label="Loading Occupella"
    >
      {/* The wordmark as supplied. Lowercase is the mark (see Wordmark). */}
      <Wordmark size={28} />
      <span className="spinner" />
    </div>
  );
}
