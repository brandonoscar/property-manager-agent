/**
 * Turn a document-extraction warning into something a property manager can act
 * on — and know when the extraction did not happen at all.
 *
 * `DocumentExtraction.warnings` mixes two kinds of string, and the chat client
 * used to render both verbatim:
 *
 *  - machine tokens the backend appends (`attachment_too_large`,
 *    `llm_unauthorized`, `pdf parsed to text via docling`), which mean nothing
 *    to a person and named an internal library to their face;
 *  - free text the MODEL writes, which the extraction prompt asks for by name
 *    ("monthly_rent: figure smudged in the rent clause") and which is the most
 *    useful line in the whole result.
 *
 * So: translate the first, pass the second through untouched. ⚠ The
 * pass-through is the load-bearing half — "drop what I don't recognise" is the
 * obvious implementation and it deletes exactly the per-field explanations that
 * tell somebody which number to re-check.
 *
 * The vocabulary lives in `src/helixis/agents/document_warnings.py`; a Python
 * test reads THIS file and fails when a token there has no sentence here, so a
 * new backend token cannot reach a screen raw.
 */

/**
 * The largest document Occupella can actually READ, mirroring
 * `DocumentSettings.max_bytes` in `src/helixis/settings.py`.
 *
 * ⚠ This is the number that decides whether the upload is worth making at all,
 * and before this existed the browser's own ceiling was 20 MB — so a 12 MB scan
 * was accepted, base64-encoded to ~16 MB of JSON, posted, and refused by the
 * reader. The person waited out the whole upload to be told `attachment_too_
 * large`. The server still enforces its own copy and its 413 names the real
 * figure, so an operator who raises the setting is never blocked by a stale
 * constant here — the client is the courtesy, the server is the rule.
 */
export const DOCUMENT_MAX_BYTES = 8 * 1024 * 1024;

/** "8 MB" — the limit, for a sentence rather than for arithmetic. */
export function formatMaxBytes(bytes: number = DOCUMENT_MAX_BYTES): string {
  return `${Math.round(bytes / (1024 * 1024))} MB`;
}

/**
 * "8.4 MB" — one decimal, because this is always printed BESIDE the rounded
 * limit. Rounding both makes an 8.4 MB file read "That file is 8 MB. Occupella
 * can read documents up to 8 MB", which is a sentence that contradicts itself
 * and makes a correct refusal look like a bug.
 */
export function describeSize(bytes: number): string {
  const mb = bytes / (1024 * 1024);
  return `${mb >= 10 ? Math.round(mb) : Math.round(mb * 10) / 10} MB`;
}

/**
 * Prefix, not an exact token: the backend appends the exception text after it.
 * An equality check here silently never fires, which would render the hardest
 * failure in the set as an ordinary note.
 */
const INVALID_EXTRACTION_PREFIX = 'invalid_extraction';

/**
 * Tokens meaning NOTHING USABLE CAME BACK. Mirrors `FAILURE_TOKENS`.
 *
 * `low_confidence`, `doc_type_uncertain` and the two retry notes are
 * deliberately absent: those results carry real extracted fields and the
 * warning is a caveat on them, not a failure.
 */
const FAILURE_TOKENS: ReadonlySet<string> = new Set([
  'attachment_unavailable',
  'attachment_too_large',
  'unsupported_mime_type',
  'document_password_protected',
  'empty_document',
  INVALID_EXTRACTION_PREFIX,
  'llm_unavailable',
  'llm_unauthorized',
]);

/** One warning that says the extraction produced nothing. */
export function isFailureWarning(warning: string): boolean {
  const token = (warning ?? '').trim();
  if (!token) return false;
  if (token.startsWith(INVALID_EXTRACTION_PREFIX)) return true;
  return FAILURE_TOKENS.has(token);
}

/** Does this result's warning list say the extraction did not happen? */
export function nothingWasRead(warnings: readonly string[] | undefined): boolean {
  return (warnings ?? []).some(isFailureWarning);
}

