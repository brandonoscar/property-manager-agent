/**
 * The wire field the panel branches on has to survive the client mapping.
 *
 * The backend emits `stateUnknown` on every connector row (pinned by
 * `tests/unit/test_connectors_state_unknown.py`), and `ComposioOAuthDetail`
 * reads `connector.stateUnknown`. Nothing compared the two sides, so dropping
 * one line from `enrichBackendConnector` would leave both halves individually
 * correct and the whole feature silently dead — gotcha 28's both-sides rule,
 * and the failure mode is the quiet one: an absent key is falsy, so every
 * deployment reads as healthy and the customer is told to click Connect again.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('./api', () => ({ apiFetch: vi.fn() }));
vi.mock('./auth', () => ({ demoMode: false }));

import { apiFetch } from './api';
import { listConnectors } from './connectors';

const fetchMock = vi.mocked(apiFetch);

function serves(rows: unknown[]) {
  fetchMock.mockResolvedValue({ json: async () => rows } as unknown as Response);
}

function wireRow(overrides: Record<string, unknown> = {}) {
  return {
    id: 'gmail',
    name: 'Gmail',
    category: 'communication',
    description: 'Send and reply to tenant emails.',
    authMethod: 'oauth',
    isConnected: false,
    needsReauth: false,
    stateUnknown: false,
    ...overrides,
  };
}

afterEach(() => {
  vi.clearAllMocks();
});

describe('listConnectors — stateUnknown crosses the wire', () => {
  it('carries a true flag through to the rendered connector', async () => {
    serves([wireRow({ stateUnknown: true })]);
    const [row] = await listConnectors();
    expect(row.stateUnknown).toBe(true);
  });

  it('leaves a healthy row alone', async () => {
    // THE NEGATIVE (gotchas 38/51). A mapping that hardcoded `true` passes the
    // test above and claims every deployment is broken.
    serves([wireRow()]);
    const [row] = await listConnectors();
    expect(row.stateUnknown).toBe(false);
  });

  it('reads an ABSENT key as known, not unknown', async () => {
    // An older backend does not send the field at all. Treating that as
    // "unreadable" would put the unavailable notice in front of every
    // customer during a deploy window — a caveat that fires when it is not
    // true trains its reader to ignore the ones that are (gotchas 57/93).
    const { stateUnknown: _dropped, ...noField } = wireRow();
    serves([noField]);
    const [row] = await listConnectors();
    expect(row.stateUnknown).toBe(false);
  });
});
