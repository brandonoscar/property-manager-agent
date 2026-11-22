/**
 * useUploadDocument — the in-chat document upload path.
 *
 * Wires the composer's attach (+) button to the document agent's tested
 * extraction core via POST /agent/extract-document. The flow mirrors a
 * normal chat turn so the result threads into the conversation: a user
 * message (📎 filename) followed by an assistant bubble that streams in
 * the formatted structured fields.
 *
 * Read-only end to end — extraction never writes, so there is no confirm
 * gate. The user can follow up in chat ("file this under unit 4B",
 * "draft a renewal from these terms") and THAT may hit the gate.
 *
 * Returns ``uploadDocument(file)``; the caller owns the file input.
 */

import { useCallback } from 'react';
import { toast } from 'sonner';

import { extractDocument, type DocumentExtraction } from '../services/agent';
import { getAccessToken, demoMode } from '../services/auth';
import { ApiAuthError, ApiError } from '../services/api';
import {
  DOCUMENT_MAX_BYTES,
  describeSize,
  explainWarning,
  failureReason,
  formatMaxBytes,
  nothingWasRead,
} from '../lib/documentWarnings';
import { SUPPORT_EMAIL } from '../lib/support';
import { useStore } from '../stores/store';

// The real (non-eval) subset the backend accepts. Kept in lockstep with
// `_UPLOAD_MIME_TYPES` in api/routes/agent.py — a PDF or a still image.
const ACCEPTED_MIME_TYPES = [
  'application/pdf',
  'image/png',
  'image/jpeg',
  'image/webp',
] as const;

export const DOCUMENT_ACCEPT = ACCEPTED_MIME_TYPES.join(',');

// Guard the upload before it ever leaves the browser, against the limit that
// actually decides the outcome: what the backend's reader can OPEN.
//
// ⚠ This used to be 20 MB, justified by a comment saying "Gemini multimodal
// caps out well above this" — a claim about a MODEL rather than about our own
// reader, and about a provider this product left in July (gotcha 18). The
// reader's ceiling has been 8 MB the whole time, so every file between the two
// was accepted here, base64-encoded to ~1.33x, posted, and refused server-side.
// The person waited out the whole upload to be shown `attachment_too_large`.
// Same outcome, thirty seconds earlier, in a sentence — and the bytes never
// leave the machine.
const MAX_BYTES = DOCUMENT_MAX_BYTES;

/**
 * Why a file cannot be sent, or `null` if it can.
 *
 * ⚠ ONE definition, deliberately, because there are now TWO doors into this
 * upload: the attach button and a drag-and-drop zone. Two copies of "what
 * counts as acceptable" drift, and the direction that hurts is the drop path
 * being MORE permissive — it would accept a file, stage it, show the person a
 * preview implying it is fine, and fail at the network. Gotcha 25's rule
 * applied to a validation rule rather than a field shape.
 *
 * The hook re-checks even though every caller has already called this. That is
 * not redundancy: this function is a courtesy that produces a good message
 * early, and the hook's copy is the last thing standing before the request.
 */
export function documentRejectionReason(file: File): string | null {
  if (!ACCEPTED_MIME_TYPES.includes(file.type as never)) {
    return 'Unsupported file. Upload a PDF or an image (PNG / JPEG / WebP).';
  }
  if (file.size > MAX_BYTES) {
    return `That file is ${describeSize(file.size)}. Occupella can read documents up to ${formatMaxBytes()} — try a lower-resolution scan, or split it into parts.`;
  }
  return null;
}

/** Read a File as a bare base64 string (no data: URL prefix). */
function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result as string;
      // FileReader.readAsDataURL → "data:<mime>;base64,<payload>".
      const comma = result.indexOf(',');
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.onerror = () => reject(reader.error ?? new Error('File read failed'));
    reader.readAsDataURL(file);
  });
}

const DOC_TYPE_LABEL: Record<DocumentExtraction['doc_type'], string> = {
  lease: 'Lease',
  invoice: 'Invoice',
  inspection_report: 'Inspection report',
  work_order: 'Work order',
  other: 'Document',
};

/**
 * Format an extraction envelope as the assistant's markdown reply.
 *
 * ⚠ TWO failure shapes this used to produce, both of which read as "nobody
 * thought about this case":
 *
 *  - a warning list rendered VERBATIM, so a 12 MB scan came back as
 *    `### Notes` / `- attachment_too_large`;
 *  - and, around it, the full success scaffolding — "0% confidence" and an
 *    invitation to "ask a follow-up to act on it" — describing an extraction
 *    that never happened and offering to act on nothing.
 *
 * So a result whose warnings say nothing was read gets ONLY the reason, and
 * every other result gets its machine tokens translated while the model's own
 * per-field notes pass through untouched.
 */
