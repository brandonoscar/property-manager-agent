/**
 * DoorLoopApiDetail — the Settings → Connectors panel for DoorLoop.
 *
 * Pins Propertyware's flow for one value (save, then test; sync now;
 * disconnect after a confirm), that every refusal is the server's own sentence
 * (gotcha 137), and that the switched-off state and the Premium-plan
 * requirement show before anyone pastes a key. All values are synthetic.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

// ⚠ Mock the NETWORK calls only; keep every constant real (gotcha 88).
vi.mock('../../services/doorloop', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../services/doorloop')>()),
  getDoorLoopStatus: vi.fn(),
  saveDoorLoopCreds: vi.fn(),
  testDoorLoopConnection: vi.fn(),
  deleteDoorLoopCreds: vi.fn(),
  runDoorLoopSync: vi.fn(),
}));

import { ApiError } from '../../services/api';
import {
  DOORLOOP_KEY_WHERE,
  deleteDoorLoopCreds,
  getDoorLoopStatus,
  runDoorLoopSync,
  saveDoorLoopCreds,
  testDoorLoopConnection,
  type DoorLoopStatus,
} from '../../services/doorloop';
import { DoorLoopApiDetail, syncSummary } from './DoorLoopApiDetail';

const statusMock = vi.mocked(getDoorLoopStatus);
const saveMock = vi.mocked(saveDoorLoopCreds);
const testMock = vi.mocked(testDoorLoopConnection);
const deleteMock = vi.mocked(deleteDoorLoopCreds);
const syncMock = vi.mocked(runDoorLoopSync);

const NOT_CONNECTED: DoorLoopStatus = {
  configured: false,
  api_key_last4: null,
  last_tested_at: null,
  connector_enabled: true,
};

const CONNECTED: DoorLoopStatus = {
  configured: true,
  api_key_last4: '0001',
  last_tested_at: new Date(Date.now() - 2 * 3_600_000).toISOString(),
  connector_enabled: true,
};

/** A synthetic key, as long as a real one could be. */
const KEY = 'dl-synthetic-key-0000000000000001';

/** The other systems the copy must never name (the panel is DoorLoop's). */
const OTHER_SYSTEMS = /buildium|rentvine|rent manager|propertyware|appfolio|yardi/i;

