/**
 * The Leasing section must not open until texting is legally allowed.
 *
 * Two halves, and the second is the one that actually binds:
 *   • this panel, which explains the blocker;
 *   • the backend refusal (`comms/sms.py::outbound_channel_block`, consulted
 *     by `POST /crm/leads/{id}/reply`), pinned in `tests/unit/
 *     test_crm_a2p_gate.py`.
 *
 * A UI gate cannot bind an API, so these tests deliberately do NOT stand in
 * for that one. What they pin is that the panel fails CLOSED — every
 * unknown, every probe failure, and the whole first paint.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';

import { leasingBlock } from './LeasingComplianceGate';
import type { SmsStatus, TwilioNumber } from '../services/sms';

// ⚠ Annotated, not inferred. Left to inference this is the narrow literal
// shape and every `{ ...NUMBER, forward_to_number: … }` in the file is a type
// error that `vitest` cannot see — it does not typecheck, so the suite goes
// green on a file `tsc` rejects (gotcha 64, in the direction that hides a
// broken test rather than a broken mutant).
const NUMBER: TwilioNumber = {
  id: 'n1',
  phone_number: '+17775550001',
  label: null,
  created_at: null,
};

const LIVE: SmsStatus = {
  enabled: true,
  configured: true,
  consent_count: 3,
  revoked_count: 0,
};

describe('leasingBlock — the pure decision', () => {
  it('opens only when the caller is manager+ AND the channel is live', () => {
    expect(leasingBlock('manager', LIVE, [NUMBER])).toBeNull();
    expect(leasingBlock('admin', LIVE, [NUMBER])).toBeNull();
  });

  it('blocks a member even on a fully approved channel', () => {
    expect(leasingBlock('member', LIVE, [NUMBER])).toBe('role');
  });

  it('blocks a configured-but-unapproved channel', () => {
    // The defect this whole change exists for: credentials present, carrier
    // approval absent. `configured` alone was the old check.
    expect(leasingBlock('admin', { ...LIVE, enabled: false }, [NUMBER])).toBe('disabled');
  });

  it('reports the carrier blocker ahead of the config blocker', () => {
    // "Finish setting up Twilio" is the wrong instruction for someone whose
    // real blocker is approval. Mirrors the backend's ordering.
    expect(
      leasingBlock('admin', { ...LIVE, enabled: false, configured: false }, [NUMBER]),
    ).toBe('disabled');
  });

  it('blocks an approved-but-unconfigured channel', () => {
    // ⚠ `block` is the backend predicate's answer, and it is what decides.
    // `configured` alone no longer blocks, deliberately: it reports whether a
    // DEPLOYMENT-GLOBAL sender exists, which on a correct white-label install
    // is `false` for everyone (each company sends from its own number —
    // gotcha 69). Blocking on it would close Leasing for every customer.
    expect(
      leasingBlock('admin', { ...LIVE, enabled: false, block: 'not_configured' }, [NUMBER]),
    ).toBe('not_configured');
    // The multi-tenant state: no global sender, but this company may send.
    expect(leasingBlock('admin', { ...LIVE, configured: false, block: '' }, [NUMBER])).toBeNull();
  });

  it('separates a campaign in review from one that was rejected', () => {
    // Same `block` value, opposite instructions: one is a 10-15 day wait with
    // nothing to do, the other needs the form fixed and resubmitted. Only
    // `campaign_status` tells them apart.
    const pending = {
      ...LIVE,
      enabled: false,
      block: 'campaign_pending' as const,
      campaign_status: 'campaign_pending',
    };
    expect(leasingBlock('admin', pending, [NUMBER])).toBe('campaign_pending');
    expect(leasingBlock('admin', { ...pending, campaign_status: 'failed' }, [NUMBER])).toBe(
      'campaign_failed',
    );
    // ⚠ 'draft' means they never filled the form in. Telling them to wait on
    // the carriers would be an instruction to do nothing, forever.
    expect(leasingBlock('admin', { ...pending, campaign_status: 'draft' }, [NUMBER])).toBe(
      'disabled',
    );
    expect(leasingBlock('admin', { ...pending, campaign_status: '' }, [NUMBER])).toBe('disabled');
  });

  it('fails closed when an older backend sends no block reason', () => {
    // A cached bundle against a newer API, or the reverse. No reason plus
    // `enabled: false` must land on the most conservative panel rather than
    // falling through to an open pipeline.
    expect(leasingBlock('admin', { ...LIVE, enabled: false }, [NUMBER])).toBe('disabled');
  });

  it('fails closed while either probe is still outstanding', () => {
    // An unknown role that renders the pipeline is open for the whole first
    // paint — the window is short and entirely sufficient.
    expect(leasingBlock(null, LIVE, [NUMBER])).toBe('loading');
    expect(leasingBlock('admin', null, [NUMBER])).toBe('loading');
    expect(leasingBlock(null, null, [NUMBER])).toBe('loading');
  });

  it('never opens on zero consent alone', () => {
    // Consent is per-recipient and enforced at send time. Blocking the whole
    // section on a company-wide count would permanently lock out a new
    // account that has not texted anyone yet — a fresh bug wearing a
    // compliance costume.
    expect(leasingBlock('manager', { ...LIVE, consent_count: 0 }, [NUMBER])).toBeNull();
  });
});

describe('LeasingComplianceGate — rendering', () => {
  beforeEach(() => {
    vi.resetModules();
  });
  afterEach(() => {
    vi.doUnmock('../services/sms');
    vi.doUnmock('../services/company');
    vi.resetModules();
    vi.restoreAllMocks();
  });

  async function renderGate(
    status: SmsStatus | Error,
    role: 'admin' | 'member' = 'admin',
    numbers: TwilioNumber[] = [NUMBER],
  ) {
    vi.doMock('../services/sms', async () => {
      const actual = await vi.importActual<typeof import('../services/sms')>('../services/sms');
      return {
        ...actual,
        fetchSmsStatus:
          status instanceof Error
            ? vi.fn().mockRejectedValue(status)
            : vi.fn().mockResolvedValue(status),
        listTwilioNumbers: vi.fn().mockResolvedValue(numbers),
        provisionTwilioNumber: vi.fn().mockResolvedValue({
          number: NUMBER,
          purchased: true,
        }),
      };
    });
    vi.doMock('../services/company', async () => {
      const actual =
        await vi.importActual<typeof import('../services/company')>('../services/company');
      return {
        ...actual,
        getMyRole: vi.fn().mockResolvedValue({
          company_id: 'c1',
          role,
          is_primary_admin: role === 'admin',
        }),
      };
    });
    const { LeasingComplianceGate } = await import('./LeasingComplianceGate');
    const { useStore } = await import('../stores/store');
    act(() => {
      useStore.getState().setCompanyRole(null);
    });
    await act(async () => {
      render(
        <LeasingComplianceGate>
          <div data-testid="pipeline">the pipeline</div>
        </LeasingComplianceGate>,
      );
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
  }

  it('renders the pipeline when the channel is live', async () => {
    await renderGate(LIVE);
    expect(screen.getByTestId('pipeline')).toBeInTheDocument();
    expect(screen.queryByTestId('leasing-compliance-gate')).not.toBeInTheDocument();
  });

  it('hides the pipeline behind the setup panel while awaiting approval', async () => {
    await renderGate({ ...LIVE, enabled: false });
    expect(screen.queryByTestId('pipeline')).not.toBeInTheDocument();
    const gate = screen.getByTestId('leasing-compliance-gate');
    expect(gate.getAttribute('data-block')).toBe('disabled');
    // ⚠ White-label: there is no "connect the carrier" step any more, and its
    // absence is the property worth pinning. It asked the customer for an
    // account SID and auth token that are ours — a checklist item they could
    // never complete. The carrier step is the live blocker and stays.
    expect(screen.queryByTestId('leasing-step-configured')).not.toBeInTheDocument();
    expect(screen.getByTestId('leasing-step-approved').textContent).toContain(
      'Registered and waiting',
    );
    // No user-visible string in the gate may name the carrier.
    expect(screen.getByTestId('leasing-compliance-gate').textContent).not.toContain('Twilio');
  });

  it('shows a brand-new company the form and no progress checklist', async () => {
    // ⚠ The checklist is PROGRESS, and a company that has submitted nothing
    // has none. It used to render for everyone, below the whole registration
    // form: a first-run customer scrolled fourteen empty fields to reach two
    // items telling them nothing was done yet, under a heading already saying
    // exactly that. Before the submission there is one thing to do and the
    // form is it.
    await renderGate({ ...LIVE, enabled: false, configured: false, consent_count: 0 });
    expect(screen.getByTestId('leasing-a2p-registration')).toBeInTheDocument();
    expect(screen.queryByTestId('leasing-step-approved')).not.toBeInTheDocument();
    expect(screen.queryByTestId('leasing-step-consent')).not.toBeInTheDocument();
  });

  it('stays closed when the status probe fails', async () => {
    // A probe that could not answer has not proved the channel is approved.
    // Opening on an unreadable status is the permissive direction, which is
    // the one that ships a violation.
    await renderGate(new Error('network'));
    expect(screen.queryByTestId('pipeline')).not.toBeInTheDocument();
    expect(screen.getByTestId('leasing-compliance-gate')).toBeInTheDocument();
  });

  it('tells a member why, without a setup checklist they cannot action', async () => {
    await renderGate(LIVE, 'member');
    expect(screen.queryByTestId('pipeline')).not.toBeInTheDocument();
    expect(screen.getByTestId('leasing-compliance-gate').getAttribute('data-block')).toBe('role');
    expect(screen.queryByTestId('leasing-step-approved')).not.toBeInTheDocument();
  });
});

describe('the per-company number blocker', () => {
  it('blocks a company that has no number of its own', () => {
    // Everything deployment-wide is green; this company still cannot send,
    // because a shared sender routes the recipient's reply to whoever owns
    // the number they can see.
    expect(leasingBlock('admin', LIVE, [])).toBe('no_number');
  });

  it('is the LAST blocker — the others cannot be fixed from this screen', () => {
    // A company with no number AND an unapproved campaign must be told about
    // the campaign: buying a number would not unblock them, and the purchase
    // costs money.
    expect(leasingBlock('admin', { ...LIVE, enabled: false }, [])).toBe('disabled');
    expect(
      leasingBlock('admin', { ...LIVE, enabled: false, block: 'not_configured' }, []),
    ).toBe('not_configured');
    expect(leasingBlock('member', LIVE, [])).toBe('role');
  });

  it('opens once the company has a number', () => {
    expect(leasingBlock('manager', LIVE, [NUMBER])).toBeNull();
  });

  it('fails closed while the number list is unknown', () => {
    // ⚠ NOT the same as an empty list. "We could not read your numbers" must
    // never render "you have none — buy one", which invites a duplicate
    // purchase for a company that already has one.
    expect(leasingBlock('admin', LIVE, null)).toBe('loading');
  });
});

describe('in-app number setup', () => {
  beforeEach(() => {
    vi.resetModules();
  });
  afterEach(() => {
    vi.doUnmock('../services/sms');
    vi.doUnmock('../services/company');
    vi.resetModules();
    vi.restoreAllMocks();
  });

  async function renderSetup(provision = vi.fn().mockResolvedValue({ number: NUMBER, purchased: true })) {
    vi.doMock('../services/sms', async () => {
      const actual = await vi.importActual<typeof import('../services/sms')>('../services/sms');
      return {
        ...actual,
        fetchSmsStatus: vi.fn().mockResolvedValue(LIVE),
        listTwilioNumbers: vi.fn().mockResolvedValue([]), // no number yet
        provisionTwilioNumber: provision,
      };
    });
    vi.doMock('../services/company', async () => {
      const actual =
        await vi.importActual<typeof import('../services/company')>('../services/company');
      return {
        ...actual,
        getMyRole: vi.fn().mockResolvedValue({
          company_id: 'c1',
          role: 'admin',
          is_primary_admin: true,
        }),
      };
    });
    const { LeasingComplianceGate } = await import('./LeasingComplianceGate');
    const { useStore } = await import('../stores/store');
    act(() => {
      useStore.getState().setCompanyRole(null);
    });
    await act(async () => {
      render(
        <LeasingComplianceGate>
          <div data-testid="pipeline">the pipeline</div>
        </LeasingComplianceGate>,
      );
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    return provision;
  }

  it('offers setup in-app rather than sending the user to Twilio', async () => {
    await renderSetup();
    expect(screen.getByTestId('leasing-number-setup')).toBeInTheDocument();
    expect(screen.getByTestId('leasing-get-number')).toBeInTheDocument();
    expect(screen.queryByTestId('pipeline')).not.toBeInTheDocument();
  });

  it('does NOT reserve a number on the first click', async () => {
    // ⚠ The two-step confirm STAYS; only its justification changed. It used to
    // be "this is a recurring charge to you". White-labeled, the customer is
    // not billed by the carrier — but the click still reserves a real phone
    // number on a real network, which is outward-facing and not cleanly
    // undoable. A single button would make that a one-click accident.
    const provision = await renderSetup();
    await act(async () => {
      fireEvent.click(screen.getByTestId('leasing-get-number'));
    });
    expect(provision).not.toHaveBeenCalled();
    // …and what will happen is stated BEFORE the commit, not after.
    expect(screen.getByTestId('leasing-confirm-number')).toBeInTheDocument();
    const panel = screen.getByTestId('leasing-number-setup').textContent ?? '';
    expect(panel).toContain('dedicated number');
    // ⚠ The old copy quoted Twilio's $1.15 line rate. That named a vendor the
    // customer has no relationship with and a charge they will never see, so
    // asserting its ABSENCE is what keeps it from creeping back in.
    expect(panel).not.toContain('Twilio');
    expect(panel).not.toContain('1.15');
  });

  it('buys and opens the pipeline once confirmed', async () => {
    const provision = await renderSetup();
    await act(async () => {
      fireEvent.click(screen.getByTestId('leasing-get-number'));
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId('leasing-confirm-number'));
    });
    expect(provision).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('pipeline')).toBeInTheDocument();
  });

  it('stays on the setup screen when the purchase fails', async () => {
    // A failed purchase must not look like a completed one — opening the
    // pipeline here would leave every send failing with no explanation.
    await renderSetup(vi.fn().mockRejectedValue(new Error('Twilio said no')));
    await act(async () => {
      fireEvent.click(screen.getByTestId('leasing-get-number'));
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId('leasing-confirm-number'));
    });
    expect(screen.queryByTestId('pipeline')).not.toBeInTheDocument();
    expect(screen.getByTestId('leasing-number-setup')).toBeInTheDocument();
  });

  it('passes the typed area code through', async () => {
    const provision = await renderSetup();
    await act(async () => {
      fireEvent.change(screen.getByTestId('leasing-area-code'), { target: { value: '512' } });
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId('leasing-get-number'));
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId('leasing-confirm-number'));
    });
    expect(provision).toHaveBeenCalledWith('512', '');
  });

  it('passes a typed forwarding number through with the purchase', async () => {
    // The one moment a human is present. A number bought by the A2P sweep on
    // carrier approval has nobody to ask, which is what the standing prompt
    // below covers — but when someone IS here, ask once and be done.
    const provision = await renderSetup();
    await act(async () => {
      fireEvent.change(screen.getByTestId('leasing-forward-to'), {
        target: { value: '(512) 555-0143' },
      });
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId('leasing-get-number'));
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId('leasing-confirm-number'));
    });
    expect(provision).toHaveBeenCalledWith('', '(512) 555-0143');
  });
});

describe('a number that rings nowhere', () => {
  // ⚠ THE GAP. `forward_to_number` has been a column since 0083 and was
  // writable from nowhere, and the A2P sweep buys a number unattended the
  // moment carriers approve — so a company could finish setup, see its number
  // on screen, and have every call go to voicemail with no surface to change
  // it. This prompt is that surface.
  beforeEach(() => {
    vi.resetModules();
  });
  afterEach(() => {
    vi.doUnmock('../services/sms');
    vi.doUnmock('../services/company');
    vi.resetModules();
    vi.restoreAllMocks();
  });

  async function renderOpen(numbers: TwilioNumber[]) {
    const save = vi.fn().mockResolvedValue({ ...numbers[0], forward_to_number: '+15125550143' });
    vi.doMock('../services/sms', async () => {
      const actual = await vi.importActual<typeof import('../services/sms')>('../services/sms');
      return {
        ...actual,
        fetchSmsStatus: vi.fn().mockResolvedValue(LIVE),
        listTwilioNumbers: vi.fn().mockResolvedValue(numbers),
        saveTwilioNumber: save,
      };
    });
    vi.doMock('../services/company', async () => {
      const actual =
        await vi.importActual<typeof import('../services/company')>('../services/company');
      return {
        ...actual,
        getMyRole: vi.fn().mockResolvedValue({
          company_id: 'c1',
          role: 'admin',
          is_primary_admin: true,
        }),
      };
    });
    const { LeasingComplianceGate } = await import('./LeasingComplianceGate');
    const { useStore } = await import('../stores/store');
    act(() => {
      useStore.getState().setCompanyRole(null);
    });
    await act(async () => {
      render(
        <LeasingComplianceGate>
          <div data-testid="pipeline">the pipeline</div>
        </LeasingComplianceGate>,
      );
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    return save;
  }

  it('asks where calls should ring, without blocking the pipeline', async () => {
    // ⚠ Advisory, never a gate. Voicemail is a legitimate choice and the
    // column cannot tell "never set" from "deliberately voicemail", so this
    // must never stand between anyone and their leads.
    await renderOpen([NUMBER]);
    expect(screen.getByTestId('leasing-forwarding-prompt')).toBeInTheDocument();
    expect(screen.getByTestId('pipeline')).toBeInTheDocument();
  });

  it('stays quiet once the number forwards somewhere', async () => {
    await renderOpen([{ ...NUMBER, forward_to_number: '+15125550143' }]);
    expect(screen.queryByTestId('leasing-forwarding-prompt')).not.toBeInTheDocument();
  });

  it('saves the typed number and then stops asking', async () => {
    const save = await renderOpen([NUMBER]);
    await act(async () => {
      fireEvent.change(screen.getByTestId('leasing-forwarding-input'), {
        target: { value: '(512) 555-0143' },
      });
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId('leasing-forwarding-save'));
    });
    expect(save).toHaveBeenCalledWith({
      phone_number: NUMBER.phone_number,
      forward_to_number: '(512) 555-0143',
    });
    // The saved row replaces the stale one, so the prompt clears without a
    // reload — a nudge that persists after you answered it reads as broken.
    expect(screen.queryByTestId('leasing-forwarding-prompt')).not.toBeInTheDocument();
  });

  it('takes "voicemail is fine" for an answer', async () => {
    const save = await renderOpen([NUMBER]);
    await act(async () => {
      fireEvent.click(screen.getByTestId('leasing-forwarding-dismiss'));
    });
    expect(screen.queryByTestId('leasing-forwarding-prompt')).not.toBeInTheDocument();
    expect(save).not.toHaveBeenCalled();
  });

  it('keeps asking when the save fails', async () => {
    // A failed save must not read as a completed one — the phone still rings
    // nowhere and the only signal is this prompt.
    vi.resetModules();
    const save = vi.fn().mockRejectedValue(new Error('nope'));
    vi.doMock('../services/sms', async () => {
      const actual = await vi.importActual<typeof import('../services/sms')>('../services/sms');
      return {
        ...actual,
        fetchSmsStatus: vi.fn().mockResolvedValue(LIVE),
        listTwilioNumbers: vi.fn().mockResolvedValue([NUMBER]),
        saveTwilioNumber: save,
      };
    });
    vi.doMock('../services/company', async () => {
      const actual =
        await vi.importActual<typeof import('../services/company')>('../services/company');
      return {
        ...actual,
        getMyRole: vi.fn().mockResolvedValue({ company_id: 'c1', role: 'admin' }),
      };
    });
    const { LeasingComplianceGate } = await import('./LeasingComplianceGate');
    const { useStore } = await import('../stores/store');
    act(() => {
      useStore.getState().setCompanyRole(null);
    });
    await act(async () => {
      render(
        <LeasingComplianceGate>
          <div data-testid="pipeline">the pipeline</div>
        </LeasingComplianceGate>,
      );
      await new Promise((r) => setTimeout(r, 20));
    });
    await act(async () => {
      fireEvent.change(screen.getByTestId('leasing-forwarding-input'), {
        target: { value: '(512) 555-0143' },
      });
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId('leasing-forwarding-save'));
    });
    expect(screen.getByTestId('leasing-forwarding-prompt')).toBeInTheDocument();
  });
});

describe('the plan blocker — Leasing is a paid capability', () => {
  it('closes the pipeline for a plan that does not carry it', () => {
    expect(leasingBlock('admin', LIVE, [NUMBER], false)).toBe('plan');
  });

  it('opens it for a plan that does', () => {
    expect(leasingBlock('admin', LIVE, [NUMBER], true)).toBeNull();
  });

  it('is the WALL BEHIND the role check, never in front of it', () => {
    // Telling a member to upgrade the company's plan is an instruction they
    // cannot carry out — an admin has to. The nearer wall wins.
    expect(leasingBlock('member', LIVE, [NUMBER], false)).toBe('role');
  });

  it('comes BEFORE every setup step', () => {
    // The alternative walks a Starter customer through carrier registration
    // for a section their plan does not carry: they finish a compliance flow
    // and are refused anyway. Nothing here is worth doing first.
    expect(leasingBlock('admin', { ...LIVE, enabled: false }, [], false)).toBe('plan');
  });

  it('waits rather than guessing while the billing read is outstanding', () => {
    expect(leasingBlock('admin', LIVE, [NUMBER], null)).toBe('loading');
  });

  it('defaults to INCLUDED when the argument is omitted', () => {
    // ⚠ Fail OPEN, unlike every other input here — those guard a carrier
    // obligation, this guards revenue. `gate.py` rule 1: a database blip must
    // not black out a section a Pro customer is paying for. The component
    // passes `true` on a failed read for the same reason.
    expect(leasingBlock('admin', LIVE, [NUMBER])).toBeNull();
  });
});
