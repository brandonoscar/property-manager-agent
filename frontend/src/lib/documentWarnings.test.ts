import { describe, expect, it } from 'vitest';

import {
  DOCUMENT_MAX_BYTES,
  describeSize,
  explainWarning,
  failureReason,
  formatMaxBytes,
  nothingWasRead,
} from './documentWarnings';

const MB = 1024 * 1024;

describe('explainWarning', () => {
  it('turns a machine token into something a person can act on', () => {
    const out = explainWarning('attachment_too_large');
    expect(out).not.toBe('attachment_too_large');
    // Says whose limit it is — "that file is invalid" would send somebody to
    // re-save a perfectly good scan instead of shrinking it.
    expect(out).toContain('Occupella can read');
    expect(out).toContain('8 MB');
  });

  it('passes the model’s own free-text warning through UNTOUCHED', () => {
    // ⚠ The load-bearing half. The extraction prompt asks the model, by name,
    // to write a warning for any field it had to null out — that line tells
    // somebody exactly which number to re-check, and "drop what I don't
    // recognise" would delete precisely those.
    const note = 'monthly_rent: figure smudged in the rent clause';
    expect(explainWarning(note)).toBe(note);
  });

  it('matches invalid_extraction as a PREFIX and drops the exception text', () => {
    // The backend appends the raw Python error. An equality check never fires,
    // and the detail is already logged server-side — the one place it must not
    // appear is a property manager's chat window.
    const out = explainWarning('invalid_extraction: Expecting value: line 1 column 1');
    expect(out).not.toContain('Expecting value');
    expect(out).toContain('could not get a usable result');
  });

  it('never tells somebody to retry a thing that cannot work', () => {
    // A spend cap and a bad key last until a person acts. "Try again in a
    // moment" there is an instruction guaranteed to waste their time.
    expect(explainWarning('llm_quota_exhausted')).toContain('administrator');
    expect(explainWarning('llm_quota_exhausted')).not.toContain('Try again');
    expect(explainWarning('llm_unauthorized')).toContain('administrator');
    // And the transient one still says to retry, or the rule above is just
    // "never suggest retrying", which is wrong in the common case.
    expect(explainWarning('llm_unavailable')).toContain('Try again');
  });

  it('does not name an internal library to the reader', () => {
    expect(explainWarning('pdf parsed to text via docling')).not.toContain('docling');
  });
});

describe('nothingWasRead', () => {
  it('is true for a failure and false for a caveat', () => {
    expect(nothingWasRead(['attachment_too_large'])).toBe(true);
    // A low-confidence result carries real extracted fields — suppressing the
    // answer there would hide work the product actually did.
    expect(nothingWasRead(['low_confidence'])).toBe(false);
    expect(nothingWasRead(['retried_once_after_malformed_json'])).toBe(false);
    expect(nothingWasRead([])).toBe(false);
    expect(nothingWasRead(undefined)).toBe(false);
  });

  it('sees a failure that is not first in the list', () => {
    expect(nothingWasRead(['low_confidence', 'empty_document'])).toBe(true);
  });

  it('recognises invalid_extraction by PREFIX, exception text and all', () => {
    // The backend appends the raw error after the token, so an equality test
    // never matches — and the client would then render the success
    // scaffolding around a read that produced nothing at all.
    expect(nothingWasRead(['invalid_extraction: Expecting value: line 1'])).toBe(true);
  });

  it('treats a password-protected PDF as unread, not as a thin result', () => {
    // The backend only appends this token once the read has ALREADY produced
    // nothing, so its presence is a fact about the outcome rather than a guess
    // about the file.
    expect(nothingWasRead(['doc_type_uncertain', 'document_password_protected'])).toBe(true);
  });
});

describe('failureReason', () => {
  it('prefers the PERMANENT cause over the generic token beside it', () => {
    // The backend emits ['llm_unavailable', 'llm_quota_exhausted'] — generic
    // first on purpose, so every membership check downstream keeps working.
    // Taking the first would promise "try again in a moment" about a spend cap
    // that lasts until somebody pays a bill.
    const out = failureReason(['llm_unavailable', 'llm_quota_exhausted']);
    expect(out).toContain('out of credit');
    expect(out).not.toContain('Try again');
  });

  it('falls back to the first failure when no permanent cause is named', () => {
    expect(failureReason(['llm_unavailable'])).toContain('Try again');
    expect(failureReason(['attachment_unavailable'])).toContain('damaged');
  });

  it('ignores caveats sitting in front of the real failure', () => {
    expect(failureReason(['low_confidence', 'attachment_too_large'])).toContain(
      'larger than Occupella can read',
    );
  });

  it('returns empty when nothing failed, so a caller cannot render a blank reason', () => {
    expect(failureReason(['low_confidence'])).toBe('');
    expect(failureReason([])).toBe('');
  });
});

describe('sizes', () => {
  it('describes the file with a decimal so it cannot contradict the limit', () => {
    // ⚠ Rounding both makes an 8.4 MB file read "That file is 8 MB. Occupella
    // can read documents up to 8 MB" — a sentence that argues with itself and
    // makes a correct refusal look like a bug.
    expect(describeSize(8.4 * MB)).toBe('8.4 MB');
    expect(formatMaxBytes()).toBe('8 MB');
    expect(describeSize(8.4 * MB)).not.toBe(formatMaxBytes());
  });

  it('drops the decimal once the number is big enough not to need it', () => {
    expect(describeSize(24 * MB)).toBe('24 MB');
  });

  it('states the reader ceiling in bytes, not a rounded figure', () => {
    expect(DOCUMENT_MAX_BYTES).toBe(8 * MB);
  });
});
