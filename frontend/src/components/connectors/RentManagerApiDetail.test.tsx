/**
 * RentManagerApiDetail — the Settings → Connectors panel for Rent Manager.
 *
 * These pin the flow (save, then test; the location as the number the backend
 * takes), that every refusal is the server's own sentence (gotcha 137), that a
 * switched-off deployment says so before anyone types a password, that nothing
 * says "connected" without a passed test, and that the copy names no other
 * system and never calls the disconnect a revocation.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

// ⚠ Mock the NETWORK calls only; keep every constant real (gotcha 88).
vi.mock('../../services/rentmanager', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../services/rentmanager')>()),
  getRentManagerStatus: vi.fn(),
  saveRentManagerCreds: vi.fn(),
  testRentManagerConnection: vi.fn(),
  deleteRentManagerCreds: vi.fn(),
  runRentManagerSync: vi.fn(),
}));

import { ApiError } from '../../services/api';
import {
  deleteRentManagerCreds,
  getRentManagerStatus,
  runRentManagerSync,
  saveRentManagerCreds,
  testRentManagerConnection,
  type RentManagerStatus,
} from '../../services/rentmanager';
import {
  FIELD_ADVICE,
  RentManagerApiDetail,
  parseLocationId,
  syncSummary,
} from './RentManagerApiDetail';

const statusMock = vi.mocked(getRentManagerStatus);
const saveMock = vi.mocked(saveRentManagerCreds);
const testMock = vi.mocked(testRentManagerConnection);
const deleteMock = vi.mocked(deleteRentManagerCreds);
const syncMock = vi.mocked(runRentManagerSync);

const NOT_CONNECTED: RentManagerStatus = {
  configured: false,
  corp_id: null,
  location_id: null,
  username_last4: null,
  partner_token_saved: false,
  last_tested_at: null,
  connector_enabled: true,
};

const CONNECTED: RentManagerStatus = {
  configured: true,
  corp_id: 'synthcorp',
  location_id: 3,
  username_last4: 'user',
  partner_token_saved: false,
  last_tested_at: new Date(Date.now() - 2 * 3_600_000).toISOString(),
  connector_enabled: true,
};

async function renderPanel(status: RentManagerStatus) {
  statusMock.mockResolvedValue(status);
  const onChange = vi.fn();
  render(<RentManagerApiDetail onClose={vi.fn()} onChange={onChange} />);
  await waitFor(() => expect(screen.getByLabelText('Corp ID')).toBeInTheDocument());
  return { onChange, user: userEvent.setup() };
}

async function fillAll(user: ReturnType<typeof userEvent.setup>, location = '1') {
  await user.clear(screen.getByLabelText('Corp ID'));
  await user.type(screen.getByLabelText('Corp ID'), 'synthcorp');
  await user.clear(screen.getByLabelText('Location ID'));
  if (location) await user.type(screen.getByLabelText('Location ID'), location);
  await user.type(screen.getByLabelText('Username'), 'rm-api-user');
  await user.type(screen.getByLabelText('Password'), 'rm-password-1');
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('RentManagerApiDetail', () => {
  it('saves, then tests, and says the portfolio is being read', async () => {
    saveMock.mockResolvedValue({ ...CONNECTED, last_tested_at: null });
    testMock.mockResolvedValue({ ok: true, properties_count: 1, error: null });
    const { onChange, user } = await renderPanel(NOT_CONNECTED);

    await fillAll(user);
    await user.click(screen.getByTestId('rentmanager-save'));

    await waitFor(() => expect(testMock).toHaveBeenCalledTimes(1));
    expect(saveMock).toHaveBeenCalledWith({
      corp_id: 'synthcorp',
      location_id: 1,
      username: 'rm-api-user',
      password: 'rm-password-1',
      partner_token: undefined,
    });
    // Saved FIRST: the test reads the details the save stored.
    expect(saveMock.mock.invocationCallOrder[0]).toBeLessThan(testMock.mock.invocationCallOrder[0]);
    const result = await screen.findByTestId('rentmanager-result');
    expect(result.textContent).toContain('Connected to Rent Manager · 1 property.');
    expect(onChange).toHaveBeenCalled();
    // The secrets do not linger in the form.
    expect((screen.getByLabelText('Password') as HTMLInputElement).value).toBe('');
    expect((screen.getByLabelText('Username') as HTMLInputElement).value).toBe('');
  });

  it('claims no count Rent Manager did not state', async () => {
    saveMock.mockResolvedValue({ ...CONNECTED, last_tested_at: null });
    testMock.mockResolvedValue({ ok: true, properties_count: null, error: null });
    const { user } = await renderPanel(NOT_CONNECTED);
    await fillAll(user);
    await user.click(screen.getByTestId('rentmanager-save'));
    expect((await screen.findByTestId('rentmanager-result')).textContent).toBe(
      'Connected to Rent Manager. Occupella has started reading your portfolio.dismiss',
    );
  });

  it('says nothing about being connected when the test fails, in the server’s words', async () => {
    const advice = 'Rent Manager refused the username or password. Check them, then save them here again.';
    saveMock.mockResolvedValue({ ...CONNECTED, last_tested_at: null });
    testMock.mockResolvedValue({ ok: false, properties_count: null, error: advice });
    const { user } = await renderPanel(NOT_CONNECTED);
    await fillAll(user);
    await user.click(screen.getByTestId('rentmanager-save'));
    const result = await screen.findByTestId('rentmanager-result');
    expect(result.textContent).toContain(advice);
    expect(result.textContent).not.toMatch(/connected/i);
    expect(result.getAttribute('role')).toBe('alert');
  });

  it('shows a refused save as the server wrote it, and tests nothing', async () => {
    const sentence = 'Enter the Rent Manager location ID as a whole number above 0, such as 1.';
    saveMock.mockRejectedValueOnce(new ApiError(422, sentence));
    const { user } = await renderPanel(NOT_CONNECTED);
    await fillAll(user);
    await user.click(screen.getByTestId('rentmanager-save'));
    expect((await screen.findByTestId('rentmanager-result')).textContent).toContain(sentence);
    expect(testMock).not.toHaveBeenCalled();
  });

  it.each(['', '0', '1.5', 'one', '-2', '2147483648'])(
    'cannot save with the location %j',
    async (location) => {
      const { user } = await renderPanel(NOT_CONNECTED);
      await fillAll(user, location);
      expect(screen.getByTestId('rentmanager-save')).toBeDisabled();
    },
  );

  it('says why Save is off for a location that is not a number, until it is one', async () => {
    // Found rendering the panel in Chromium: "main office" disabled Save and
    // nothing on screen said why (rule 16).
    const { user } = await renderPanel(NOT_CONNECTED);
    expect(screen.queryByTestId('rentmanager-location-error')).not.toBeInTheDocument();
    await fillAll(user, 'main office');
    const error = screen.getByTestId('rentmanager-location-error');
    expect(error).toHaveTextContent(
      'Enter the Rent Manager location ID as a whole number above 0, such as 1.',
    );
    expect(screen.getByLabelText('Location ID')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByLabelText('Location ID')).toHaveAttribute('inputmode', 'numeric');
    expect(screen.getByTestId('rentmanager-save')).toBeDisabled();

    await user.clear(screen.getByLabelText('Location ID'));
    expect(screen.queryByTestId('rentmanager-location-error')).not.toBeInTheDocument();
    await user.type(screen.getByLabelText('Location ID'), '3');
    expect(screen.queryByTestId('rentmanager-location-error')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Location ID')).not.toHaveAttribute('aria-invalid');
    expect(screen.getByTestId('rentmanager-save')).toBeEnabled();
  });

  it('says why Save is off for a corp ID that is not one, until it is one', async () => {
    const { user } = await renderPanel(NOT_CONNECTED);
    await fillAll(user);
    await user.clear(screen.getByLabelText('Corp ID'));
    await user.type(screen.getByLabelText('Corp ID'), 'synthcorp.api.rentmanager.com');
    expect(screen.getByTestId('rentmanager-corp-error')).toHaveTextContent(FIELD_ADVICE.corp_id);
    expect(screen.getByTestId('rentmanager-save')).toBeDisabled();
    await user.clear(screen.getByLabelText('Corp ID'));
    await user.type(screen.getByLabelText('Corp ID'), 'synthcorp');
    expect(screen.queryByTestId('rentmanager-corp-error')).not.toBeInTheDocument();
    expect(screen.getByTestId('rentmanager-save')).toBeEnabled();
  });

  it('fills in the saved corp ID and location, never the username or password', async () => {
    await renderPanel(CONNECTED);
    expect((screen.getByLabelText('Corp ID') as HTMLInputElement).value).toBe('synthcorp');
    expect((screen.getByLabelText('Location ID') as HTMLInputElement).value).toBe('3');
    expect((screen.getByLabelText('Username') as HTMLInputElement).value).toBe('');
    expect((screen.getByLabelText('Password') as HTMLInputElement).value).toBe('');
    expect(screen.getByLabelText('Password')).toHaveAttribute('type', 'password');
  });

  it('shows what is saved without any secret', async () => {
    await renderPanel({ ...CONNECTED, partner_token_saved: true });
    const summary = screen.getByTestId('rentmanager-connection-summary');
    expect(summary.textContent).toContain('synthcorp.api.rentmanager.com');
    expect(summary.textContent).toContain('Location 3');
    expect(summary.textContent).toContain('••••user');
    expect(summary.textContent).toContain('Partner token saved');
    expect(summary.textContent).toMatch(/Last passed a test 2h ago/);
  });

  it('says plainly when saved details have not passed a test', async () => {
    await renderPanel({ ...CONNECTED, last_tested_at: null, username_last4: null });
    const summary = screen.getByTestId('rentmanager-connection-summary');
    expect(summary.textContent).toContain('have not passed a test since they were saved');
    expect(summary.textContent).toContain('User saved');
  });

  it('says it is switched off before any field, and nothing can be typed or saved', async () => {
    await renderPanel({ ...CONNECTED, connector_enabled: false });
    const banner = screen.getByTestId('rentmanager-connector-off');
    // Before the first field in the document, so it is read first.
    expect(
      banner.compareDocumentPosition(screen.getByLabelText('Corp ID')) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    for (const label of ['Corp ID', 'Location ID', 'Username', 'Password']) {
      expect(screen.getByLabelText(label)).toBeDisabled();
    }
    expect(screen.getByTestId('rentmanager-save')).toBeDisabled();
    expect(screen.getByTestId('rentmanager-test')).toBeDisabled();
    expect(screen.getByTestId('rentmanager-sync')).toBeDisabled();
    expect(screen.getByTestId('rentmanager-disconnect')).toBeEnabled();
    expect(banner).toHaveTextContent('You can still disconnect.');
  });

  it('a switch turned off while the panel is open stops a filled-in save', async () => {
    // The disabled fields keep what was typed, so the form alone still reads
    // as complete: the button has to know about the switch itself.
    testMock.mockResolvedValue({ ok: true, properties_count: 2, error: null });
    const { user } = await renderPanel(CONNECTED);
    await fillAll(user);
    expect(screen.getByTestId('rentmanager-save')).toBeEnabled();
    statusMock.mockResolvedValue({ ...CONNECTED, connector_enabled: false });
    await user.click(screen.getByTestId('rentmanager-test'));
    await screen.findByTestId('rentmanager-connector-off');
    expect((screen.getByLabelText('Password') as HTMLInputElement).value).toBe('rm-password-1');
    expect(screen.getByTestId('rentmanager-save')).toBeDisabled();
  });

  it('switched off with nothing stored, it does not offer a disconnect', async () => {
    await renderPanel({ ...NOT_CONNECTED, connector_enabled: false });
    expect(screen.getByTestId('rentmanager-connector-off')).not.toHaveTextContent(/disconnect/i);
    expect(screen.queryByTestId('rentmanager-disconnect')).not.toBeInTheDocument();
  });

  it('a failed status read is an error, not a switched-off connector', async () => {
    statusMock.mockRejectedValue(new Error('Could not load the Rent Manager connection.'));
    render(<RentManagerApiDetail onClose={vi.fn()} />);
    expect(await screen.findByText('Could not load the Rent Manager connection.')).toBeInTheDocument();
    expect(screen.queryByTestId('rentmanager-connector-off')).not.toBeInTheDocument();
  });

  it('asks for a partner token only when one is wanted, and sends it trimmed', async () => {
    saveMock.mockResolvedValue({ ...CONNECTED, partner_token_saved: true });
    testMock.mockResolvedValue({ ok: true, properties_count: 2, error: null });
    const { user } = await renderPanel(NOT_CONNECTED);
    expect(screen.queryByLabelText('Partner token')).not.toBeInTheDocument();
    await user.click(screen.getByTestId('rentmanager-partner-toggle'));
    await fillAll(user);
    await user.type(screen.getByLabelText('Partner token'), '  rm-partner  ');
    await user.click(screen.getByTestId('rentmanager-save'));
    await waitFor(() =>
      expect(saveMock).toHaveBeenCalledWith(expect.objectContaining({ partner_token: 'rm-partner' })),
    );
  });

  it('with a partner token saved, says a new save replaces it', async () => {
    await renderPanel({ ...CONNECTED, partner_token_saved: true });
    expect(screen.getByLabelText('Partner token')).toHaveValue('');
    expect(screen.getByText(/Saving new details replaces it/)).toBeInTheDocument();
  });

  it('disconnects only after the confirm, which says nothing changes inside Rent Manager', async () => {
    deleteMock.mockResolvedValue({ ...NOT_CONNECTED });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false);
    const { user } = await renderPanel(CONNECTED);

    await user.click(screen.getByTestId('rentmanager-disconnect'));
    expect(deleteMock).not.toHaveBeenCalled();
    const text = confirm.mock.calls[0][0] as string;
    expect(text).toMatch(/Nothing changes inside Rent Manager/);
    expect(text).toMatch(/deleted from Occupella/);
    expect(text).not.toMatch(/revoke/i);

    confirm.mockReturnValueOnce(true);
    await user.click(screen.getByTestId('rentmanager-disconnect'));
    await waitFor(() => expect(deleteMock).toHaveBeenCalledTimes(1));
    confirm.mockRestore();
  });

  it('reports a sync that left stages stale as not finished', async () => {
    syncMock.mockResolvedValue({
      ok: false,
      rows: { properties: 2 },
      stale: ['units'],
      error: "Some of Rent Manager didn't sync this time: units.",
    });
    const { user } = await renderPanel(CONNECTED);
    await user.click(screen.getByTestId('rentmanager-sync'));
    const result = await screen.findByTestId('rentmanager-result');
    expect(result.textContent).toContain("Some of Rent Manager didn't sync this time: units.");
    expect(result.getAttribute('role')).toBe('alert');
  });

  it('reports a finished sync by what it counted', async () => {
    syncMock.mockResolvedValue({ ok: true, rows: { properties: 1, units: 3 }, stale: [] });
    const { user } = await renderPanel(CONNECTED);
    await user.click(screen.getByTestId('rentmanager-sync'));
    expect((await screen.findByTestId('rentmanager-result')).textContent).toContain(
      'Synced 1 property, 3 units.',
    );
  });

  it.each([
    ['nothing saved', NOT_CONNECTED],
    ['connected', { ...CONNECTED, partner_token_saved: true }],
    ['switched off', { ...CONNECTED, connector_enabled: false }],
  ])('names no other system (%s)', async (_state, status) => {
    await renderPanel(status as RentManagerStatus);
    expect(document.body.textContent).not.toMatch(/Buildium|Rentvine|Propertyware|AppFolio|Yardi/);
  });
});

describe('syncSummary', () => {
  it('counts each stage with the right plural', () => {
    expect(syncSummary({ properties: 1, units: 3, work_orders: 1, balances: 2 })).toBe(
      'Synced 1 property, 3 units, 1 work order, 2 open balances.',
    );
  });

  it('names an unknown stage by itself and says nothing was counted when nothing was', () => {
    expect(syncSummary({ new_stage: 2 })).toBe('Synced 2 new stage.');
    expect(syncSummary({})).toBe('Sync finished.');
    expect(syncSummary(undefined)).toBe('Sync finished.');
  });
});

describe('parseLocationId', () => {
  it('takes a whole number above 0 that fits the column, and nothing else', () => {
    expect(parseLocationId(' 7 ')).toBe(7);
    expect(parseLocationId('2147483647')).toBe(2_147_483_647);
    for (const bad of ['', '0', '-1', '1.0', '1e3', 'abc', '2147483648']) {
      expect(parseLocationId(bad)).toBeNull();
    }
  });
});
