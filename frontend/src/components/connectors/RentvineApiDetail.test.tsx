/**
 * RentvineApiDetail — the Settings → Connectors panel for Rentvine.
 *
 * Until it existed, a Rentvine workspace could connect only in the onboarding
 * wizard, and a key Rentvine revoked could not be replaced anywhere in the app,
 * while chat sent people to this page to do it. These pin the flow the wizard
 * already had (save, then test; the account code only when the server asks for
 * it) and that every refusal is the server's own sentence (gotcha 137).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

// ⚠ Mock the NETWORK calls only; keep every constant real (gotcha 88).
vi.mock('../../services/rentvine', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../services/rentvine')>()),
  getRentvineStatus: vi.fn(),
  saveRentvineCreds: vi.fn(),
  testRentvineConnection: vi.fn(),
  deleteRentvineCreds: vi.fn(),
  runRentvineSync: vi.fn(),
  saveRentvineWebhookSecret: vi.fn(),
  deleteRentvineWebhookSecret: vi.fn(),
}));

import { ApiError } from '../../services/api';
import {
  RENTVINE_WEBHOOK_EVENTS,
  deleteRentvineCreds,
  deleteRentvineWebhookSecret,
  getRentvineStatus,
  runRentvineSync,
  saveRentvineCreds,
  saveRentvineWebhookSecret,
  testRentvineConnection,
  type RentvineStatus,
} from '../../services/rentvine';
import { RentvineApiDetail, syncSummary } from './RentvineApiDetail';

const statusMock = vi.mocked(getRentvineStatus);
const saveMock = vi.mocked(saveRentvineCreds);
const testMock = vi.mocked(testRentvineConnection);
const deleteMock = vi.mocked(deleteRentvineCreds);
const syncMock = vi.mocked(runRentvineSync);
const saveKeyMock = vi.mocked(saveRentvineWebhookSecret);
const deleteKeyMock = vi.mocked(deleteRentvineWebhookSecret);

const NOT_CONNECTED: RentvineStatus = {
  configured: false,
  account_code: null,
  api_key_last4: null,
  last_tested_at: null,
  connector_enabled: true,
};

const CONNECTED: RentvineStatus = {
  configured: true,
  account_code: 'examplepm',
  api_key_last4: '9f2c',
  last_tested_at: new Date(Date.now() - 2 * 3_600_000).toISOString(),
  connector_enabled: true,
};

async function renderPanel(status: RentvineStatus) {
  statusMock.mockResolvedValue(status);
  const onChange = vi.fn();
  render(<RentvineApiDetail onClose={vi.fn()} onChange={onChange} />);
  await waitFor(() => expect(screen.getByLabelText('Access key')).toBeInTheDocument());
  return { onChange, user: userEvent.setup() };
}

async function fillKeys(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText('Access key'), 'key-1234');
  await user.type(screen.getByLabelText('Secret'), 'secret-12345678');
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('RentvineApiDetail', () => {
  it('saves, then tests, and says the portfolio is being read', async () => {
    saveMock.mockResolvedValue({ ...CONNECTED, last_tested_at: null });
    testMock.mockResolvedValue({ ok: true, properties_count: 1, error: null });
    const { onChange, user } = await renderPanel(NOT_CONNECTED);

    await fillKeys(user);
    await user.click(screen.getByTestId('rentvine-save'));

    await waitFor(() => expect(testMock).toHaveBeenCalledTimes(1));
    expect(saveMock).toHaveBeenCalledWith({
      api_key: 'key-1234',
      api_secret: 'secret-12345678',
      account_code: undefined,
    });
    // Saved FIRST: the test reads the keys the save stored.
    expect(saveMock.mock.invocationCallOrder[0]).toBeLessThan(testMock.mock.invocationCallOrder[0]);
    const result = await screen.findByTestId('rentvine-result');
    // "1 properties" would be the first sentence a one-building customer reads.
    expect(result.textContent).toContain('Connected to Rentvine · 1 property.');
    expect(onChange).toHaveBeenCalled();
    // The secret does not linger in the form.
    expect((screen.getByLabelText('Secret') as HTMLInputElement).value).toBe('');
  });

  it('asks for the account code only when the server does, in its words', async () => {
    const sentence =
      "Occupella could not work out your Rentvine address from those keys. Open Rentvine and look at your browser's address bar.";
    saveMock.mockRejectedValueOnce(new ApiError(422, sentence));
    const { user } = await renderPanel(NOT_CONNECTED);
    expect(screen.queryByLabelText('Rentvine account code')).not.toBeInTheDocument();

    await fillKeys(user);
    await user.click(screen.getByTestId('rentvine-save'));

    expect(await screen.findByLabelText('Rentvine account code')).toBeInTheDocument();
    expect((await screen.findByTestId('rentvine-result')).textContent).toContain(sentence);
    expect(testMock).not.toHaveBeenCalled();

    saveMock.mockResolvedValueOnce({ ...CONNECTED, last_tested_at: null });
    testMock.mockResolvedValueOnce({ ok: true, properties_count: null, error: null });
    await user.type(screen.getByLabelText('Access key'), 'key-1234');
    await user.type(screen.getByLabelText('Secret'), 'secret-12345678');
    await user.type(screen.getByLabelText('Rentvine account code'), 'examplepm');
    await user.click(screen.getByTestId('rentvine-save'));
    await waitFor(() =>
      expect(saveMock).toHaveBeenLastCalledWith(
        expect.objectContaining({ account_code: 'examplepm' }),
      ),
    );
    // No count stated means none is claimed (gotcha 26).
    expect((await screen.findByTestId('rentvine-result')).textContent).toBe(
      'Connected to Rentvine. Occupella has started reading your portfolio.dismiss',
    );
  });

  it("shows Rentvine's refusal as the server wrote it", async () => {
    const advice = 'Rentvine rejected the access key or secret. Re-copy both from Rentvine.';
    testMock.mockResolvedValue({ ok: false, properties_count: null, error: advice });
    const { user } = await renderPanel(CONNECTED);

    await user.click(screen.getByTestId('rentvine-test'));

    const result = await screen.findByTestId('rentvine-result');
    expect(result.textContent).toContain(advice);
    expect(result.getAttribute('role')).toBe('alert');
  });

  it('shows what is saved without any secret', async () => {
    await renderPanel(CONNECTED);
    const summary = screen.getByTestId('rentvine-connection-summary');
    expect(summary.textContent).toContain('examplepm.rentvine.com');
    expect(summary.textContent).toContain('••••9f2c');
    expect(summary.textContent).toMatch(/Last passed a test 2h ago/);
  });

  it('says plainly when saved keys have not passed a test', async () => {
    await renderPanel({ ...CONNECTED, last_tested_at: null });
    expect(screen.getByTestId('rentvine-connection-summary').textContent).toContain(
      'have not passed a test since they were saved',
    );
  });

  it('refuses to save while the connector is off, and still lets you disconnect', async () => {
    const { user } = await renderPanel({ ...CONNECTED, connector_enabled: false });
    await fillKeys(user);
    expect(screen.getByTestId('rentvine-connector-off')).toBeInTheDocument();
    expect(screen.getByTestId('rentvine-save')).toBeDisabled();
    expect(screen.getByTestId('rentvine-test')).toBeDisabled();
    expect(screen.getByTestId('rentvine-sync')).toBeDisabled();
    expect(screen.getByTestId('rentvine-disconnect')).toBeEnabled();
    expect(screen.getByTestId('rentvine-connector-off')).toHaveTextContent(
      'You can still disconnect.',
    );
  });

  it('with the connector off and no keys stored, it does not offer a disconnect', async () => {
    // Found in a real-browser render, 2026-10-03: the banner pointed at a
    // Disconnect button that is not on the screen when no keys are stored (rule 16).
    await renderPanel({ ...NOT_CONNECTED, connector_enabled: false });
    expect(screen.getByTestId('rentvine-connector-off')).not.toHaveTextContent(/disconnect/i);
    expect(screen.queryByTestId('rentvine-disconnect')).not.toBeInTheDocument();
  });

  it('a failed status read is an error, not an off connector', async () => {
    statusMock.mockRejectedValue(new Error('Could not load the Rentvine connection.'));
    render(<RentvineApiDetail onClose={vi.fn()} />);
    expect(await screen.findByText('Could not load the Rentvine connection.')).toBeInTheDocument();
    expect(screen.queryByTestId('rentvine-connector-off')).not.toBeInTheDocument();
  });

  it('disconnects only after the confirm, which says the key stays in Rentvine', async () => {
    deleteMock.mockResolvedValue({ ...NOT_CONNECTED });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false);
    const { user } = await renderPanel(CONNECTED);

    await user.click(screen.getByTestId('rentvine-disconnect'));
    expect(deleteMock).not.toHaveBeenCalled();
    // ADR 0010 e2: no surface may call this a revocation.
    expect(confirm.mock.calls[0][0]).toMatch(/does not delete the key inside Rentvine/);
    expect(confirm.mock.calls[0][0]).not.toMatch(/revoke/i);

    confirm.mockReturnValueOnce(true);
    await user.click(screen.getByTestId('rentvine-disconnect'));
    await waitFor(() => expect(deleteMock).toHaveBeenCalledTimes(1));
    confirm.mockRestore();
  });

  it('reports a sync that left stages stale as not finished', async () => {
    syncMock.mockResolvedValue({
      ok: false,
      rows: { properties: 13 },
      stale: ['leases'],
      error: "Some of Rentvine didn't sync this time: leases.",
    });
    const { user } = await renderPanel(CONNECTED);
    await user.click(screen.getByTestId('rentvine-sync'));
    const result = await screen.findByTestId('rentvine-result');
    expect(result.textContent).toContain("Some of Rentvine didn't sync this time: leases.");
    expect(result.getAttribute('role')).toBe('alert');
  });
});

describe('syncSummary', () => {
  it('counts each stage with the right plural', () => {
    expect(syncSummary({ properties: 1, units: 157, work_orders: 1, leases: 28 })).toBe(
      'Synced 1 property, 157 units, 1 work order, 28 lease records.',
    );
  });

  it('names an unknown stage by itself and says nothing was counted when nothing was', () => {
    expect(syncSummary({ new_stage: 2 })).toBe('Synced 2 new stage.');
    expect(syncSummary({})).toBe('Sync finished.');
    expect(syncSummary(undefined)).toBe('Sync finished.');
  });
});

describe('Rentvine webhooks', () => {
  // The deployment can check a signature, and no key is stored yet.
  const READY: RentvineStatus = {
    ...CONNECTED,
    webhook_scheme_ready: true,
    webhook_secret_saved: false,
  };

  it('is not offered before Rentvine is connected', async () => {
    await renderPanel({ ...NOT_CONNECTED, webhook_scheme_ready: true });
    expect(screen.queryByTestId('rentvine-webhooks')).not.toBeInTheDocument();
  });

  it('gives the URL with the account code, all eight events, and says no key is saved', async () => {
    await renderPanel(READY);
    const section = screen.getByTestId('rentvine-webhooks');
    expect(section.textContent).toContain('/webhooks/rentvine/examplepm');
    for (const event of RENTVINE_WEBHOOK_EVENTS) {
      expect(screen.getByTestId('rentvine-webhook-events').textContent).toContain(event);
    }
    expect(screen.getByTestId('rentvine-webhook-key-status')).toHaveTextContent(
      'No signing key saved yet',
    );
  });

  it.each([
    ['cannot check a signature', false],
    ['did not say', undefined],
  ])('when the deployment %s, it says deliveries are turned away and asks for nothing', async (_why, ready) => {
    await renderPanel({ ...CONNECTED, webhook_scheme_ready: ready });
    expect(screen.getByTestId('rentvine-webhooks-not-ready')).toHaveTextContent(
      'turns away every delivery',
    );
    expect(screen.queryByLabelText('Signing key')).not.toBeInTheDocument();
    expect(screen.getByTestId('rentvine-webhooks').textContent).not.toContain('/webhooks/rentvine/');
  });

  it('saves the pasted key without the spaces around it, then re-reads the status', async () => {
    saveKeyMock.mockResolvedValue({ ...READY, webhook_secret_saved: true });
    const { user } = await renderPanel(READY);
    statusMock.mockResolvedValue({ ...READY, webhook_secret_saved: true });

    await user.type(screen.getByLabelText('Signing key'), '  rv-signing-key  ');
    await user.click(screen.getByTestId('rentvine-webhook-key-save'));

    await waitFor(() => expect(saveKeyMock).toHaveBeenCalledWith('rv-signing-key'));
    expect(await screen.findByTestId('rentvine-webhook-result')).toHaveTextContent('Signing key saved.');
    await waitFor(() =>
      expect(screen.getByTestId('rentvine-webhook-key-status')).toHaveTextContent(
        'A signing key is saved.',
      ),
    );
    expect((screen.getByLabelText('Signing key') as HTMLInputElement).value).toBe('');
  });

  it("shows the server's refusal in its own words and keeps what was typed", async () => {
    const sentence = "That doesn't look like a whole signing key. Copy it again from Rentvine.";
    saveKeyMock.mockRejectedValue(new ApiError(422, sentence));
    const { user } = await renderPanel(READY);

    await user.type(screen.getByLabelText('Signing key'), 'half-a-key');
    await user.click(screen.getByTestId('rentvine-webhook-key-save'));

    const result = await screen.findByTestId('rentvine-webhook-result');
    expect(result).toHaveTextContent(sentence);
    expect(result.getAttribute('role')).toBe('alert');
    expect((screen.getByLabelText('Signing key') as HTMLInputElement).value).toBe('half-a-key');
  });

  it('cannot save an empty key', async () => {
    const { user } = await renderPanel(READY);
    expect(screen.getByTestId('rentvine-webhook-key-save')).toBeDisabled();
    await user.type(screen.getByLabelText('Signing key'), '   ');
    expect(screen.getByTestId('rentvine-webhook-key-save')).toBeDisabled();
  });

  it('removes a saved key only after the confirm, and keeps the connection', async () => {
    deleteKeyMock.mockResolvedValue({ ...READY, webhook_secret_saved: false });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false);
    const { user } = await renderPanel({ ...READY, webhook_secret_saved: true });

    await user.click(screen.getByTestId('rentvine-webhook-key-remove'));
    expect(deleteKeyMock).not.toHaveBeenCalled();
    expect(confirm.mock.calls[0][0]).toMatch(/turn away every Rentvine webhook/);

    confirm.mockReturnValueOnce(true);
    await user.click(screen.getByTestId('rentvine-webhook-key-remove'));
    await waitFor(() => expect(deleteKeyMock).toHaveBeenCalledTimes(1));
    expect(deleteMock).not.toHaveBeenCalled();
    confirm.mockRestore();
  });

  it('with the connector off, a key cannot be saved but a saved one can be removed', async () => {
    const { user } = await renderPanel({
      ...READY,
      connector_enabled: false,
      webhook_secret_saved: true,
    });
    await user.type(screen.getByLabelText('Signing key'), 'rv-signing-key');
    expect(screen.getByTestId('rentvine-webhook-key-save')).toBeDisabled();
    expect(screen.getByTestId('rentvine-webhook-key-remove')).toBeEnabled();
  });

  it('never puts a saved key back on the screen', async () => {
    await renderPanel({ ...READY, webhook_secret_saved: true });
    expect((screen.getByLabelText('Signing key') as HTMLInputElement).value).toBe('');
    expect(screen.getByLabelText('Signing key')).toHaveAttribute('type', 'password');
  });
});
