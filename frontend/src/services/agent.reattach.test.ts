/**
 * A dropped stream must be re-attached, and the retry budget must be REAL.
 *
 * Live probe, 2026-08-26: a turn returned `"OCCUPELLA\n\nFailed to fetch"` at
 * 201.8s while the backend ran happily on to completion — and the next send
 * then hit a session that was still busy, so one blip cost two turns.
 *
 * The re-attach machinery already existed. Two defects made it useless:
 *
 *   1. A failure to CONNECT surfaced immediately with no retry. runAgent's
 *      POST stream dies on a blip and re-attaches a millisecond later over the
 *      SAME sick connection — the blip that killed the POST is exactly the one
 *      most likely to kill the first re-attach.
 *   2. `attempt` was a fresh `let attempt = 0` in every invocation, and the
 *      retry recursed into a NEW watchSession. So the budget reset every time
 *      and `WATCH_RETRY_DELAYS_MS` could never be exhausted — a backend that
 *      was genuinely down got an unbounded 500ms hot loop, the exact opposite
 *      of the "deliberately short" bound its own comment promises.
 *
 * They interact: fixing (1) without (2) turns a dead backend into an infinite
 * reconnect. Both are pinned here.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./api', () => ({
  apiFetch: vi.fn(),
  ApiError: class extends Error {
    constructor(
      public status: number,
      msg: string,
    ) {
      super(msg);
    }
  },
  ApiAuthError: class extends Error {},
}));
vi.mock('./auth', () => ({ demoMode: false }));
vi.mock('./analytics', () => ({
  captureConfirmation: vi.fn(),
  captureTaskCompleted: vi.fn(),
  captureTaskFailed: vi.fn(),
  captureTaskSubmitted: vi.fn(),
}));

import { apiFetch } from './api';
import { watchSession } from './agent';

const apiFetchMock = apiFetch as unknown as ReturnType<typeof vi.fn>;

/** What a mid-turn network drop actually looks like in the browser. */
const failedToFetch = () => new TypeError('Failed to fetch');

beforeEach(() => {
  apiFetchMock.mockReset();
  vi.useFakeTimers();
});
afterEach(() => {
  vi.useRealTimers();
});

/** Drain every pending retry timer so the budget runs to its end. */
async function drainRetries(): Promise<void> {
  for (let i = 0; i < 12; i += 1) {
    await vi.advanceTimersByTimeAsync(5000);
  }
}

describe('watchSession re-attach', () => {
  it('retries a failure to CONNECT instead of surfacing it at once', async () => {
    // The regression: one shot, then "Failed to fetch" in front of the user.
    apiFetchMock.mockRejectedValue(failedToFetch());
    const onError = vi.fn();

    await watchSession({
      sessionId: 's1',
      onEvent: vi.fn(),
      onDone: vi.fn(),
      onError,
    });

    expect(apiFetchMock).toHaveBeenCalledTimes(1);
    expect(onError).not.toHaveBeenCalled(); // ← the whole point: not yet

    await drainRetries();
    expect(apiFetchMock.mock.calls.length).toBeGreaterThan(1);
  });

  it('gives up eventually — the budget is not reset by the recursion', async () => {
    // The other half. Without threading `attempt` this never terminates, so a
    // dead backend becomes an unbounded 500ms hot loop that never tells the
    // user anything.
    apiFetchMock.mockRejectedValue(failedToFetch());
    const onError = vi.fn();

    await watchSession({
      sessionId: 's1',
      onEvent: vi.fn(),
      onDone: vi.fn(),
      onError,
    });
    await drainRetries();

    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][0].message).toContain('Failed to fetch');
    // 1 initial + exactly the configured delays. If this grows without bound
    // the budget has stopped binding again.
    expect(apiFetchMock).toHaveBeenCalledTimes(4);
  });

  it('resumes from the cursor rather than replaying the turn', async () => {
    apiFetchMock.mockRejectedValue(failedToFetch());
    await watchSession({
      sessionId: 's1',
      onEvent: vi.fn(),
      onDone: vi.fn(),
      onError: vi.fn(),
      after: 42,
    });
    await drainRetries();

    // Every attempt carries the cursor — a re-attach that replays from event
    // zero renders duplicated output, which is what makes a naive reconnect
    // look broken.
    for (const call of apiFetchMock.mock.calls) {
      expect(String(call[0])).toContain('after=42');
    }
  });

  it('never retries a deliberate abort', async () => {
    // Navigation, unmount, or a superseding run. Retrying those would resurrect
    // a stream the caller deliberately killed.
    const controller = new AbortController();
    controller.abort();
    apiFetchMock.mockRejectedValue(failedToFetch());
    const onError = vi.fn();

    await watchSession({
      sessionId: 's1',
      onEvent: vi.fn(),
      onDone: vi.fn(),
      onError,
      controller,
    });
    await drainRetries();

    expect(apiFetchMock).toHaveBeenCalledTimes(1);
    expect(onError).not.toHaveBeenCalled();
  });
});
