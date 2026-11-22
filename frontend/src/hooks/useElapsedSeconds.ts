/**
 * Seconds since `active` became true. Zero whenever it is false.
 *
 * ⚠ Why a turn needs a clock on screen at all: the measured p50 for a chat
 * turn is ~40s and the tail runs into minutes (gotcha 61). At that length a
 * status line saying what it is doing still cannot tell the reader whether it
 * is SLOW or STUCK — the words are identical either way, and a stuck run is
 * the one they need to act on. A number that keeps moving answers that
 * without anyone having to guess.
 *
 * ⚠ Deliberately NOT `useStillLoading`. That one crosses a threshold once and
 * latches, which is right for swapping a skeleton and useless here: the whole
 * value is that the digits keep changing, because a frozen counter is exactly
 * the signal that something died.
 */

import { useEffect, useRef, useState } from 'react';

export function useElapsedSeconds(active: boolean): number {
  const [seconds, setSeconds] = useState(0);
  const startedAt = useRef<number | null>(null);

  useEffect(() => {
    if (!active) {
      startedAt.current = null;
      setSeconds(0);
      return;
    }
    // Anchor on the FIRST render of this active window, not on each tick, so
    // a re-render mid-turn cannot restart the count.
    if (startedAt.current === null) startedAt.current = Date.now();
    const tick = () => {
      const from = startedAt.current;
      if (from !== null) setSeconds(Math.floor((Date.now() - from) / 1000));
    };
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [active]);

  return seconds;
}
