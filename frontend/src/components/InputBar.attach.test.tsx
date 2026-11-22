/**
 * Attaching a file: the drop zone, the preview, and what Send does with it.
 *
 * ⚠ THE BEHAVIOUR CHANGE THIS PINS. Picking a file used to upload it
 * instantly, so the first time anybody saw WHICH file they had chosen was in
 * the transcript, after it had cost a request and a chat turn. Adding a drop
 * zone made that unacceptable rather than merely untidy: a drop is one
 * careless drag away from happening by accident, and an accidental upload
 * that cannot be taken back is a worse failure than an extra click.
 *
 * So both doors stage, and these tests exist so a future simplification that
 * "removes the pointless extra step" fails loudly.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({
  upload: vi.fn(),
  submitTask: vi.fn(),
  toastError: vi.fn(),
}));

vi.mock('../hooks/useUploadDocument', async (importOriginal) => ({
  // Spread the REAL module so `documentRejectionReason` and DOCUMENT_ACCEPT
  // are the shipped ones. Mocking the validator would let this file agree
  // with itself about what is acceptable while the product disagreed.
  ...(await importOriginal<typeof import('../hooks/useUploadDocument')>()),
  useUploadDocument: () => h.upload,
}));

vi.mock('../hooks/useSubmitChatTask', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../hooks/useSubmitChatTask')>()),
  useSubmitChatTask: () => h.submitTask,
}));

vi.mock('sonner', () => ({
  toast: { error: h.toastError, success: vi.fn() },
}));

vi.mock('../services/agent', () => ({ cancelAgent: vi.fn() }));
vi.mock('../services/auth', () => ({
  getAccessToken: vi.fn().mockResolvedValue('tok'),
  demoMode: false,
}));

import { InputBar } from './InputBar';
import { useStore } from '../stores/store';

const pdf = (name = 'lease.pdf') =>
  new File(['x'], name, { type: 'application/pdf' });
const png = (name = 'shot.png') => new File(['x'], name, { type: 'image/png' });

/** A drag carrying files, as the browser presents it. */
const fileDrag = (files: File[]) => ({
  dataTransfer: { files, types: ['Files'], items: files },
});

const attachInput = () => screen.getByTestId('chat-attach-input');
const composer = () => screen.getByTestId('chat-composer-landing').parentElement!;

beforeEach(() => {
  vi.clearAllMocks();
  h.upload.mockResolvedValue(undefined);
  h.submitTask.mockResolvedValue(undefined);
  useStore.setState({ composerDraft: '', isRunning: false });
});

describe('picking a file stages it instead of sending it', () => {
  it('shows the file, and has NOT uploaded anything yet', async () => {
    render(<InputBar embedded />);
    fireEvent.change(attachInput(), { target: { files: [pdf()] } });

    await screen.findByTestId('staged-file');
    expect(screen.getByText('lease.pdf')).toBeTruthy();
    // The load-bearing half.
    expect(h.upload).not.toHaveBeenCalled();
  });

  it('lets you take it back before it costs anything', async () => {
    render(<InputBar embedded />);
    fireEvent.change(attachInput(), { target: { files: [pdf()] } });
    await screen.findByTestId('staged-file');

    fireEvent.click(screen.getByRole('button', { name: /remove lease\.pdf/i }));
    await waitFor(() => expect(screen.queryByTestId('staged-file')).toBeNull());
    expect(h.upload).not.toHaveBeenCalled();
  });
});

