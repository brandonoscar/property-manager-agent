/**
 * The one decision both connector surfaces render.
 *
 * The Settings list row and the OAuth detail panel each used to derive
 * "connected / lapsed / unreadable" from the same three wire fields by hand —
 * so `needsReauth` was added twice and `stateUnknown` would have been too. The
 * list row is a function nested inside a 3,000-line page component, which
 * makes it effectively untestable by rendering; extracting the decision is
 * what makes the precedence assertable at all.
 *
 * The load-bearing cases are the last two (gotchas 38/51): a working row must
 * wear no warning, and a genuinely-lapsed grant must still say Reconnect — a
 * helper that returned `stateUnknown: true` for everything passes every other
 * assertion here and tells every customer their setup is unfixable.
 */

import { describe, expect, it } from 'vitest';

import { connectorRowState, type Connector } from './connectors';

function connector(overrides: Partial<Connector> = {}): Connector {
  return {
    id: 'gmail',
    name: 'Gmail',
    category: 'communication',
    description: '',
    capabilities: ['api'],
    authMethod: 'oauth',
    apiConnected: false,
    needsReauth: false,
    stateUnknown: false,
    ...overrides,
  };
}

describe('connectorRowState', () => {
  it('an unreadable state outranks a lapsed grant', () => {
    // THE PRECEDENCE. With the integration unreachable the degraded-toolkit
    // probe never ran, so `needsReauth` is untrustworthy — and "reconnect" is
    // the one instruction guaranteed not to help (gotchas 102/106).
    const state = connectorRowState(
      connector({ needsReauth: true, stateUnknown: true }),
    );
    expect(state.stateUnknown).toBe(true);
    expect(state.needsReauth).toBe(false);
  });

  it('an active connection wears no warning at all', () => {
    const state = connectorRowState(
      connector({ apiConnected: true, needsReauth: true, stateUnknown: true }),
    );
    expect(state).toEqual({
      isConnected: true,
      needsReauth: false,
      stateUnknown: false,
    });
  });

  it('the webhook row has no OAuth state to read', () => {
    // A webhook row carries no Composio grant, so neither derived flag can
    // mean anything there — claiming otherwise would put a badge on a row
    // whose state we never looked up.
    const state = connectorRowState(
      connector({ needsReauth: true, stateUnknown: true }),
      'webhook',
    );
    expect(state).toEqual({
      isConnected: false,
      needsReauth: false,
      stateUnknown: false,
    });
  });

  it('a lapsed grant on a reachable deployment still says reconnect', () => {
    const state = connectorRowState(connector({ needsReauth: true }));
    expect(state.needsReauth).toBe(true);
    expect(state.stateUnknown).toBe(false);
  });

  it('a never-connected row on a reachable deployment claims nothing', () => {
    // THE NEGATIVE. The ordinary first-run path: click Connect, and it works.
    expect(connectorRowState(connector())).toEqual({
      isConnected: false,
      needsReauth: false,
      stateUnknown: false,
    });
  });
});
