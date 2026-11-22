/**
 * PropertywareApiDetail — the Settings → Connectors panel for Propertyware.
 *
 * Pins the flow Rentvine's panel has (save, then test; sync now; disconnect
 * after a confirm), that every refusal is the server's own sentence (gotcha
 * 137), that the switched-off state shows before anyone types keys, and that
 * the copy names an action and never another system. All values are synthetic.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

// ⚠ Mock the NETWORK calls only; keep every constant real (gotcha 88).
vi.mock('../../services/propertyware', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../services/propertyware')>()),
  getPropertywareStatus: vi.fn(),
  savePropertywareCreds: vi.fn(),
  testPropertywareConnection: vi.fn(),
  deletePropertywareCreds: vi.fn(),
  runPropertywareSync: vi.fn(),
}));

import { ApiError } from '../../services/api';
import {
  PROPERTYWARE_READ_PERMISSIONS,
  deletePropertywareCreds,
  getPropertywareStatus,
  runPropertywareSync,
  savePropertywareCreds,
  testPropertywareConnection,
  type PropertywareStatus,
} from '../../services/propertyware';
import { PropertywareApiDetail, syncSummary } from './PropertywareApiDetail';

const statusMock = vi.mocked(getPropertywareStatus);
const saveMock = vi.mocked(savePropertywareCreds);
const testMock = vi.mocked(testPropertywareConnection);
const deleteMock = vi.mocked(deletePropertywareCreds);
const syncMock = vi.mocked(runPropertywareSync);

const NOT_CONNECTED: PropertywareStatus = {
  configured: false,
  system_id: null,
  client_id_last4: null,
  last_tested_at: null,
  connector_enabled: true,
};

const CONNECTED: PropertywareStatus = {
  configured: true,
  system_id: '990000001',
  client_id_last4: '0001',
  last_tested_at: new Date(Date.now() - 2 * 3_600_000).toISOString(),
  connector_enabled: true,
};

/** The other systems the copy must never name (the panel is Propertyware's). */
const OTHER_SYSTEMS = /buildium|rentvine|rent manager|appfolio|yardi/i;

async function renderPanel(status: PropertywareStatus) {
  statusMock.mockResolvedValue(status);
  const onChange = vi.fn();
  render(<PropertywareApiDetail onClose={vi.fn()} onChange={onChange} />);
  await waitFor(() => expect(screen.getByLabelText('Client ID')).toBeInTheDocument());
  return { onChange, user: userEvent.setup() };
}