describe('dropping a file', () => {
  it('stages it, the same as the button', async () => {
    render(<InputBar embedded />);
    fireEvent.drop(composer(), fileDrag([png('roof.png')]));
    await screen.findByTestId('staged-file');
    expect(screen.getByText('roof.png')).toBeTruthy();
    expect(h.upload).not.toHaveBeenCalled();
  });

  it('refuses a file the upload would reject, and stages nothing', async () => {
    // ⚠ Refused BEFORE staging. A preview of a file that Send will later
    // refuse is a promise the button has to break.
    render(<InputBar embedded />);
    const zip = new File(['x'], 'books.zip', { type: 'application/zip' });
    fireEvent.drop(composer(), fileDrag([zip]));

    await waitFor(() => expect(h.toastError).toHaveBeenCalled());
    expect(screen.queryByTestId('staged-file')).toBeNull();
    expect(String(h.toastError.mock.calls[0][0])).toMatch(/pdf or an image/i);
  });

  it('does not stage while the agent is answering', async () => {
    useStore.setState({ isRunning: true });
    render(<InputBar embedded />);
    fireEvent.drop(composer(), fileDrag([pdf()]));

    await waitFor(() => expect(h.toastError).toHaveBeenCalled());
    expect(screen.queryByTestId('staged-file')).toBeNull();
  });

  it('ignores a drag that carries no files', () => {
    // Dragging selected TEXT across the composer fires the same events.
    // Lighting up "drop your file here" for someone moving a sentence
    // promises something that will not happen.
    render(<InputBar embedded />);
    fireEvent.dragEnter(composer(), {
      dataTransfer: { files: [], types: ['text/plain'], items: [] },
    });
    expect(screen.queryByText(/drop to attach/i)).toBeNull();
  });

  it('shows the overlay for a real file drag', async () => {
    render(<InputBar embedded />);
    fireEvent.dragEnter(composer(), fileDrag([pdf()]));
    expect(await screen.findByText(/drop to attach/i)).toBeTruthy();
  });
});

describe('sending', () => {
  it('uploads the staged file', async () => {
    render(<InputBar embedded />);
    fireEvent.change(attachInput(), { target: { files: [pdf()] } });
    await screen.findByTestId('staged-file');

    fireEvent.click(screen.getByRole('button', { name: /^send$/i }));
    await waitFor(() => expect(h.upload).toHaveBeenCalledTimes(1));
    expect(h.upload.mock.calls[0][0].name).toBe('lease.pdf');
  });

  it('is sendable with an attachment and no typed words', async () => {
    // "read this lease" with no covering sentence is the commonest way
    // anybody uses an attachment. A greyed-out Send beside a visible
    // attachment reads as broken.
    render(<InputBar embedded />);
    fireEvent.change(attachInput(), { target: { files: [pdf()] } });
    await screen.findByTestId('staged-file');

    const send = screen.getByRole('button', { name: /^send$/i });
    expect(send.hasAttribute('disabled')).toBe(false);
  });

  it('sends the file AND the words when both are present', async () => {
    render(<InputBar embedded />);
    useStore.setState({ composerDraft: 'what is the rent here?' });
    fireEvent.change(attachInput(), { target: { files: [pdf()] } });
    await screen.findByTestId('staged-file');

    fireEvent.click(screen.getByRole('button', { name: /^send$/i }));
    await waitFor(() => expect(h.upload).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(h.submitTask).toHaveBeenCalledWith('what is the rent here?'),
    );
  });

  it('clears the attachment so a second Send cannot re-send it', async () => {
    // ⚠ The duplicate-send shape (gotcha 25): a second Enter while the first
    // upload is in flight must not read the same staged file. It is cleared
    // BEFORE the await, so the worst case is a lost attachment the user can
    // SEE is gone — never a duplicate they cannot take back.
    let release!: () => void;
    h.upload.mockImplementation(
      () => new Promise<void>((r) => { release = () => r(); }),
    );

    render(<InputBar embedded />);
    fireEvent.change(attachInput(), { target: { files: [pdf()] } });
    await screen.findByTestId('staged-file');

    const send = screen.getByRole('button', { name: /^send$/i });
    fireEvent.click(send);
    await waitFor(() => expect(screen.queryByTestId('staged-file')).toBeNull());
    fireEvent.click(send);

    release();
    await waitFor(() => expect(h.upload).toHaveBeenCalledTimes(1));
  });
});
