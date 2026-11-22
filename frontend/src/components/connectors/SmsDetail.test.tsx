/**
 * SmsDetail — the consent-checkbox gate + ledger rendering.
 *
 * Pins the compliance-relevant UI behavior: the submit button stays
 * disabled until the explicit consent-language checkbox is ticked (the
 * A2P campaign's declared 'app_settings' opt-in flow), the status line
 * distinguishes Dark from Live, and revoked rows render as Revoked with
 * no Revoke button.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

// ⚠ Mock the NETWORK calls only; keep every constant real (gotcha 88).
// The hand-written literal list this replaces went stale the moment the
// module gained an export — `OUTCOME_LABEL` — and vitest failed with
// "no export is defined on the mock", far from the change that caused it.
// Worse, stubbing a lookup table would make each render test assert whatever
// the stub said rather than what a manager actually reads.
vi.mock('../../services/sms', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../services/sms')>()),
  fetchSmsStatus: vi.fn(),
  listSmsConsent: vi.fn(),
  addSmsConsent: vi.fn(),
  revokeSmsConsent: vi.fn(),
}));

import {
  addSmsConsent,
  fetchSmsStatus,
  listSmsConsent,
  type SmsConsent,
} from '../../services/sms';
import { SmsDetail } from './SmsDetail';

const statusMock = vi.mocked(fetchSmsStatus);
const listMock = vi.mocked(listSmsConsent);
const addMock = vi.mocked(addSmsConsent);

const ACTIVE_ROW: SmsConsent = {
  id: 'c1',
  phone: '+15551234567',
  display_name: 'Dana Ops',
  recipient_kind: 'manager',
  source: 'app_settings',
  timezone: 'America/New_York',
  consented_at: '2026-08-01T12:00:00Z',
  revoked_at: null,
};

const REVOKED_ROW: SmsConsent = {
  ...ACTIVE_ROW,
  id: 'c2',
  phone: '+15559876543',
  display_name: 'Sam PM',
  revoked_at: '2026-08-10T12:00:00Z',
};

beforeEach(() => {
  vi.clearAllMocks();
  statusMock.mockResolvedValue({
    enabled: false,
    configured: true,
    consent_count: 1,
    revoked_count: 1,
  });
  listMock.mockResolvedValue([ACTIVE_ROW, REVOKED_ROW]);
});

describe('SmsDetail', () => {
  it('shows Dark while awaiting carrier approval and renders the ledger', async () => {
    render(<SmsDetail onClose={() => {}} />);
    await waitFor(() =>
      expect(screen.getByTestId('sms-status-badge').textContent).toBe('Dark'),
    );
    expect(screen.getByText(/Awaiting carrier approval/)).toBeTruthy();
    expect(screen.getByText('+15551234567')).toBeTruthy();
    expect(screen.getByText('Active')).toBeTruthy();
    expect(screen.getByText('Revoked')).toBeTruthy();
    // Only the ACTIVE row offers Revoke.
    expect(screen.getAllByRole('button', { name: 'Revoke' })).toHaveLength(1);
  });

  it('shows Live when enabled and configured', async () => {
    statusMock.mockResolvedValue({
      enabled: true,
      configured: true,
      consent_count: 1,
      revoked_count: 0,
    });
    render(<SmsDetail onClose={() => {}} />);
    await waitFor(() =>
      expect(screen.getByTestId('sms-status-badge').textContent).toBe('Live'),
    );
  });

  it('keeps the submit disabled until the consent checkbox is ticked', async () => {
    const user = userEvent.setup();
    render(<SmsDetail onClose={() => {}} />);
    await waitFor(() => expect(listMock).toHaveBeenCalled());

    const submit = screen.getByRole('button', { name: 'Record consent' });
    await user.type(screen.getByLabelText('Phone number'), '(555) 000-1111');
    expect((submit as HTMLButtonElement).disabled).toBe(true); // phone alone isn't enough

    await user.click(screen.getByLabelText('Consent confirmation'));
    expect((submit as HTMLButtonElement).disabled).toBe(false);

    addMock.mockResolvedValue({ ...ACTIVE_ROW, id: 'c3', phone: '+15550001111' });
    await user.click(submit);
    await waitFor(() => expect(addMock).toHaveBeenCalled());
    expect(addMock.mock.calls[0][0]).toMatchObject({
      phone: '(555) 000-1111',
      recipient_kind: 'manager',
      timezone: 'America/New_York',
    });
  });

  it('renders the full consent language next to the checkbox', async () => {
    render(<SmsDetail onClose={() => {}} />);
    await waitFor(() => expect(listMock).toHaveBeenCalled());
    const label = screen.getByText(
      /agreed to receive account-notification text messages from Occupella/,
    ).textContent;
    // The four required disclosures, with the verbatim Sept-2026 opt-out
    // phrase (A2P code 30539). Must stay in sync with _CONSENT_LANGUAGE in
    // api/routes/sms.py — the server stamps that text into each row's
    // evidence, so drift here means the evidence no longer matches the UI.
    expect(label).toMatch(/Message frequency varies/);
    expect(label).toMatch(/message and data rates may apply/);
    expect(label).toMatch(/Reply STOP to cancel/);
    expect(label).toMatch(/HELP/);
  });

  it('links Terms and Privacy adjacent to the consent checkbox', async () => {
    // A2P unified content policy (rejection code 30564): ToS + Privacy
    // links must sit next to the opt-in checkbox.
    render(<SmsDetail onClose={() => {}} />);
    await waitFor(() => expect(listMock).toHaveBeenCalled());
    const terms = screen.getByRole('link', { name: 'Terms' });
    const privacy = screen.getByRole('link', { name: 'Privacy Policy' });
    // ⚠ RE-POINTED, not relaxed (2026-09-08). These two asserted this app's
    // own host with a bare `/terms` and `/privacy`, which render the LOGIN
    // SCREEN — an SPA answers every path vercel.json does not exclude, and
    // `/terms` is not excluded. (Then the apex, now app.occupella.com — the
    // same-origin path below is right under both.) So the test
    // was pinning the defect: the two links a carrier reviewer is
    // specifically told to click (30564) went to a login form. The
    // same-origin static pages are what SettingsPage already uses.
    expect(terms).toHaveAttribute('href', '/legal/terms.html');
    expect(privacy).toHaveAttribute('href', '/legal/privacy.html');
    // Adjacent means INSIDE the checkbox label, not elsewhere on the card.
    expect(terms.closest('label')).not.toBeNull();
    expect(privacy.closest('label')).not.toBeNull();
  });

  // ── whose number is it (migration 0096) ───────────────────────────
  //
  // The manager digest texts every live manager-kind number, so a departed
  // colleague kept receiving portfolio content. A departure can only revoke
  // a number somebody CLAIMED, and this checkbox is the only thing that
  // makes a claim — server-side it is never inferred from who submitted the
  // form, because that is wrong for every number entered on someone else's
  // behalf.

  it('sends the claim when somebody says the number is their own', async () => {
    addMock.mockResolvedValue({ ...ACTIVE_ROW, owner_user_id: 'u1' });
    render(<SmsDetail onClose={() => {}} />);
    await waitFor(() => expect(listMock).toHaveBeenCalled());
    await userEvent.type(screen.getByLabelText('Phone number'), '5551110000');
    await userEvent.click(screen.getByTestId('sms-own-number'));
    await userEvent.click(screen.getByLabelText('Consent confirmation'));
    await userEvent.click(screen.getByRole('button', { name: /record consent/i }));
    await waitFor(() => expect(addMock).toHaveBeenCalled());
    expect(addMock.mock.calls[0][0]).toMatchObject({ is_own_number: true });
  });

  it('does NOT claim ownership when the box is left alone', async () => {
    // The direction that matters: the default must not quietly record the
    // submitter as the owner, or their departure revokes a colleague's alerts.
    addMock.mockResolvedValue(ACTIVE_ROW);
    render(<SmsDetail onClose={() => {}} />);
    await waitFor(() => expect(listMock).toHaveBeenCalled());
    await userEvent.type(screen.getByLabelText('Phone number'), '5551110000');
    await userEvent.click(screen.getByLabelText('Consent confirmation'));
    await userEvent.click(screen.getByRole('button', { name: /record consent/i }));
    await waitFor(() => expect(addMock).toHaveBeenCalled());
    expect(addMock.mock.calls[0][0]).toMatchObject({ is_own_number: false });
  });

  it('marks a live staff number nobody has claimed', async () => {
    // That row keeps receiving the digest whoever leaves, because the ledger
    // does not know whose it is. Showing it is what lets an admin close the
    // gap; hiding it is how the gap survives.
    listMock.mockResolvedValue([ACTIVE_ROW]);
    render(<SmsDetail onClose={() => {}} />);
    await waitFor(() => expect(listMock).toHaveBeenCalled());
    expect(screen.getByTestId('sms-unattributed')).toBeTruthy();
  });

  it('says nothing about a claimed number, or a revoked one', async () => {
    // Both silences are the point. A marker on every row is decoration, and
    // a REVOKED row is not a live exposure however unattributed it is.
    listMock.mockResolvedValue([
      { ...ACTIVE_ROW, owner_user_id: 'u1' },
      { ...REVOKED_ROW, owner_user_id: null },
      { ...ACTIVE_ROW, id: 'c3', recipient_kind: 'tenant', owner_user_id: null },
    ]);
    render(<SmsDetail onClose={() => {}} />);
    await waitFor(() => expect(listMock).toHaveBeenCalled());
    expect(screen.queryByTestId('sms-unattributed')).toBeNull();
  });

  // ── delivery outcome (checklist #77 / #89) ─────────────────────────
  //
  // `helixis_sms_log` recorded the delivery result from day one and nothing
  // read it. A landline came back `undelivered` with error 30006, was written
  // down, and the number stayed in this table looking healthy.

  it('shows a landline as not delivered, and names the Twilio code', async () => {
    listMock.mockResolvedValue([
      { ...ACTIVE_ROW, last_outcome: 'undelivered', last_error_code: '30006' },
    ]);
    render(<SmsDetail onClose={() => {}} />);
    await waitFor(() => expect(listMock).toHaveBeenCalled());
    const cell = screen.getByTestId('sms-last-outcome');
    expect(cell.textContent).toContain('Not delivered');
    expect(cell.querySelector('[title]')?.getAttribute('title')).toContain('30006');
  });

  it('does not report a never-confirmed message as a failure', async () => {
    // Twilio said NOTHING about this one — a carrier filtered it. Calling that
    // "not delivered" is a claim Twilio never made, and calling it delivered
    // hides the message that vanished. Four values exist for this row.
    listMock.mockResolvedValue([{ ...ACTIVE_ROW, last_outcome: 'no_receipt' }]);
    render(<SmsDetail onClose={() => {}} />);
    await waitFor(() => expect(listMock).toHaveBeenCalled());
    const text = screen.getByTestId('sms-last-outcome').textContent ?? '';
    expect(text).toContain('Never confirmed');
    expect(text).not.toContain('Not delivered');
  });

  it('says "no messages sent" for a number nobody has texted', async () => {
    // The other direction. A number that has never been messaged must not
    // render as healthy OR as failing — both are claims about a send that
    // never happened, and one of them is how the column gets ignored.
    listMock.mockResolvedValue([{ ...ACTIVE_ROW, last_outcome: null }]);
    render(<SmsDetail onClose={() => {}} />);
    await waitFor(() => expect(listMock).toHaveBeenCalled());
    const text = screen.getByTestId('sms-last-outcome').textContent ?? '';
    expect(text).toContain('No messages sent');
    expect(text).not.toContain('Delivered');
  });

  it('shows a delivered message plainly', async () => {
    listMock.mockResolvedValue([{ ...ACTIVE_ROW, last_outcome: 'delivered' }]);
    render(<SmsDetail onClose={() => {}} />);
    await waitFor(() => expect(listMock).toHaveBeenCalled());
    expect(screen.getByTestId('sms-last-outcome').textContent).toContain('Delivered');
  });

});