async function fillKeys(user: ReturnType<typeof userEvent.setup>, { systemId = true } = {}) {
  if (systemId) await user.type(screen.getByLabelText('Organization ID'), ' 990000001 ');
  await user.type(screen.getByLabelText('Client ID'), 'pw-client-id-0001');
  await user.type(screen.getByLabelText('Client secret'), 'pw-secret-12345678');
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('PropertywareApiDetail', () => {
  it('saves all three values, then tests, and says the portfolio is being read', async () => {
    saveMock.mockResolvedValue({ ...CONNECTED, last_tested_at: null });
    testMock.mockResolvedValue({ ok: true, properties_count: 1, error: null });
    const { onChange, user } = await renderPanel(NOT_CONNECTED);

    await fillKeys(user);
    await user.click(screen.getByTestId('propertyware-save'));

    await waitFor(() => expect(testMock).toHaveBeenCalledTimes(1));
    expect(saveMock).toHaveBeenCalledWith({
      system_id: '990000001',
      client_id: 'pw-client-id-0001',
      client_secret: 'pw-secret-12345678',
    });
    // Saved FIRST: the test reads the keys the save stored.
    expect(saveMock.mock.invocationCallOrder[0]).toBeLessThan(testMock.mock.invocationCallOrder[0]);
    const result = await screen.findByTestId('propertyware-result');
    // "1 buildings" would be the first sentence a one-building customer reads.
    expect(result.textContent).toContain('Propertyware accepted the keys · 1 building.');
    expect(result.textContent).toContain('Occupella has started reading your portfolio.');
    expect(onChange).toHaveBeenCalled();
    // Neither key lingers in the form.
    expect((screen.getByLabelText('Client secret') as HTMLInputElement).value).toBe('');
    expect((screen.getByLabelText('Client ID') as HTMLInputElement).value).toBe('');
  });

  it('claims no count Propertyware did not state', async () => {
    saveMock.mockResolvedValue({ ...CONNECTED, last_tested_at: null });
    testMock.mockResolvedValue({ ok: true, properties_count: null, error: null });
    const { user } = await renderPanel(NOT_CONNECTED);
    await fillKeys(user);
    await user.click(screen.getByTestId('propertyware-save'));
    // No count stated means none is claimed (gotcha 26).
    expect((await screen.findByTestId('propertyware-result')).textContent).toBe(
      'Propertyware accepted the keys. Occupella has started reading your portfolio.dismiss',
    );
  });

  it('waits for all three values before it will save', async () => {
    const { user } = await renderPanel(NOT_CONNECTED);
    expect(screen.getByTestId('propertyware-save')).toBeDisabled();
    await fillKeys(user, { systemId: false });
    expect(screen.getByTestId('propertyware-save')).toBeDisabled();
    await user.type(screen.getByLabelText('Organization ID'), '990000001');
    expect(screen.getByTestId('propertyware-save')).toBeEnabled();
  });

  it('fills in the saved organization ID, so replacing a key needs only the key', async () => {
    saveMock.mockResolvedValue({ ...CONNECTED, last_tested_at: null });
    testMock.mockResolvedValue({ ok: true, properties_count: 3, error: null });
    const { user } = await renderPanel(CONNECTED);
    expect((screen.getByLabelText('Organization ID') as HTMLInputElement).value).toBe('990000001');
    expect(screen.getByTestId('propertyware-save')).toHaveTextContent('Replace keys');

    await fillKeys(user, { systemId: false });
    await user.click(screen.getByTestId('propertyware-save'));
    await waitFor(() =>
      expect(saveMock).toHaveBeenCalledWith(expect.objectContaining({ system_id: '990000001' })),
    );
  });

  it.each([
    [422, "That organization ID can't be used. Paste only your Propertyware organization ID, on one line, and save again."],
    [409, 'This workspace is already connected to another property-management system. Disconnect it first, then connect Propertyware.'],
    [503, 'The Propertyware connector is switched off on this Occupella deployment, so keys can’t be saved.'],
  ])('shows a %i refusal of the save in the server’s words, and tests nothing', async (code, sentence) => {
    saveMock.mockRejectedValueOnce(new ApiError(code, sentence));
    const { user } = await renderPanel(NOT_CONNECTED);
    await fillKeys(user);
    await user.click(screen.getByTestId('propertyware-save'));

    const result = await screen.findByTestId('propertyware-result');
    expect(result.textContent).toContain(sentence);
    expect(result.getAttribute('role')).toBe('alert');
    expect(testMock).not.toHaveBeenCalled();
  });

  it("shows Propertyware's refusal as the server wrote it", async () => {
    const advice =
      'Propertyware refused to let this API key read buildings. The API key lacks BUILDINGS - Read.';
    testMock.mockResolvedValue({ ok: false, properties_count: null, error: advice });
    const { user } = await renderPanel(CONNECTED);

    await user.click(screen.getByTestId('propertyware-test'));

    const result = await screen.findByTestId('propertyware-result');
    expect(result.textContent).toContain(advice);
    expect(result.getAttribute('role')).toBe('alert');
  });

  it('shows what is saved without any secret', async () => {
    await renderPanel(CONNECTED);
    const summary = screen.getByTestId('propertyware-connection-summary');
    expect(summary.textContent).toContain('Organization ID: 990000001');
    expect(summary.textContent).toContain('••••0001');
    expect(summary.textContent).toMatch(/Last passed a test 2h ago/);
    expect(screen.getByLabelText('Client secret')).toHaveAttribute('type', 'password');
    expect((screen.getByLabelText('Client secret') as HTMLInputElement).value).toBe('');
  });

  it('says plainly when saved keys have not passed a test, and never calls them connected', async () => {
    await renderPanel({ ...CONNECTED, last_tested_at: null });
    expect(screen.getByTestId('propertyware-connection-summary').textContent).toContain(
      'have not passed a test since they were saved. Run Test connection.',
    );
    expect(document.body.textContent).not.toMatch(/\bconnected\b/i);
  });

  it('lists the permissions the key needs, and where to create it', async () => {
    await renderPanel(NOT_CONNECTED);
    const where = screen.getByTestId('propertyware-where');
    for (const permission of PROPERTYWARE_READ_PERMISSIONS) {
      expect(where.textContent).toContain(permission);
    }
    expect(where.textContent).toContain('Administration Setup → API Keys');
    expect(where.textContent).toContain('ask Propertyware support');
  });

  it('refuses to save while the connector is off, and still lets you disconnect', async () => {
    const { user } = await renderPanel({ ...CONNECTED, connector_enabled: false });
    await fillKeys(user, { systemId: false });
    expect(screen.getByTestId('propertyware-connector-off')).toBeInTheDocument();
    expect(screen.getByTestId('propertyware-save')).toBeDisabled();
    expect(screen.getByTestId('propertyware-test')).toBeDisabled();
    expect(screen.getByTestId('propertyware-sync')).toBeDisabled();
    expect(screen.getByTestId('propertyware-disconnect')).toBeEnabled();
    expect(screen.getByTestId('propertyware-connector-off')).toHaveTextContent(
      'You can still disconnect.',
    );
  });

  it('shows the switched-off state before anyone types keys, with no disconnect to offer', async () => {
    await renderPanel({ ...NOT_CONNECTED, connector_enabled: false });
    expect(screen.getByTestId('propertyware-connector-off')).toBeInTheDocument();
    expect(screen.getByTestId('propertyware-connector-off')).not.toHaveTextContent(/disconnect/i);
    expect(screen.queryByTestId('propertyware-disconnect')).not.toBeInTheDocument();
  });

  it('a failed status read is an error, not an off connector', async () => {
    statusMock.mockRejectedValue(new Error('Only an admin can view the Propertyware connection.'));
    render(<PropertywareApiDetail onClose={vi.fn()} />);
    expect(
      await screen.findByText('Only an admin can view the Propertyware connection.'),
    ).toBeInTheDocument();
    expect(screen.queryByTestId('propertyware-connector-off')).not.toBeInTheDocument();
  });

  it('disconnects only after the confirm, which says the key stays in Propertyware', async () => {
    deleteMock.mockResolvedValue({ ...NOT_CONNECTED, purged_rows: 12 });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false);
    const { user } = await renderPanel(CONNECTED);

    await user.click(screen.getByTestId('propertyware-disconnect'));
    expect(deleteMock).not.toHaveBeenCalled();
    const asked = String(confirm.mock.calls[0][0]);
    expect(asked).toMatch(/does not delete the API key inside Propertyware/);
    expect(asked).toMatch(/will be deleted from Occupella/);
    // A disconnect is a purge; no surface may call it a revocation.
    expect(asked).not.toMatch(/revoke/i);
    expect(asked).not.toMatch(OTHER_SYSTEMS);

    confirm.mockReturnValueOnce(true);
    await user.click(screen.getByTestId('propertyware-disconnect'));
    await waitFor(() => expect(deleteMock).toHaveBeenCalledTimes(1));
    confirm.mockRestore();
  });

  it('reports a sync that left tables stale in the server’s words', async () => {
    const sentence =
      "Some of Propertyware didn't sync this time, so what's on screen for it is from the last " +
      'sync that worked. Vendors: Propertyware refused this API key: it needs the VENDORS - Read permission.';
    syncMock.mockResolvedValue({ ok: false, rows: { properties: 13 }, stale: ['vendors'], error: sentence });
    const { user } = await renderPanel(CONNECTED);
    await user.click(screen.getByTestId('propertyware-sync'));
    const result = await screen.findByTestId('propertyware-result');
    expect(result.textContent).toContain(sentence);
    expect(result.getAttribute('role')).toBe('alert');
  });

  it('reports a clean sync by what it read', async () => {
    syncMock.mockResolvedValue({ ok: true, rows: { properties: 1, units: 4 }, stale: [] });
    const { user } = await renderPanel(CONNECTED);
    await user.click(screen.getByTestId('propertyware-sync'));
    expect((await screen.findByTestId('propertyware-result')).textContent).toContain(
      'Synced 1 building, 4 units.',
    );
  });

  it.each([
    ['not connected', NOT_CONNECTED],
    ['connected', CONNECTED],
    ['switched off', { ...CONNECTED, connector_enabled: false }],
  ])('names no other system when %s', async (_state, status) => {
    await renderPanel(status);
    expect(document.body.textContent).not.toMatch(OTHER_SYSTEMS);
  });
});

describe('syncSummary', () => {
  it("counts each table in Propertyware's words with the right plural", () => {
    expect(
      syncSummary({ properties: 1, units: 157, work_orders: 1, leases: 28, turnovers: 2 }),
    ).toBe('Synced 1 building, 157 units, 1 work order, 28 leases, 2 turnovers opened.');
  });

  it('names an unknown table by itself and says nothing was counted when nothing was', () => {
    expect(syncSummary({ new_table: 2 })).toBe('Synced 2 new table.');
    expect(syncSummary({})).toBe('Sync finished.');
    expect(syncSummary(undefined)).toBe('Sync finished.');
  });
});