export function formatExtraction(
  result: DocumentExtraction,
  filename: string,
): string {
  const warnings = result.warnings ?? [];

  if (nothingWasRead(warnings)) {
    // One sentence, no scaffolding, no offer to act on fields that do not
    // exist. Which sentence is `failureReason`'s job — picking it off the
    // list is where the quota-vs-transient mistake lives.
    return [`**Couldn't read** · \`${filename}\``, '', failureReason(warnings)]
      .join('\n')
      .trimEnd();
  }

  const lines: string[] = [];
  const label = DOC_TYPE_LABEL[result.doc_type] ?? 'Document';
  const pct = Math.round((result.confidence ?? 0) * 100);
  lines.push(`**${label}** · \`${filename}\` · ${pct}% confidence`);

  if (result.summary) {
    lines.push('', result.summary);
  }

  const fieldEntries = Object.entries(result.fields ?? {}).filter(
    ([, v]) => v !== null && v !== undefined && v !== '',
  );
  if (fieldEntries.length > 0) {
    lines.push('', '### Extracted fields');
    for (const [key, value] of fieldEntries) {
      const pretty = key
        .replace(/_/g, ' ')
        .replace(/\b\w/g, (c) => c.toUpperCase());
      const rendered = Array.isArray(value)
        ? value.join(', ')
        : typeof value === 'object'
          ? JSON.stringify(value)
          : String(value);
      lines.push(`- **${pretty}:** ${rendered}`);
    }
  }

  if (warnings.length > 0) {
    // Machine tokens become sentences; the model's own per-field notes
    // ("monthly_rent: figure smudged in the rent clause") pass through
    // untouched — those are the half worth reading.
    lines.push('', '### Notes');
    for (const w of warnings) lines.push(`- ${explainWarning(w)}`);
  }

  lines.push(
    '',
    '_Extraction is read-only. Ask a follow-up to act on it — e.g. "file this under the right unit" or "draft a renewal from these terms"._',
  );

  return lines.join('\n');
}

export function useUploadDocument(): (file: File) => Promise<void> {
  // ⚠ `isRunning` / `companyId` / `activeThreadId` are READ LIVE below, not
  // captured here. This hook never got gotcha 44's fix: its sibling
  // `useSubmitChatTask` moved exactly these three to `getState()` because
  // Zustand's `set` is synchronous while React's re-render is not, so a
  // handler React already holds can close over the previous values. Here that
  // window let the guard read a stale `companyId` and hand it to
  // `extractDocument` — the attach path's version of the defect that ate a
  // typed message.
  const createThread = useStore((s) => s.createThread);
  const addUserMessage = useStore((s) => s.addUserMessage);
  const startAgentResponse = useStore((s) => s.startAgentResponse);
  const appendToken = useStore((s) => s.appendToken);
  const finishAgentResponse = useStore((s) => s.finishAgentResponse);
  const setError = useStore((s) => s.setError);

  return useCallback(
    async (file: File) => {
      const { isRunning, companyId, activeThreadId } = useStore.getState();

      // ⚠ THIS USED TO BE A BARE `return`, and it is the purest instance of
      // gotcha 44 in the product: InputBar clears the staged file BEFORE
      // awaiting (deliberately — a visibly lost attachment beats a duplicate
      // upload nobody can take back), so attaching a document while the agent
      // was thinking made it disappear with NO toast, no upload and no error.
      // The clearing stays; what was missing is any sign it happened.
      //
      // It cannot queue the way a typed message does — the queue drains
      // through `submitTask` and carries text, not files — so the honest
      // instruction is to attach it again when the turn ends.
      if (isRunning) {
        toast.info('Occupella is still working — attach it again when this turn finishes.');
        return;
      }

      const rejection = documentRejectionReason(file);
      if (rejection) {
        toast.error(rejection);
        return;
      }

      let threadId = activeThreadId;
      if (!threadId) threadId = createThread();

      addUserMessage(`📎 ${file.name}`);
      startAgentResponse();

      // Demo mode is frontend-only with no backend — return a canned,
      // clearly-labelled extraction so the feature is demoable offline
      // without ever touching the network or real data.
      if (demoMode) {
        const demo: DocumentExtraction = {
          doc_type: 'lease',
          confidence: 0.94,
          fields: {
            tenant_name: 'Maria Lopez',
            property_address: '540 Market Ave #2',
            monthly_rent: '$1,840',
            lease_start: '2026-07-01',
            lease_end: '2027-06-30',
          },
          summary:
            'Standard 12-month residential lease. Rent $1,840/mo, due on the 1st, $1,840 security deposit.',
          warnings: ['demo_mode_sample'],
        };
        const text = formatExtraction(demo, file.name);
        appendToken(text);
        finishAgentResponse(text);
        return;
      }

      // ⚠ SAME WORDING AS THE SEND PATH, and deliberately NOT the same
      // control flow. `useSubmitChatTask` throws so the composer can put the
      // words back; here the staged file was cleared before the await on
      // purpose (InputBar's own comment: a visibly-lost attachment beats a
      // duplicate upload nobody can take back), and this call already owns an
      // open agent-response lifecycle that has to be closed. So the fix at
      // this layer is the MESSAGE — an instruction the reader can act on
      // rather than a state they cannot change (gotcha 25: every layer that
      // carries the rule, not just the one that reported it).
      const token = await getAccessToken();
      if (!token) {
        toast.error('Your session expired. Sign in again, then attach it once more.');
        finishAgentResponse('');
        return;
      }
      if (!companyId) {
        toast.error(
          `Your workspace didn't finish loading. Reload the page — if it keeps happening, email ${SUPPORT_EMAIL}.`,
        );
        finishAgentResponse('');
        return;
      }

      try {
        const base64Content = await fileToBase64(file);
        const result = await extractDocument({
          companyId,
          base64Content,
          mimeType: file.type,
          filename: file.name,
        });
        const text = formatExtraction(result, file.name);
        appendToken(text);
        finishAgentResponse(text);
      } catch (err) {
        const message =
          err instanceof ApiAuthError
            ? 'Session expired. Please sign in again.'
            : err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : 'Could not read that document.';
        setError(message);
        toast.error(message);
        finishAgentResponse('');
      }
    },
    [
      createThread,
      addUserMessage,
      startAgentResponse,
      appendToken,
      finishAgentResponse,
      setError,
    ],
  );
}
