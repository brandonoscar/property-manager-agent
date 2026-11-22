/**
 * Reveal streamed text progressively, so an answer types itself out.
 *
 * ⚠ **This is the right layer, and deliberately not the backend.**
 * `StreamScrubber` buffers deltas and publishes only at boundaries an
 * identifier cannot straddle — a newline, or a sentence terminator. That is a
 * safety property (a record number split across two publishes is unredactable,
 * and two cleverer designs shipped exactly that corruption), so the backend
 * cannot emit per token no matter how much nicer it would look. Its own
 * docstring says where the fix belongs: *"the typing feel belongs to the
 * frontend, which can animate whatever it receives."* This is that.
 *
 * The backend now sends sentence-sized chunks instead of paragraph-sized ones,
 * so what arrives is already close enough that a short catch-up reads as
 * typing rather than as a progress bar.
 *
 * ⚠ **Catches up in proportion to the backlog, never at a fixed rate.** A
 * fixed characters-per-tick is the obvious version and it is wrong in both
 * directions: too slow and a 200-character chunk takes seconds to appear while
 * the next one is already queued behind it, too fast and it is not an
 * animation. Draining a FRACTION of whatever is outstanding self-corrects —
 * a big chunk catches up quickly, a trickle stays smooth, and the reveal can
 * never fall permanently behind the stream.
 */

import { useEffect, useRef, useState } from 'react';

/** How much of the outstanding backlog to reveal per tick. */
const _CATCH_UP_FRACTION = 0.18;
/** Never reveal fewer than this per tick, or a 1-character backlog stalls. */
const _MIN_CHARS_PER_TICK = 2;
const _TICK_MS = 16;

function prefersReducedMotion(): boolean {
  // ⚠ jsdom DOES implement matchMedia and answers `matches: true` for this
  // query — measured, not assumed. So a test that does not stub it exercises
  // the reduced-motion branch while appearing to test the animation, and reads
  // as "the typewriter reveals everything at once". Every test in
  // useTypewriter.test.ts states the preference it means; the try/catch is for
  // an environment with no matchMedia at all, where the fallback is "animate"
  // because that is the normal case.
  try {
    return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
  } catch {
    return false;
  }
}

export function useTypewriter(content: string, streaming: boolean): string {
  // ⚠ A streaming turn starts from nothing even if it mounts with text
  // already in hand. Initialising to `content.length` unconditionally reads
  // fine in the normal case — the bubble mounts empty and grows — and skips
  // the animation entirely whenever a turn arrives with content on its first
  // render, which is exactly what a re-attached stream does (agent.ts replays
  // from a cursor after a dropped connection).
  const [revealed, setRevealed] = useState(streaming ? 0 : content.length);
  const revealedRef = useRef(revealed);
  revealedRef.current = revealed;

  useEffect(() => {
    // ⚠ A settled turn shows everything, immediately. Re-reading history must
    // not replay an animation, and a turn that has stopped streaming has no
    // reason to hold text back.
    if (!streaming || prefersReducedMotion()) {
      setRevealed(content.length);
      return;
    }
    // A shorter string means a different turn is reusing this hook — start
    // from the top rather than showing nothing until the new text overtakes
    // the old length.
    if (revealedRef.current > content.length) {
      setRevealed(content.length);
      return;
    }
    if (revealedRef.current >= content.length) return;

    const id = window.setInterval(() => {
      setRevealed((prev) => {
        if (prev >= content.length) return prev;
        const backlog = content.length - prev;
        const step = Math.max(_MIN_CHARS_PER_TICK, Math.ceil(backlog * _CATCH_UP_FRACTION));
        return Math.min(content.length, prev + step);
      });
    }, _TICK_MS);
    return () => window.clearInterval(id);
  }, [content, streaming]);

  // ⚠ Slice on the CONTENT, never keep a copy. Holding the revealed substring
  // in state would let it drift from the source when a turn is re-rendered
  // with corrected text, and a stale answer is worse than an abrupt one.
  // No clamp: `slice` already clamps an end past the length, and a `Math.min`
  // here reads as a safety check while being provably a no-op for every input.
  return content.slice(0, revealed);
}