/**
 * Every enumerable token → the sentence a person gets instead.
 *
 * Two rules the wording follows, both learned the expensive way elsewhere in
 * this product: say whether the limit is OURS or the file's, and never tell
 * somebody to retry a thing that cannot work. A spend cap and a bad key last
 * until a person acts, so "try again in a moment" there is an instruction that
 * is guaranteed to waste their time.
 */
const SENTENCES: Readonly<Record<string, string>> = {
  attachment_unavailable:
    'Occupella could not read that file — it may be damaged, or the upload did not finish. Try uploading it again.',
  attachment_too_large: `That file is larger than Occupella can read (${formatMaxBytes()}). Try a lower-resolution scan, or split it into parts.`,
  unsupported_mime_type:
    "That file type isn't supported. Upload a PDF or an image (PNG, JPEG or WebP).",
  document_password_protected:
    'That PDF is password-protected, so its contents could not be read. Remove the password and upload it again.',
  empty_document:
    'Nothing could be read from that document — it looks blank or unscannable.',
  [INVALID_EXTRACTION_PREFIX]:
    'Occupella could not get a usable result from that document, even after a second attempt.',
  llm_unavailable:
    "The document reader didn't respond. Try again in a moment.",
  llm_unauthorized:
    'The document reader rejected our credentials. An administrator needs to fix this — trying again will not help.',
  llm_quota_exhausted:
    'The document reader is out of credit. An administrator needs to fix this — trying again will not help.',
  low_confidence:
    'Some of these fields are uncertain — check them against the document before relying on them.',
  doc_type_uncertain:
    "Occupella could not tell what kind of document this is, so it read it generically.",
  retried_once_after_malformed_json:
    'Occupella had to read this document a second time to get a clean result.',
  recovered_noisy_json_attempt_0:
    'Occupella cleaned up a malformed response before reading this.',
  'pdf parsed to text via docling':
    "Read from the PDF's own text layer rather than as an image.",
};

/**
 * The sentence for a machine token, or the warning unchanged when it is the
 * model's own free text.
 *
 * ⚠ The `invalid_extraction` arm drops the exception text the backend appends.
 * That detail is a Python error string; it is already logged server-side, and
 * the one place it must not appear is a property manager's chat window.
 */
export function explainWarning(warning: string): string {
  const token = (warning ?? '').trim();
  if (!token) return token;
  if (token.startsWith(INVALID_EXTRACTION_PREFIX)) {
    return SENTENCES[INVALID_EXTRACTION_PREFIX];
  }
  return SENTENCES[token] ?? warning;
}

/**
 * The ONE sentence to show when a result says nothing was read, or `''`.
 *
 * ⚠ **The first failure warning is the wrong answer, and the case that proves
 * it is the expensive one.** A dead provider emits `['llm_unavailable',
 * 'llm_quota_exhausted']` — the generic token leads deliberately, so every
 * membership check downstream keeps working, and the CAUSE rides alongside it.
 * Taking the first would say "try again in a moment" about a spend cap that
 * lasts until somebody pays a bill: an instruction guaranteed to waste the
 * reader's time, which is the same silent-degradation shape the backend fixed
 * for its own prompt and never propagated to the screen.
 *
 * So a cause that names a PERMANENT state outranks the generic, because the two
 * give opposite instructions. Everything else is first-failure-wins.
 */
export function failureReason(warnings: readonly string[] | undefined): string {
  const list = (warnings ?? []).map((w) => (w ?? '').trim()).filter(Boolean);
  for (const permanent of ['llm_quota_exhausted', 'llm_unauthorized']) {
    if (list.includes(permanent)) return SENTENCES[permanent];
  }
  const failure = list.find(isFailureWarning);
  return failure ? explainWarning(failure) : '';
}

/** Exposed for the cross-language guard; not part of the render path. */
export const EXPLAINED_TOKENS: readonly string[] = Object.keys(SENTENCES);
