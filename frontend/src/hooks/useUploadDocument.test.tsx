/**
 * The document upload path says what it can actually do.
 *
 * Two defects, one chain. A property manager attaching a 12 MB scan got:
 *
 *   1. the whole upload — accepted by a browser ceiling of 20 MB, base64
 *      encoded to ~16 MB of JSON, posted — because the number the browser
 *      guarded was a comment about a MODEL ("Gemini multimodal caps out well
 *      above this") rather than about our own reader, which opens 8 MB;
 *   2. and then this, rendered verbatim:
 *
 *          **Document** · `lease.pdf` · 0% confidence
 *
 *          ### Notes
 *          - attachment_too_large
 *
 *          _Extraction is read-only. Ask a follow-up to act on it…_
 *
 * Every clause of that is wrong in a different way: a machine token where a
 * sentence belongs, a confidence percentage for a read that never happened,
 * and an invitation to act on fields that do not exist. The reason was known
 * exactly, before the first byte left the machine, and was never said.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { renderHook } from '@testing-library/react';

// ⚠ vi.mock is hoisted above every top-level binding (gotcha 44), so a spy
// shared with a factory has to be created inside vi.hoisted.
const { toastError, toastInfo, extractDocumentMock } = vi.hoisted(() => ({
  toastError: vi.fn(),
  toastInfo: vi.fn(),
  extractDocumentMock: vi.fn(),
}));
vi.mock('sonner', () => ({
  toast: { error: toastError, info: toastInfo, success: vi.fn() },
}));
vi.mock('../services/agent', () => ({ extractDocument: extractDocumentMock }));
vi.mock('../services/auth', () => ({
  getAccessToken: vi.fn(async () => 'tok'),
  demoMode: false,
}));

import type { DocumentExtraction } from '../services/agent';
import { useStore } from '../stores/store';
import {
  documentRejectionReason,
  formatExtraction,
  useUploadDocument,
} from './useUploadDocument';

const MB = 1024 * 1024;

function fileOfSize(bytes: number, type = 'application/pdf', name = 'lease.pdf'): File {
  const file = new File(['x'], name, { type });
  // File.size is read-only; the constructor cannot make a 12 MB blob cheaply.
  Object.defineProperty(file, 'size', { value: bytes });
  return file;
}

function extraction(over: Partial<DocumentExtraction> = {}): DocumentExtraction {
  return {
    doc_type: 'lease',
    confidence: 0.9,
    fields: { tenant_name: 'Maria Lopez' },
    summary: 'Standard 12-month residential lease.',
    warnings: [],
    ...over,
  } as DocumentExtraction;
}

beforeEach(() => {
  toastError.mockReset();
  toastInfo.mockReset();
  extractDocumentMock.mockReset();
  extractDocumentMock.mockResolvedValue(extraction());
  useStore.setState({
    isRunning: false,
    companyId: 'c1',
    activeThreadId: null,
    threads: [],
    queuedMessages: [],
  });
});
afterEach(() => {
  useStore.setState({ isRunning: false, activeThreadId: null, threads: [], queuedMessages: [] });
});

describe('the size the browser guards', () => {
  it('refuses a file the reader could never open, naming BOTH numbers', () => {
    const reason = documentRejectionReason(fileOfSize(12 * MB));
    expect(reason).toBeTruthy();
    expect(reason).toContain('12 MB'); // what they picked
    expect(reason).toContain('8 MB'); // what we can read
    // And what to do about it, because "too large" alone leaves somebody
    // re-uploading the same file expecting a different answer.
    expect(reason).toMatch(/lower-resolution|split/);
  });

  it('still accepts a file inside the limit', () => {
    // The other direction, or the guard above passes against a rule that
    // refuses everything (gotchas 38/51) — the same outage in nicer words.
    expect(documentRejectionReason(fileOfSize(3 * MB))).toBeNull();
  });

  it('names an 8.4 MB file as 8.4, not as the limit it is breaking', () => {
    // Rounding both sides produces "That file is 8 MB. Occupella can read
    // documents up to 8 MB" — a correct refusal that reads like a bug.
    const reason = documentRejectionReason(fileOfSize(8.4 * MB)) ?? '';
    expect(reason).toContain('8.4 MB');
  });

  it('never sends the bytes for a file it has already refused', async () => {
    // ⚠ The whole point. Before this, an oversize attachment was base64
    // encoded, uploaded in full, and refused server-side — the person paid the
    // upload to learn something the product knew before they clicked.
    const { result } = renderHook(() => useUploadDocument());
    await result.current(fileOfSize(12 * MB));

    expect(extractDocumentMock).not.toHaveBeenCalled();
    expect(toastError).toHaveBeenCalledWith(expect.stringContaining('8 MB'));
  });

  it('does send a file inside the limit', async () => {
    const { result } = renderHook(() => useUploadDocument());
    await result.current(fileOfSize(2 * MB));
    expect(extractDocumentMock).toHaveBeenCalledTimes(1);
  });
});

describe('formatExtraction', () => {
  it('shows a failure as a reason, with none of the success scaffolding', () => {
    const out = formatExtraction(
      extraction({ confidence: 0, fields: {}, summary: '', warnings: ['attachment_too_large'] }),
      'lease.pdf',
    );

    expect(out).not.toContain('attachment_too_large');
    expect(out).toContain('larger than Occupella can read');
    // No confidence figure for a read that did not happen, and no offer to act
    // on fields that do not exist.
    expect(out).not.toContain('confidence');
    expect(out).not.toContain('Ask a follow-up');
    expect(out).toContain('lease.pdf');
  });

  it('translates a caveat but keeps the result it is a caveat ABOUT', () => {
    const out = formatExtraction(
      extraction({ confidence: 0.4, warnings: ['low_confidence'] }),
      'lease.pdf',
    );

    expect(out).not.toContain('low_confidence');
    expect(out).toContain('uncertain');
    // The extraction still happened, so the fields and the scaffolding stay.
    expect(out).toContain('Maria Lopez');
    expect(out).toContain('40% confidence');
    expect(out).toContain('Ask a follow-up');
  });

  it('leaves the model’s own per-field note exactly as written', () => {
    const note = 'monthly_rent: figure smudged in the rent clause';
    const out = formatExtraction(extraction({ warnings: [note] }), 'lease.pdf');
    expect(out).toContain(`- ${note}`);
  });

  it('gives a dead provider the sentence for its CAUSE, not the generic one', () => {
    // The backend emits the generic token first on purpose; the cause rides
    // alongside. Rendering the first would promise "try again in a moment"
    // about a spend cap that lasts until somebody pays a bill.
    const out = formatExtraction(
      extraction({
        confidence: 0,
        fields: {},
        summary: '',
        warnings: ['llm_unavailable', 'llm_quota_exhausted'],
      }),
      'lease.pdf',
    );
    expect(out).toContain('out of credit');
    expect(out).not.toContain('Try again');
  });
});

describe('the attach path says something the reader can act on', () => {
  it('does not tell a workspace-less user to select a company', async () => {
    // ⚠ SAME RULE, SECOND LAYER. The send path and the attach path each
    // carried "No company selected." — a state with no control attached,
    // because there is no company picker for somebody with zero companies
    // (gotchas 102/106/118/133). Fixing the one that reported the defect and
    // leaving its twin is the fan-out this repo keeps recording (gotcha 25).
    //
    // The CONTROL FLOW here is deliberately unchanged: the staged file was
    // cleared before the await on purpose (InputBar's comment — a visibly
    // lost attachment beats a duplicate upload nobody can take back), and
    // this call owns an open agent-response lifecycle it has to close. So
    // only the wording moved.
    const { result } = renderHook(() => useUploadDocument());
    useStore.setState({ companyId: null });

    await result.current(fileOfSize(1024));

    expect(extractDocumentMock).not.toHaveBeenCalled();
    const calls = toastError.mock.calls;
    const said = String(calls[calls.length - 1]?.[0] ?? '');
    expect(said.toLowerCase()).toContain('reload');
    // Both directions (gotchas 38/51): a message that merely APPENDED
    // "reload" would pass the check above and still point at a picker that
    // does not exist.
    expect(said.toLowerCase()).not.toContain('select');
  });
});

describe('attaching while a turn is running', () => {
  it('says the attachment did not go, instead of dropping it in silence', async () => {
    // ⚠ The purest instance of gotcha 44 in the product. InputBar clears the
    // staged file BEFORE awaiting — deliberately, so a second Enter cannot
    // upload it twice — so a bare `return` here made the document vanish with
    // no toast, no upload and no error. A refusal the user cannot perceive is
    // indistinguishable from a send.
    const { result } = renderHook(() => useUploadDocument());
    useStore.setState({ isRunning: true });

    await result.current(fileOfSize(1024));

    expect(extractDocumentMock).not.toHaveBeenCalled();
    expect(toastInfo).toHaveBeenCalled();
    const calls = toastInfo.mock.calls;
    const said = String(calls[calls.length - 1]?.[0] ?? '').toLowerCase();
    // ⚠ It must NOT promise the file is queued. The queue drains through
    // `submitTask` and carries text, not files — a user who believes the
    // attachment landed and gets an answer that never read it has been
    // misled rather than helped (the same rule the send path's queue
    // message is written to).
    expect(said).not.toContain('queued');
    expect(said).toMatch(/again|finish/);
  });

  it('reads isRunning at CALL time, not from the render closure', async () => {
    // The store changes AFTER the hook rendered — the window the defect
    // lived in. Its sibling `useSubmitChatTask` moved these reads to
    // `getState()` for exactly this; this hook never got that fix.
    const { result } = renderHook(() => useUploadDocument());
    useStore.setState({ isRunning: true });

    await result.current(fileOfSize(1024));
    expect(extractDocumentMock).not.toHaveBeenCalled();
  });

  it('uses the company resolved at CALL time, not the one at render', async () => {
    // Boot order: App.tsx awaits `ensureCompanyProvisioned()` and only then
    // calls `setCompanies`, so the composer renders with companyId null. A
    // stale read refused an upload the store could have served.
    const { result } = renderHook(() => useUploadDocument());
    useStore.setState({ companyId: 'resolved-after-render' });

    await result.current(fileOfSize(1024));

    expect(extractDocumentMock).toHaveBeenCalledTimes(1);
    expect(extractDocumentMock.mock.calls[0][0].companyId).toBe('resolved-after-render');
  });
});
