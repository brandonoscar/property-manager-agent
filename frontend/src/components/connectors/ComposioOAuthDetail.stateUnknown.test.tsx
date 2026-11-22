/**
 * "We could not read your connection state" must not render as "you never
 * connected this".
 *
 * The backend sets `stateUnknown` when Composio is unconfigured on the
 * deployment or the connection lookup raised. Both used to arrive as
 * `isConnected: false` — byte-identical to a customer who genuinely has not
 * set the connector up — so the panel offered a Connect button whose route
 * answers 503, and the agent told them the same false story in chat.
 *
 * ⚠ The load-bearing test is the NEGATIVE one: a panel that always claimed the
 * integration was unavailable would satisfy every positive assertion here and
 * tell every new customer their setup was unfixable (gotchas 38/51).
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { ComposioOAuthDetail } from './ComposioOAuthDetail';
import type { Connector } from '../../services/connectors';

vi.mock('../../services/connectors', async () => {
  const actual =
    await vi.importActual<typeof import('../../services/connectors')>(
      '../../services/connectors',
    );
  return {
    ...actual,
    // Network only — the pure helpers stay real, or every assertion below
    // would be checking a stub's opinion (gotcha 88).
    initiateConnection: vi.fn(),
    disconnectConnector: vi.fn(),
    listConnectors: vi.fn(async () => []),
  };
});

function connector(overrides: Partial<Connector> = {}): Connector {
  return {
    id: 'gmail',
    name: 'Gmail',
    category: 'communication',
    description: 'Read and draft mail.',
    capabilities: ['api'],
    authMethod: 'oauth',
    apiConnected: false,
    needsReauth: false,
    stateUnknown: false,
    ...overrides,
  };
}

function panel(overrides: Partial<Connector> = {}) {
  return render(
    <ComposioOAuthDetail
      connector={connector(overrides)}
      onClose={() => {}}
      onChange={() => {}}
    />,
  );
}

describe('ComposioOAuthDetail — the state we could not read', () => {
  it('says the problem is ours and names no action the user can take', () => {
    panel({ stateUnknown: true });
    expect(screen.getByText(/setup problem on our side/i)).toBeTruthy();
    expect(screen.getByText(/nothing for you to change/i)).toBeTruthy();
  });

  it('disables the connect button rather than inviting a 503', () => {
    panel({ stateUnknown: true });
    const button = screen.getByTestId('composio-connect-gmail');
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(button.textContent).toMatch(/unavailable/i);
  });

  it('does NOT tell an unreadable connector to reconnect', () => {
    // Precedence: with the integration unreachable the lapse probe never ran,
    // so needsReauth cannot be trusted — and "reconnect" is the one
    // instruction guaranteed not to help here.
    panel({ stateUnknown: true, needsReauth: true });
    expect(screen.queryByText(/This connection expired and stopped syncing/i)).toBeNull();
    expect(screen.getByTestId('composio-connect-gmail').textContent).toMatch(
      /unavailable/i,
    );
  });

  it('leaves a genuinely-unconnected connector offering Connect', () => {
    // THE NEGATIVE. A version that always claimed unavailability passes every
    // test above and breaks the ordinary first-run path for everybody.
    panel();
    const button = screen.getByTestId('composio-connect-gmail');
    expect((button as HTMLButtonElement).disabled).toBe(false);
    expect(button.textContent).toMatch(/connect with/i);
    expect(screen.queryByText(/setup problem on our side/i)).toBeNull();
  });

  it('leaves a lapsed grant offering Reconnect', () => {
    panel({ needsReauth: true });
    expect(screen.getByText(/This connection expired and stopped syncing/i)).toBeTruthy();
    expect(screen.getByTestId('composio-connect-gmail').textContent).toMatch(
      /reconnect with/i,
    );
  });
});
