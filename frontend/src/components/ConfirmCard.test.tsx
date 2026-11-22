/**
 * PR #75 Bug E — clicking Confirm in the ``<ConfirmCard>`` must POST
 * the user's choice to ``/api/v1/agent/confirm`` so the backend's
 * awaiting ``request_user_confirmation`` future resolves and the
 * destructive action proceeds (or aborts on cancel).
 *
 * Pre-PR-#75 this card never rendered in the live runtime because the
 * orchestrator never emitted ``confirm_gate``; once that wiring landed
 * (commit 1ef81c0) the card finally shows and the click round-trip
 * needs to actually fire.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import { ConfirmCard } from './ConfirmCard';
import { useStore } from '../stores/store';

// ``confirmAction`` lives in services/agent.ts and is the function
// ``ConfirmCard`` calls. We mock it directly rather than mocking
// ``fetch`` so the assertions stay close to the contract under test.
// Default it to the live-resolved path; tests override per-case.
const mockConfirmAction = vi.fn().mockResolvedValue('resolved');
vi.mock('../services/agent', () => ({
  confirmAction: (...args: unknown[]) => mockConfirmAction(...args),
}));

// Toast surface (sonner). The restart-orphaned path warns the user.
const mockToastError = vi.fn();
vi.mock('sonner', () => ({
  toast: { error: (...args: unknown[]) => mockToastError(...args) },
}));

// ``ConfirmCard`` reads the auth token via ``getAccessToken``. Always
// resolve to a deterministic stub so the test doesn't depend on a
// real Supabase session.
vi.mock('../services/auth', () => ({
  getAccessToken: vi.fn().mockResolvedValue('test-token'),
}));

describe('ConfirmCard (PR #75 Bug E confirm round-trip)', () => {
  beforeEach(() => {
    mockConfirmAction.mockReset();
    mockConfirmAction.mockResolvedValue('resolved');
    mockToastError.mockReset();
    useStore.setState({
      pendingConfirmation: {
        confirmId: 'cid-abc-123',
        action: 'send_email',
        title: 'Send 3 collection emails?',
        details: 'To Sarah, Maria, and James.',
        items: ['Sarah Chen', 'Maria Lopez', 'James Chen'],
      },
    });
  });

  afterEach(() => {
    useStore.setState({ pendingConfirmation: null });
  });

  it('POSTs approved=true with the confirm_id when the user clicks Approve', async () => {
    render(<ConfirmCard />);
    fireEvent.click(screen.getByText('Approve'));
    // The card resolves the access token before calling confirmAction.
    await Promise.resolve();
    await Promise.resolve();
    expect(mockConfirmAction).toHaveBeenCalledTimes(1);
    // edit-before-approve: approve now always passes the (reason, edits)
    // tail — no editable fields on this gate, so edits is empty.
    expect(mockConfirmAction).toHaveBeenCalledWith(
      'cid-abc-123',
      true,
      'test-token',
      undefined,
      {},
    );
  });

  it('POSTs approved=false after the two-step cancel (reason optional)', async () => {
    render(<ConfirmCard />);
    // First press reveals the optional-reason input — nothing sent yet
    // (approval-pipeline PR).
    fireEvent.click(screen.getByText('Cancel'));
    expect(mockConfirmAction).not.toHaveBeenCalled();
    expect(screen.getByTestId('confirm-gate-reason')).toBeInTheDocument();
    // Second press ("Don't run") sends the decline with the typed reason.
    fireEvent.change(screen.getByTestId('confirm-gate-reason'), {
      target: { value: 'wrong vendor' },
    });
    fireEvent.click(screen.getByText("Don't run"));
    await Promise.resolve();
    await Promise.resolve();
    expect(mockConfirmAction).toHaveBeenCalledTimes(1);
    expect(mockConfirmAction).toHaveBeenCalledWith(
      'cid-abc-123',
      false,
      'test-token',
      'wrong vendor',
    );
  });

  it('warns the user when an approval is orphaned by a restart', async () => {
    mockConfirmAction.mockResolvedValue('orphaned');
    render(<ConfirmCard />);
    fireEvent.click(screen.getByText('Approve'));
    // token resolve → confirmAction resolve → toast
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    expect(mockToastError).toHaveBeenCalledTimes(1);
    expect(String(mockToastError.mock.calls[0][0])).toMatch(/restart/i);
  });

  it('does not warn on the normal resolved path', async () => {
    mockConfirmAction.mockResolvedValue('resolved');
    render(<ConfirmCard />);
    fireEvent.click(screen.getByText('Approve'));
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    expect(mockToastError).not.toHaveBeenCalled();
  });

  it('renders nothing when no confirmation is pending', () => {
    useStore.setState({ pendingConfirmation: null });
    const { container } = render(<ConfirmCard />);
    expect(container.firstChild).toBeNull();
  });

  it('exposes confirm-gate test ids so the live E2E harness can find + cancel the gate', () => {
    // The playground auto-cancels any surfaced write by these test ids; without
    // them on the actually-mounted card its "never let a write through" safety
    // silently no-ops (#7 F6). Keep them in lockstep with ConfirmGate.tsx.
    render(<ConfirmCard />);
    expect(screen.getByTestId('confirm-gate')).toBeTruthy();
    expect(screen.getByTestId('confirm-gate-cancel')).toBeTruthy();
    expect(screen.getByTestId('confirm-gate-confirm')).toBeTruthy();
  });
});

describe('ConfirmCard SMS phone-bubble preview (UI/UX day, 2026-08-19)', () => {
  afterEach(() => {
    useStore.setState({ pendingConfirmation: null });
  });

  const smsPending = {
    confirmId: 'cid-sms-1',
    action: 'send_text_message',
    title: 'Send this text message?',
    details: 'Sends the rent_reminder template to Maria Alvarez.',
    items: ['To: Maria Alvarez (···4471)', 'Template: rent_reminder'],
    requestBody: {
      to: 'Maria Alvarez (···4471)',
      template_id: 'rent_reminder',
      message:
        'Occupella: Hi Maria, your rent of $1,840.00 is due Sep 1. ' +
        'Reply STOP to unsubscribe, HELP for help.',
    },
  };

  it('renders the exact rendered message as a phone bubble with recipient + meta', () => {
    useStore.setState({ pendingConfirmation: smsPending });
    render(<ConfirmCard />);
    expect(screen.getByTestId('confirm-gate-sms-bubble').textContent).toBe(
      smsPending.requestBody.message,
    );
    expect(screen.getByTestId('confirm-gate-sms-preview')).toHaveTextContent(
      'Text to Maria Alvarez (···4471)',
    );
    // 116 chars → 1 segment.
    expect(screen.getByTestId('confirm-gate-sms-meta')).toHaveTextContent('1 segment');
  });

  it('suppresses the raw JSON dump for SMS gates (the bubble IS the payload view)', () => {
    useStore.setState({ pendingConfirmation: smsPending });
    render(<ConfirmCard />);
    expect(screen.queryByTestId('confirm-gate-request-body')).toBeNull();
  });

  it('counts concatenated segments for long messages', () => {
    useStore.setState({
      pendingConfirmation: {
        ...smsPending,
        requestBody: { ...smsPending.requestBody, message: 'x'.repeat(320) },
      },
    });
    render(<ConfirmCard />);
    expect(screen.getByTestId('confirm-gate-sms-meta')).toHaveTextContent(
      '320 characters · 3 segments',
    );
  });

  it('renders no bubble for non-SMS actions even when a message field exists', () => {
    useStore.setState({
      pendingConfirmation: {
        ...smsPending,
        action: 'buildium_create_work_order',
      },
    });
    render(<ConfirmCard />);
    expect(screen.queryByTestId('confirm-gate-sms-bubble')).toBeNull();
    // Non-SMS gates keep the raw request-body view.
    expect(screen.getByTestId('confirm-gate-request-body')).toBeInTheDocument();
  });
});

describe('ConfirmCard edit-before-approve (UI/UX day item 1)', () => {
  beforeEach(() => {
    mockConfirmAction.mockReset();
    mockConfirmAction.mockResolvedValue('resolved');
  });
  afterEach(() => {
    useStore.setState({ pendingConfirmation: null });
  });

  const editablePending = {
    confirmId: 'cid-edit-1',
    action: 'GMAIL_SEND_EMAIL',
    title: 'Confirm this action?',
    details: 'Send an email to Sarah.',
    items: [],
    requestBody: { subject: 'Renewal', body: 'Hi Sarah — original draft.' },
    editableFields: ['body', 'subject'],
  };

  it('renders prefilled editors for the declared editable fields only', () => {
    useStore.setState({ pendingConfirmation: editablePending });
    render(<ConfirmCard />);
    expect(screen.getByTestId('confirm-gate-edit-body')).toHaveValue(
      'Hi Sarah — original draft.',
    );
    expect(screen.getByTestId('confirm-gate-edit-subject')).toHaveValue('Renewal');
    // The editors replace the raw JSON dump.
    expect(screen.queryByTestId('confirm-gate-request-body')).toBeNull();
  });

  it('sends only the CHANGED fields as edits on approve', async () => {
    useStore.setState({ pendingConfirmation: editablePending });
    render(<ConfirmCard />);
    fireEvent.change(screen.getByTestId('confirm-gate-edit-body'), {
      target: { value: 'Hi Sarah — my edited version.' },
    });
    fireEvent.click(screen.getByTestId('confirm-gate-confirm'));
    await waitFor(() => expect(mockConfirmAction).toHaveBeenCalled());
    const call = mockConfirmAction.mock.calls[0];
    expect(call[1]).toBe(true);
    expect(call[4]).toEqual({ body: 'Hi Sarah — my edited version.' });
  });

  it('sends no edits when nothing changed', async () => {
    useStore.setState({ pendingConfirmation: editablePending });
    render(<ConfirmCard />);
    fireEvent.click(screen.getByTestId('confirm-gate-confirm'));
    await waitFor(() => expect(mockConfirmAction).toHaveBeenCalled());
    expect(mockConfirmAction.mock.calls[0][4]).toEqual({});
  });

  it('renders no editors for a read-only gate (e.g. SMS)', () => {
    useStore.setState({
      pendingConfirmation: {
        confirmId: 'cid-sms-2',
        action: 'send_text_message',
        title: 'Send this text message?',
        details: 'x',
        items: [],
        requestBody: { message: 'Occupella: hi.' },
      },
    });
    render(<ConfirmCard />);
    expect(screen.queryByTestId('confirm-gate-editors')).toBeNull();
  });
});

describe('ConfirmCard keyboard safety (design-review fix, 2026-08-18)', () => {
  beforeEach(() => {
    mockConfirmAction.mockReset();
    mockConfirmAction.mockResolvedValue('resolved');
  });
  afterEach(() => {
    useStore.setState({ pendingConfirmation: null });
  });

  it('a bare Enter does NOT approve — approval requires the ⌘/Ctrl chord', async () => {
    useStore.setState({
      pendingConfirmation: {
        confirmId: 'cid-key-1',
        action: 'buildium_create_work_order',
        title: 'Create work order?',
        details: 'x',
        items: [],
      },
    });
    render(<ConfirmCard />);
    fireEvent.keyDown(window, { key: 'Enter' });
    await new Promise((r) => setTimeout(r, 10));
    expect(mockConfirmAction).not.toHaveBeenCalled();

    fireEvent.keyDown(window, { key: 'Enter', ctrlKey: true });
    await waitFor(() => expect(mockConfirmAction).toHaveBeenCalled());
    expect(mockConfirmAction.mock.calls[0][1]).toBe(true);
  });
});