async function renderPanel(status: DoorLoopStatus) {
  statusMock.mockResolvedValue(status);
  const onChange = vi.fn();
  render(<DoorLoopApiDetail onClose={vi.fn()} onChange={onChange} />);
  await waitFor(() => expect(screen.getByLabelText('API key')).toBeInTheDocument());
  return { onChange, user: userEvent.setup() };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('DoorLoopApiDetail', () => {
  it('saves the key, then tests it, and clears it from the form', async () => {
    saveMock.mockResolvedValue({ ...CONNECTED, last_tested_at: null });
    testMock.mockResolvedValue({ ok: true, properties_count: 1, error: null });
    const { onChange, user } = await renderPanel(NOT_CONNECTED);

    await user.type(screen.getByLabelText('API key'), ` ${KEY} `);
    await user.click(screen.getByTestId('doorloop-save'));

    await waitFor(() => expect(testMock).toHaveBeenCalledTimes(1));
    expect(saveMock).toHaveBeenCalledWith({ api_key: KEY });
    // Saved FIRST: the test reads the key the save stored.
    expect(saveMock.mock.invocationCallOrder[0]).toBeLessThan(testMock.mock.invocationCallOrder[0]);
    const result = await screen.findByTestId('doorloop-result');
    // "1 properties" would be the first sentence a one-property customer reads.
    expect(result.textContent).toContain('DoorLoop accepted the key · 1 property.');
    expect(result.textContent).toContain('Occupella has started reading your portfolio.');
    expect(onChange).toHaveBeenCalled();
    expect((screen.getByLabelText('API key') as HTMLInputElement).value).toBe('');
  });

  // Re-pointed from "...and no sync it does not have": since slice A1 a
  // passed test starts the first sync, and the panel says so.
  it('claims no count DoorLoop did not state', async () => {
    saveMock.mockResolvedValue({ ...CONNECTED, last_tested_at: null });
    testMock.mockResolvedValue({ ok: true, properties_count: null, error: null });
    const { user } = await renderPanel(NOT_CONNECTED);
    await user.type(screen.getByLabelText('API key'), KEY);
    await user.click(screen.getByTestId('doorloop-save'));
    // No count stated means none is claimed (gotcha 26).
    expect((await screen.findByTestId('doorloop-result')).textContent).toBe(
      'DoorLoop accepted the key. Occupella has started reading your portfolio.dismiss',
    );
  });

  it('offers Sync now only with a key saved and the connector on', async () => {
    await renderPanel(NOT_CONNECTED);
    expect(screen.getByTestId('doorloop-sync')).toBeDisabled();
  });

  it('reports a sync that left tables stale in the server’s words', async () => {
    const sentence =
      "Some of DoorLoop didn't sync this time, so what's on screen for it is from the last " +
      "sync that worked. Vendors: DoorLoop accepted the API key, but its access role can't read these.";
    syncMock.mockResolvedValue({ ok: false, rows: { properties: 13 }, stale: ['vendors'], error: sentence });
    const { user } = await renderPanel(CONNECTED);
    await user.click(screen.getByTestId('doorloop-sync'));
    const result = await screen.findByTestId('doorloop-result');
    expect(result.textContent).toContain(sentence);
    expect(result.getAttribute('role')).toBe('alert');
  });

  it('reports a clean sync by what it read', async () => {
    syncMock.mockResolvedValue({ ok: true, rows: { properties: 1, units: 4 }, stale: [] });
    const { onChange, user } = await renderPanel(CONNECTED);
    await user.click(screen.getByTestId('doorloop-sync'));
    expect((await screen.findByTestId('doorloop-result')).textContent).toContain(
      'Synced 1 property, 4 units.',
    );
    expect(onChange).toHaveBeenCalled();
  });

  it('a sync refused before it starts shows the refusal, never a crash', async () => {
    syncMock.mockRejectedValue(new ApiError(503, 'The DoorLoop connector is switched off.'));
    const { user } = await renderPanel(CONNECTED);
    await user.click(screen.getByTestId('doorloop-sync'));
    expect((await screen.findByTestId('doorloop-result')).textContent).toContain(
      'The DoorLoop connector is switched off.',
    );
  });

  it('waits for a whole key before it will save', async () => {
    const { user } = await renderPanel(NOT_CONNECTED);
    expect(screen.getByTestId('doorloop-save')).toBeDisabled();
    await user.type(screen.getByLabelText('API key'), 'too-short');
    expect(screen.getByTestId('doorloop-save')).toBeDisabled();
    await user.type(screen.getByLabelText('API key'), '-now-long-enough');
    expect(screen.getByTestId('doorloop-save')).toBeEnabled();
  });

  it.each([
    [422, "That API key can't be used. In DoorLoop, open Settings → Zapier & API Keys and create a key."],
    [409, 'This workspace is already connected to another property-management system. Disconnect it first, then connect DoorLoop.'],
    [503, 'The DoorLoop connector is switched off on this Occupella deployment, so an API key can’t be saved.'],
  ])('shows a %i refusal of the save in the server’s words, and tests nothing', async (code, sentence) => {
    saveMock.mockRejectedValueOnce(new ApiError(code, sentence));
    const { user } = await renderPanel(NOT_CONNECTED);
    await user.type(screen.getByLabelText('API key'), KEY);
    await user.click(screen.getByTestId('doorloop-save'));

    const result = await screen.findByTestId('doorloop-result');
    expect(result.textContent).toContain(sentence);
    expect(result.getAttribute('role')).toBe('alert');
    expect(testMock).not.toHaveBeenCalled();
  });

  it("shows DoorLoop's refusal as the server wrote it", async () => {
    const advice = "DoorLoop accepted this API key, but its access role can't read properties.";
    testMock.mockResolvedValue({ ok: false, properties_count: null, error: advice });
    const { user } = await renderPanel(CONNECTED);

    await user.click(screen.getByTestId('doorloop-test'));

    const result = await screen.findByTestId('doorloop-result');
    expect(result.textContent).toContain(advice);
    expect(result.getAttribute('role')).toBe('alert');
  });

  it('shows what is saved without the key', async () => {
    await renderPanel(CONNECTED);
    const summary = screen.getByTestId('doorloop-connection-summary');
    expect(summary.textContent).toContain('••••0001');
    expect(summary.textContent).toMatch(/Last passed a test 2h ago/);
    expect(screen.getByLabelText('API key')).toHaveAttribute('type', 'password');
    expect((screen.getByLabelText('API key') as HTMLInputElement).value).toBe('');
    expect(screen.getByTestId('doorloop-save')).toHaveTextContent('Replace key');
  });

  it('says plainly when a saved key has not passed a test, and never calls it connected', async () => {
    await renderPanel({ ...CONNECTED, last_tested_at: null });
    expect(screen.getByTestId('doorloop-connection-summary').textContent).toContain(
      'has not passed a test since it was saved. Run Test connection.',
    );
    expect(document.body.textContent).not.toMatch(/\bconnected\b/i);
  });

  it('says where the key is made, and that it needs the Premium plan, before anyone pastes one', async () => {
    await renderPanel(NOT_CONNECTED);
    const where = screen.getByTestId('doorloop-where');
    expect(where.textContent).toContain(DOORLOOP_KEY_WHERE);
    expect(where.textContent).toContain('Settings → Zapier & API Keys');
    expect(where.textContent).toContain("DoorLoop's Premium plan");
    expect(where.textContent).toContain('access role');
  });

  it('refuses to save while the connector is off, and still lets you disconnect', async () => {
    const { user } = await renderPanel({ ...CONNECTED, connector_enabled: false });
    await user.type(screen.getByLabelText('API key'), KEY);
    expect(screen.getByTestId('doorloop-connector-off')).toBeInTheDocument();
    expect(screen.getByTestId('doorloop-save')).toBeDisabled();
    expect(screen.getByTestId('doorloop-test')).toBeDisabled();
    expect(screen.getByTestId('doorloop-disconnect')).toBeEnabled();
    expect(screen.getByTestId('doorloop-connector-off')).toHaveTextContent('You can still disconnect.');
  });

  it('shows the switched-off state before anyone pastes a key, with no disconnect to offer', async () => {
    await renderPanel({ ...NOT_CONNECTED, connector_enabled: false });
    expect(screen.getByTestId('doorloop-connector-off')).toBeInTheDocument();
    expect(screen.getByTestId('doorloop-connector-off')).not.toHaveTextContent(/disconnect/i);
    expect(screen.queryByTestId('doorloop-disconnect')).not.toBeInTheDocument();
  });

  it('a failed status read is an error, not an off connector', async () => {
    statusMock.mockRejectedValue(new Error('Only an admin can view the DoorLoop connection.'));
    render(<DoorLoopApiDetail onClose={vi.fn()} />);
    expect(await screen.findByText('Only an admin can view the DoorLoop connection.')).toBeInTheDocument();
    expect(screen.queryByTestId('doorloop-connector-off')).not.toBeInTheDocument();
  });

  it('disconnects only after the confirm, which says the key stays in DoorLoop', async () => {
    deleteMock.mockResolvedValue({ ...NOT_CONNECTED, purged_rows: 0 });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false);
    const { user } = await renderPanel(CONNECTED);

    await user.click(screen.getByTestId('doorloop-disconnect'));
    expect(deleteMock).not.toHaveBeenCalled();
    const asked = String(confirm.mock.calls[0][0]);
    expect(asked).toMatch(/does not delete the API key inside DoorLoop/);
    // The disconnect purges the synced data: the confirm says so.
    expect(asked).toMatch(/synced DoorLoop data .* will be deleted from Occupella/);
    expect(asked).toMatch(/Settings → Zapier & API Keys/);
    // A disconnect is a purge; no surface may call it a revocation.
    expect(asked).not.toMatch(/revoke/i);
    expect(asked).not.toMatch(OTHER_SYSTEMS);

    confirm.mockReturnValueOnce(true);
    await user.click(screen.getByTestId('doorloop-disconnect'));
    await waitFor(() => expect(deleteMock).toHaveBeenCalledTimes(1));
    confirm.mockRestore();
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
  it('counts each table with the right plural', () => {
    expect(
      syncSummary({ properties: 1, units: 157, work_orders: 1, leases: 28, lease_transactions: 2 }),
    ).toBe('Synced 1 property, 157 units, 1 work order, 28 leases, 2 charges, payments and credits.');
    expect(syncSummary({ tasks: 1, applicants: 2, bills: 3, gl_accounts: 40, turnovers: 1 })).toBe(
      'Synced 1 task, 2 applicants, 3 bills, 40 accounts, 1 turnover opened.',
    );
  });

  it('names an unknown table by itself and says nothing was counted when nothing was', () => {
    expect(syncSummary({ new_table: 2 })).toBe('Synced 2 new table.');
    expect(syncSummary({})).toBe('Sync finished.');
    expect(syncSummary(undefined)).toBe('Sync finished.');
  });
});
