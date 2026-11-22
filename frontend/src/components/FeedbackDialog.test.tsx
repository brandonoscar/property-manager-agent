/**
 * The only support channel inside the product, which had no tests at all.
 *
 * ⚠ THE BUG THIS FILE EXISTS FOR: `submitFeedback` never checked `res.ok`.
 * `apiFetch` resolves with the Response whatever the status — every other
 * caller in `services/` tests it — so a 422 or a 500 came back as a resolved
 * promise, the dialog ran its success path, and the person was told
 * *"Sent — thanks. We read these and reply by the next business day"* for a
 * report that never arrived. A failure nobody can perceive, wearing the costume
 * of a success, in the one place somebody goes when everything else has failed.
 *
 * ⚠ The bound is the SERVER's: `message` is `min_length=3, max_length=5000` in
 * `api/routes/feedback.py`. So a long report — the most valuable kind, and the
 * one somebody spent ten minutes writing — was composed happily and rejected,
 * with "please try again" as the advice, which is wrong forever.
 *
 * Every assertion below is about what a PERSON can perceive: text on screen, a
 * call that happened or did not, a toast. Asserting an internal validity flag
 * would pass against the broken version, because the broken version computed it
 * correctly and then said nothing.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

vi.mock('../services/feedback', async () => {
  const actual =
    await vi.importActual<typeof import('../services/feedback')>('../services/feedback');
  return { ...actual, submitFeedback: vi.fn() };
});

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn(), message: vi.fn() },
}));

import { toast } from 'sonner';
import {
  FeedbackRejected,
  MAX_MESSAGE_CHARS,
  MIN_MESSAGE_CHARS,
  submitFeedback,
} from '../services/feedback';
import { FeedbackDialog } from './FeedbackDialog';

const send = vi.mocked(submitFeedback);

function open(onClose = vi.fn()) {
  render(<FeedbackDialog onClose={onClose} />);
  return { onClose, box: screen.getByRole('textbox') as HTMLTextAreaElement };
}

function type(box: HTMLTextAreaElement, value: string) {
  fireEvent.change(box, { target: { value } });
}

function clickSend() {
  fireEvent.click(screen.getByRole('button', { name: 'Send' }));
}

beforeEach(() => {
  vi.clearAllMocks();
  send.mockResolvedValue(undefined);
});

describe('a report that fails to send', () => {
  it('does NOT tell the person it was sent', async () => {
    // The headline defect. Before the fix this test fails on both assertions:
    // the toast fires and the dialog closes on a report that never landed.
    send.mockRejectedValue(new Error('Feedback POST failed with 500'));
    const { onClose, box } = open();
    type(box, 'the chat went blank halfway through an answer');
    clickSend();

    await screen.findByText(/Couldn't send that/);
    expect(toast.success).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('keeps what they wrote so it is not lost', async () => {
    send.mockRejectedValue(new Error('boom'));
    const { box } = open();
    type(box, 'a long and carefully written report');
    clickSend();

    await screen.findByText(/Couldn't send that/);
    expect(box.value).toBe('a long and carefully written report');
  });

  it('points at a route that works when ours does not', async () => {
    send.mockRejectedValue(new Error('boom'));
    const { box } = open();
    type(box, 'something broke');
    clickSend();
    await screen.findByText(/team@occupella\.com/);
  });
});

describe('a report the server will never accept', () => {
  it('does not tell them to try again, because trying again cannot work', async () => {
    send.mockRejectedValue(new FeedbackRejected());
    const { box } = open();
    type(box, 'x'.repeat(20));
    clickSend();

    await screen.findByText(/can't be sent as written/);
    expect(screen.queryByText(/please try again/)).toBeNull();
  });
});

describe('refusals a person can see', () => {
  it('says why a too-short message did not send, instead of doing nothing', async () => {
    // The old guard returned silently, so the button moved and nothing
    // happened — indistinguishable from a send that failed (gotcha 44).
    const { box } = open();
    type(box, 'x'.repeat(MIN_MESSAGE_CHARS - 1));
    clickSend();

    await screen.findByText(new RegExp(`at least ${MIN_MESSAGE_CHARS} characters`));
    expect(send).not.toHaveBeenCalled();
  });

  it('says how far over the limit a long message is', async () => {
    const { box } = open();
    type(box, 'x'.repeat(MAX_MESSAGE_CHARS + 12));
    clickSend();

    await screen.findByText(/12 characters over the limit/);
    expect(send).not.toHaveBeenCalled();
  });

  it('leaves the button live so the click can be explained', () => {
    // A disabled button swallows the click before any code runs, and then
    // there is nowhere to put the reason — InviteUsersDialog's rule, applied
    // to the same defect here.
    open();
    expect(screen.getByRole('button', { name: 'Send' })).not.toBeDisabled();
  });
});

describe('the character counter', () => {
  it('stays out of the way until the limit is in sight', () => {
    const { box } = open();
    type(box, 'x'.repeat(100));
    expect(screen.queryByTestId('feedback-counter')).toBeNull();
  });

  it('appears as the limit approaches', () => {
    const { box } = open();
    type(box, 'x'.repeat(MAX_MESSAGE_CHARS - 100));
    expect(screen.getByTestId('feedback-counter')).toBeTruthy();
  });

  it('counts the overflow once past it', () => {
    const { box } = open();
    type(box, 'x'.repeat(MAX_MESSAGE_CHARS + 5));
    expect(screen.getByTestId('feedback-counter').textContent).toMatch(/5 over the/);
  });
});

describe('a report that sends', () => {
  it('closes, thanks them, and passes what they wrote', async () => {
    const { onClose, box } = open();
    type(box, 'the properties table header does not stick');
    clickSend();

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    // ⚠ The last two are spelled out rather than `expect.anything()`, which
    // does not match `null` — and the session id is legitimately null with no
    // chat thread open. A matcher that rejects the real value would have
    // failed here for a reason unrelated to the behaviour being pinned.
    expect(send).toHaveBeenCalledWith(
      'bug',
      'the properties table header does not stick',
      'chat',
      null,
    );
    expect(toast.success).toHaveBeenCalled();
  });
});
