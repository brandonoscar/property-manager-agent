/**
 * A 402 is a DECISION, not a crash — and it lands at the moment a trial
 * converts.
 *
 * `billing/gate.py` returns three distinct blocks (trial_expired,
 * subscription_canceled, turns_exhausted). Each is written for the user, each
 * names Settings → Billing, and each needs a DIFFERENT action from them —
 * "pick a plan" and "add seats or move up a plan" are not the same
 * instruction. `agent.py` raises them as HTTP 402, deliberately distinguishable
 * from the 429 above it so the client can route to Billing rather than telling
 * someone to wait for midnight.
 *
 * The client threw that away. `runAgent` passed 429 and 409 through untouched
 * and prefixed EVERYTHING else with "Agent request failed:", so a paywall
 * rendered as an outage and the one sentence that unblocks the customer was
 * buried behind a crash framing. `grep 402` across the whole frontend —
 * services, stores, components, hooks — matched nothing but unit ids and
 * dollar amounts.
 *
 * ⚠ Both directions (gotcha 38). Passing EVERY status through unprefixed would
 * strip the framing from real failures, where "Agent request failed:" is
 * exactly right and its absence would read as the agent's own words.
 */

import { describe, expect, it } from 'vitest';

import { agentErrorMessage } from './agent';

/** The real texts, copied from billing/gate.py rather than invented — a
 *  fixture drifted from the thing it stands for is fiction (gotcha 41). */
const TRIAL_EXPIRED =
  'Your free trial has ended. Pick a plan in Settings → Billing to carry on — ' +
  'everything Occupella has already synced is still here.';
const SUBSCRIPTION_ENDED =
  'Your Occupella subscription has ended. Start it again in Settings → Billing ' +
  'and your data is exactly where you left it.';
const TURNS_EXHAUSTED =
  "You've used all 400 questions on the Starter plan this period. Settings → " +
  'Billing can add seats or move you up a plan.';

describe('a billing block reaches the user as written', () => {
  it.each([
    ['trial expired', TRIAL_EXPIRED],
    ['subscription ended', SUBSCRIPTION_ENDED],
    ['turns exhausted', TURNS_EXHAUSTED],
  ])('passes the 402 %s message through verbatim', (_label, text) => {
    expect(agentErrorMessage(402, text)).toBe(text);
  });

  it('keeps the route to Billing intact', () => {
    // The load-bearing half. Whatever the framing does, the sentence telling
    // the customer where to go must survive to the screen.
    expect(agentErrorMessage(402, TRIAL_EXPIRED)).toContain('Settings → Billing');
  });

  it('does not reframe the paywall as a crash', () => {
    expect(agentErrorMessage(402, TRIAL_EXPIRED)).not.toContain('Agent request failed');
  });
});

describe('the other direction', () => {
  it('still prefixes a genuine failure', () => {
    // A 500 IS a failure. Stripping the prefix would put the backend's error
    // text in the agent's own mouth.
    expect(agentErrorMessage(500, 'Internal Server Error')).toBe(
      'Agent request failed: Internal Server Error',
    );
  });

  it('still prefixes a 400 and a 404', () => {
    expect(agentErrorMessage(400, 'Bad Request')).toContain('Agent request failed');
    expect(agentErrorMessage(404, 'Not Found')).toContain('Agent request failed');
  });

  it('leaves the 429 and 409 passthroughs alone', () => {
    // These predate the 402 and are what the branch was built for; widening it
    // must not disturb them.
    const budget = 'Daily budget reached — try again after midnight.';
    const busy = 'A run is already active in this thread.';
    expect(agentErrorMessage(429, budget)).toBe(budget);
    expect(agentErrorMessage(409, busy)).toBe(busy);
  });
});

describe('the call site still uses it', () => {
  it('runAgent phrases its ApiError through agentErrorMessage', async () => {
    // ⚠ A pure function that is correct in isolation says nothing about a
    // caller that ignores it — gotcha 62's rule, and the shape that let four
    // route-level mutations through in gotcha 69. The behavioural test above
    // cannot see this, so read the call site.
    // `?raw` — the same mechanism ChatThread.test.tsx already uses to read a
    // sibling module's source, rather than inventing a second one.
    const source = (await import('./agent?raw')).default as string;
    expect(source).toContain('agentErrorMessage(err.status, err.message)');
    // And the framing must be BUILT in exactly one place, so a second copy
    // cannot drift away from the pass-through set.
    //
    // ⚠ Anchored on the template-literal CONSTRUCT, not the bare phrase.
    // A plain substring count reads 2, because the helper's own doc comment
    // quotes the phrase in order to explain it — gotcha 40/59's trap, which
    // this guard walked straight into on its first run.
    const built = source.match(/`Agent request failed: \$\{/g) ?? [];
    expect(built).toHaveLength(1);
  });
});
