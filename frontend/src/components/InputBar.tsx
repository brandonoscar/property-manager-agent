/**
 * Input bar — the composer pinned to the bottom of the chat view.
 *
 * The text sits on top and wraps as it grows (up to MAX_COMPOSER_PX, then
 * it scrolls inside the box); the attach button and Send sit on a row under
 * it. A one-line text input scrolled sideways once a prompt outgrew it, so
 * the start of what you had typed disappeared off the left edge (founder,
 * 2026-09-30). Enter sends, Shift+Enter adds a line. The stop button
 * replaces Send while the agent runs.
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { motion } from 'motion/react';
import { Plus, Square } from 'lucide-react';
import { toast } from 'sonner';
import { cancelAgent } from '../services/agent';
import { getAccessToken } from '../services/auth';
import { TaskNotSent, useSubmitChatTask } from '../hooks/useSubmitChatTask';
import {
  documentRejectionReason,
  useUploadDocument,
  DOCUMENT_ACCEPT,
} from '../hooks/useUploadDocument';
import { StagedFile } from './StagedFile';
import { useUnsavedWork } from '../hooks/useUnsavedWork';
import { useStore } from '../stores/store';

// The three quick-action chips ("Late rent" / "Maintenance" / "Tenant
// emails") that used to sit under the composer are GONE (founder direction,
// 2026-08-30). They were prefill shortcuts, not capabilities: clicking one
// typed a sentence into the box the user was already looking at. Removed,
// not hidden — same treatment as the "Owner report" chip in the 2026-08-15
// audit, so there is no dead constant for a future session to re-render.
//
// The composer's placeholder is its only prompt. The empty chat screen has
// four starter QUESTIONS under the composer (ChatThread StarterQuestions,
// docs/decisions.md 2026-09-29), which send when clicked; they are not these
// prefill chips and do not live in this component.

/** Solid iris when there is something to send; the shared gray otherwise. */
function SEND_STYLE(canSend: boolean): React.CSSProperties {
  return canSend
    ? { background: 'var(--iris)', color: '#fff', border: '1px solid var(--iris)' }
    : { background: 'var(--disabled-bg)', color: 'var(--disabled-ink)', border: '1px solid var(--disabled-bg)' };
}

/** The box grows with the prompt up to this height, then scrolls inside. */
const MAX_COMPOSER_PX = 200;

/**
 * The prompt field: a textarea that grows with what is typed.
 *
 * ⚠ Height is measured from `scrollHeight` after resetting to `auto`, on
 * every value change. Without the reset, `scrollHeight` never reports less
 * than the current height, so a box that grew for a long paste would never
 * shrink back after the text was cleared.
 */
function ComposerField({
  value,
  onChange,
  onKeyDown,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  onKeyDown: (e: React.KeyboardEvent<HTMLTextAreaElement>) => void;
  placeholder: string;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = 'auto';
    const full = el.scrollHeight;
    el.style.height = `${Math.min(full, MAX_COMPOSER_PX)}px`;
    el.style.overflowY = full > MAX_COMPOSER_PX ? 'auto' : 'hidden';
  }, [value]);
  return (
    <textarea
      ref={ref}
      rows={1}
      data-testid="chat-composer-input"
      aria-label="Message Occupella"
      className="block w-full resize-none bg-transparent border-none outline-none text-[14px] leading-[1.5]"
      // Outline off INLINE: the global `:focus-visible` rule is unlayered and
      // beats the utility class, and a focused text field always matches it,
      // so the box drew a second ring inside the composer's own.
      style={{
        color: 'var(--ink)',
        fontFamily: 'var(--font-sans)',
        maxHeight: MAX_COMPOSER_PX,
        outline: 'none',
      }}
      placeholder={placeholder}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={onKeyDown}
      // ⚠ NOT disabled mid-run. A disabled box is why a thought that arrived
      // during a three-minute turn had nowhere to go. Enter queues it as a
      // follow-up (see the placeholder, which says so).
    />
  );
}

/** Clicking the composer's padding focuses the field, so the whole box is
 *  the target, not just the line of text. */
function focusFieldOnPadding(e: React.MouseEvent<HTMLElement>) {
  if (e.target !== e.currentTarget) return;
  // Without preventDefault the mousedown's default action runs after this
  // and blurs the field it just focused.
  e.preventDefault();
  e.currentTarget.querySelector<HTMLTextAreaElement>('textarea')?.focus();
}

interface InputBarProps {
  /**
   * When true, the composer renders without outer page padding so it
   * can sit inline inside another layout (e.g. the empty hero). When
   * false (default), it renders as the bottom-pinned composer.
   */
  embedded?: boolean;
}

export function InputBar({ embedded = false }: InputBarProps = {}) {
  const input = useStore((s) => s.composerDraft);
  const setInput = useStore((s) => s.setComposerDraft);
  const isRunning = useStore((s) => s.isRunning);
  const activeThreadId = useStore((s) => s.activeThreadId);
  const abortController = useStore((s) => s.abortController);
  const pendingConfirmation = useStore((s) => s.pendingConfirmation);
  const setError = useStore((s) => s.setError);
  const queuedMessages = useStore((s) => s.queuedMessages);
  const removeQueuedMessage = useStore((s) => s.removeQueuedMessage);

  // Single shared send path — also used by the chat quick-actions rail.
  const submitTask = useSubmitChatTask();

  // In-chat document upload (the attach + button). A hidden file input
  // is opened by the button; the picked file is read as base64 and sent
  // to the document agent, rendering its structured fields as a chat
  // turn. Read-only — no write, no confirm gate.
  const uploadDocument = useUploadDocument();
  const fileInputRef = useRef<HTMLInputElement>(null);

  // ⚠ STAGED, not sent. Both doors — the button and a drop — put the file
  // here and wait for Send. A drop is one careless drag away from happening
  // by accident, and the old flow uploaded on pick, so the first time anyone
  // saw WHICH file they had chosen was in the transcript, after it had cost a
  // request and a turn. See StagedFile.tsx.
  const [staged, setStaged] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  // Nested drag targets each fire enter/leave, so a boolean flickers as the
  // pointer crosses a child. Counting enters and leaves is what makes the
  // overlay stable.
  const dragDepth = useRef(0);

  const stageFile = useCallback(
    (file: File) => {
      const rejection = documentRejectionReason(file);
      if (rejection) {
        // Refused BEFORE staging, so nothing appears that cannot be sent —
        // showing a preview of a file we will later refuse is a promise the
        // Send button has to break.
        toast.error(rejection);
        return;
      }
      setStaged(file);
    },
    [],
  );

  const handleAttachClick = useCallback(() => {
    if (isRunning) return;
    fileInputRef.current?.click();
  }, [isRunning]);

  const handleFileChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      // Reset so picking the same file twice still fires onChange.
      e.target.value = '';
      if (file) stageFile(file);
    },
    [stageFile],
  );

  const dropProps = {
    onDragEnter: (e: React.DragEvent) => {
      // Only react to an actual FILE drag. Dragging selected TEXT across the
      // composer also fires these events, and lighting up a "drop your file"
      // overlay for someone moving a sentence is a lie about what will happen.
      if (!Array.from(e.dataTransfer?.types ?? []).includes('Files')) return;
      dragDepth.current += 1;
      setDragging(true);
    },
    onDragOver: (e: React.DragEvent) => {
      if (!Array.from(e.dataTransfer?.types ?? []).includes('Files')) return;
      // ⚠ preventDefault is what stops the BROWSER taking the drop and
      // navigating away to the file — losing the whole conversation. It has
      // to be on dragover specifically; doing it only on drop is too late.
      e.preventDefault();
    },
    onDragLeave: () => {
      dragDepth.current = Math.max(0, dragDepth.current - 1);
      if (dragDepth.current === 0) setDragging(false);
    },
    onDrop: (e: React.DragEvent) => {
      const file = e.dataTransfer?.files?.[0];
      if (!file) return;
      e.preventDefault();
      dragDepth.current = 0;
      setDragging(false);
      if (isRunning) {
        toast.error('Wait for the current answer to finish, then drop the file again.');
        return;
      }
      stageFile(file);
    },
  };

  const dropOverlay = dragging ? (
    <div
      // Presentational only: the keyboard path to the same capability is the
      // attach button, which is already labelled and focusable. Announcing a
      // mouse-only overlay would offer a screen-reader user a door they
      // cannot open.
      aria-hidden="true"
      className="absolute inset-0 z-10 flex items-center justify-center rounded-[var(--r-lg)] pointer-events-none text-[13px] font-medium"
      style={{
        background: 'var(--iris-ring)',
        border: '1.5px dashed var(--iris)',
        color: 'var(--iris)',
      }}
    >
      Drop to attach a PDF or image
    </div>
  ) : null;

  // Typed words and a picked file are both work with no other home: the draft
  // lives in memory, and closing the tab loses it silently. Deliberately NOT
  // gated on `isRunning` — a run in flight survives the tab and reattaches,
  // so warning about it would be warning about nothing.
  // ⚠ Two different losses, said separately. A tab close destroys both; an
  // in-app navigation destroys only the STAGED FILE, because the draft is
  // `composerDraft` in the store and comes back untouched. Warning about the
  // draft on every click of the rail is how a warning becomes one people click
  // through — and then it is not there for the carrier registration either.
  useUnsavedWork(input.trim().length > 0 || staged !== null, {
    lostOnNavigation: staged !== null,
  });

  const stagedStrip = staged ? (
    <div className="mx-auto w-full max-w-[820px] mb-2">
      <StagedFile file={staged} disabled={isRunning} onRemove={() => setStaged(null)} />
    </div>
  ) : null;

  const hiddenFileInput = (
    <input
      ref={fileInputRef}
      type="file"
      accept={DOCUMENT_ACCEPT}
      className="hidden"
      data-testid="chat-attach-input"
      onChange={handleFileChange}
    />
  );

  const handleSubmit = useCallback(async () => {
    const task = input.trim();

    // ⚠ THE FILE GOES FIRST, and it is cleared BEFORE the await. A second
    // Enter while the upload is in flight would otherwise read the same
    // `staged` and send the document twice — the check-then-act shape this
    // repo keeps getting bitten by (gotcha 25). Clearing first means the
    // worst case is a lost attachment the user can see is gone, not a
    // duplicate they cannot take back.
    if (staged) {
      const file = staged;
      setStaged(null);
      await uploadDocument(file);
    }

    if (!task) return;
    // ⚠ NO ``isRunning`` BAIL. It used to return here, and the composer had
    // already cleared, so a message typed in the window React had not yet
    // re-rendered vanished (gotcha 44). ``submitTask`` reads the live state
    // itself and now QUEUES rather than dropping, so every keystroke has a
    // destination: sent, queued, or thrown back into the box below.
    setInput('');
    try {
      await submitTask(task);
    } catch (err) {
      // The send was REFUSED, not attempted. Put the user's words back
      // rather than swallowing them — losing a typed message with no error
      // is the worst thing this composer can do.
      if (err instanceof TaskNotSent) {
        setInput(task);
        return;
      }
      throw err;
    }
  }, [input, setInput, submitTask, staged, uploadDocument]);

  const handleStop = useCallback(async () => {
    if (abortController) abortController.abort();
    if (activeThreadId) {
      const token = await getAccessToken();
      if (token) {
        cancelAgent(activeThreadId, token).catch(() => {
          // The run may have already finished/been cleaned up server-side
          // (or belong to another company) by the time cancel arrives —
          // the abort above already stopped the client from waiting on it.
        });
      }
    }
    setError('Cancelled');
  }, [abortController, activeThreadId, setError]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter while an IME is composing picks the candidate; it must not send.
    if (e.nativeEvent.isComposing) return;
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  // Drain the queue the moment the turn ends.
  //
  // ⚠ TAKE BEFORE AWAITING. ``submitTask`` is async and flips ``isRunning``
  // on a later tick, so an effect that re-ran while the queue still held its
  // items would send them twice — check-then-act around an await is not a
  // claim (gotcha 25, arriving in React). ``takeQueuedMessages`` reads and
  // clears in one ``set``.
  //
  // The items go as ONE follow-up rather than N turns: the user was adding
  // context to a single thought, and N separate turns would each pay the full
  // model cost and answer a fragment.
  useEffect(() => {
    if (isRunning || queuedMessages.length === 0) return;
    const pending = useStore.getState().takeQueuedMessages(activeThreadId);
    if (pending.length === 0) return;
    const followUp = pending.join('\n');
    void submitTask(followUp).catch(() => {
      // Refused after all (a teammate took the thread, a run started in the
      // gap). Put it back rather than losing words the user watched us
      // accept — the queue chip is a promise that they still exist.
      useStore.getState().queueMessage(followUp, activeThreadId);
    });
  }, [isRunning, queuedMessages, submitTask, activeThreadId]);

  // The composer promises "press Esc to stop" while the agent runs, but the
  // input is disabled mid-run so a per-input handler never fires — wire Esc
  // at the window. Skip when a confirmation is pending: ConfirmCard owns Esc
  // then (Esc = deny), so we must not also abort the turn out from under it.
  useEffect(() => {
    if (!isRunning || pendingConfirmation) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        handleStop();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [isRunning, pendingConfirmation, handleStop]);

  // ⚠ Says QUEUE, not "the agent will see this". It won't — the run is
  // already dispatched. Promising otherwise is worse than the old disabled box.
  const placeholder = isRunning
    ? 'Add a follow-up — it sends when this finishes (Esc to stop)'
    : activeThreadId
    ? 'Reply or start a new task'
    : 'What should Occupella do next?';

  // Enter mid-run queues, so the button is live then too.
  // A staged file is a sendable thing on its own — "read this lease" with no
  // covering sentence is the commonest way anybody uses an attachment, and a
  // Send button greyed out next to a visible attachment reads as broken.
  const canSend = input.trim().length > 0 || staged !== null;
  // ⚠ THE QUEUE HAS TO BE VISIBLE AND CANCELLABLE. A message the user typed
  // and can no longer see is indistinguishable from one we dropped, and the
  // whole point of queueing was to stop dropping them. The wording says
  // FOLLOW-UP, never "the agent is reading this" — the run is already
  // dispatched and cannot see it.
  const mine = queuedMessages
    .map((q, i) => ({ ...q, index: i }))
    .filter((q) => q.threadId === activeThreadId);
  const queueStrip =
    mine.length === 0 ? null : (
      <div
        data-testid="queued-messages"
        className="mx-auto w-full max-w-[820px] mb-2 flex flex-col gap-1"
      >
        <div className="text-[12px]" style={{ color: 'var(--ink-faint)' }}>
          {mine.length === 1
            ? 'Sends as a follow-up when this turn finishes'
            : `${mine.length} follow-ups send together when this turn finishes`}
        </div>
        {/* ⚠ Remove by the index into the FULL list, not this filtered view —
            another thread's queued items shift the positions. */}
        {mine.map(({ text, index }) => (
          <div
            key={index}
            data-testid="queued-message"
            className="flex items-start gap-2 rounded-[var(--r-sm)] px-2.5 py-1.5 text-[13px]"
            style={{
              background: 'var(--canvas-elevated)',
              border: '1px solid var(--line)',
              color: 'var(--ink-subtle)',
            }}
          >
            <span className="flex-1 min-w-0 break-words">{text}</span>
            <button
              type="button"
              aria-label="Remove queued message"
              onClick={() => removeQueuedMessage(index)}
              className="shrink-0 cursor-pointer"
              style={{ color: 'var(--ink-faint)' }}
            >
              ×
            </button>
          </div>
        ))}
      </div>
    );

  // PR #119 — embedded (chat landing) gets a calm flat composer
  // surface to match the mockup: no conic-gradient border, no large
  // iris box-shadow, no focus iris-ring. The conic-glow treatment
  // is kept for the in-thread bottom composer (non-embedded) where
  // it cues "this is where you reply." Chips also move to a
  // sibling row OUTSIDE the composer in embedded mode.
  if (embedded) {
    return (
      <div className="w-full" {...dropProps}>
        {queueStrip}
        {stagedStrip}
        <div
          data-testid="chat-composer-landing"
          onMouseDown={focusFieldOnPadding}
          className="relative flex flex-col gap-2 rounded-[var(--r-md)] px-4 pt-3.5 pb-3 transition-shadow mx-auto w-full cursor-text focus-within:shadow-[0_0_0_3px_var(--iris-ring)]"
          style={{
            // Commit 3 — composer is a resting surface, not a state:
            // white + card-edge, brand colour only on the send button
            // + focus ring.
            background: 'var(--canvas)',
            border: '1px solid var(--card-edge)',
          }}
        >
          {hiddenFileInput}
          {dropOverlay}
          <ComposerField
            value={input}
            onChange={setInput}
            onKeyDown={handleKeyDown}
            placeholder={placeholder}
          />
          <div className="flex items-center justify-between gap-3">
            <button
              type="button"
              aria-label="Attach a document"
              title="Attach a PDF or image"
              onClick={handleAttachClick}
              disabled={isRunning}
              className="shrink-0 flex items-center justify-center w-8 h-8 -ml-1.5 rounded-[var(--r-sm)] transition-colors duration-150 cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed hover:bg-[var(--canvas-hover)]"
              style={{ color: 'var(--ink-subtle)' }}
            >
              <Plus size={18} strokeWidth={1.75} />
            </button>

            {isRunning ? (
              <button
                type="button"
                aria-label="Stop agent"
                title="Stop"
                onClick={handleStop}
                className="shrink-0 flex items-center justify-center w-9 h-9 rounded-[var(--r-sm)] transition-colors cursor-pointer"
                style={{
                  background: 'var(--danger-bg)',
                  color: 'var(--danger)',
                  border: '1px solid rgba(248, 113, 113, 0.25)',
                }}
              >
                <Square size={11} strokeWidth={2.5} fill="currentColor" />
              </button>
            ) : (
              <button
                type="button"
                aria-label="Send"
                title={canSend ? 'Send' : 'Type a prompt to send'}
                onClick={handleSubmit}
                disabled={!canSend}
                data-testid="chat-composer-send"
                // A labelled button (brief, 2026-09-29): blue with "Send" when
                // there is something to send, gray when the box is empty.
                className="shrink-0 h-9 px-4 rounded-[var(--r-sm)] text-[14px] font-medium transition-colors disabled:cursor-not-allowed cursor-pointer"
                style={SEND_STYLE(canSend)}
              >
                Send
              </button>
            )}
          </div>
        </div>
      </div>
    );
  }

  return (
    // Utility classes throughout: the old inline paddingLeft/Right: 48
    // predated the reset moving into @layer base — inline styles beat the
    // max-md:px-3 class, so the composer clipped against the send button
    // at 390px (Stage 3 audit). Classes let the mobile override win.
    <div className="px-12 pt-3 pb-5 max-md:px-3 max-md:pb-3" {...dropProps}>
      {queueStrip}
      {stagedStrip}
      <div
        className="relative flex flex-col rounded-[var(--r-lg)] transition-[box-shadow] duration-150 w-full max-w-[820px]"
        style={{
          marginInline: 'auto',
          // Flat surface + 1px border (was a conic-gradient "rainbow"
          // border). Depth comes from a soft elevation shadow; the brand
          // colour appears only as the focus ring, per the design brief.
          background: 'var(--canvas)',
          border: '1px solid var(--card-edge)',
          boxShadow: 'var(--shadow-md)',
        }}
        onFocusCapture={(e) => {
          const el = e.currentTarget as HTMLElement;
          el.style.boxShadow = '0 0 0 3px var(--iris-ring), var(--shadow-md)';
        }}
        onBlurCapture={(e) => {
          const el = e.currentTarget as HTMLElement;
          el.style.boxShadow = 'var(--shadow-md)';
        }}
      >
        {dropOverlay}
        {/* Text on top, controls underneath: the field wraps and grows
            instead of scrolling sideways. */}
        <div
          className="flex flex-col gap-1.5 pl-3.5 pr-2 pt-3 pb-2 cursor-text"
          onMouseDown={focusFieldOnPadding}
        >
          {hiddenFileInput}
          <div className="pr-1.5">
            <ComposerField
              value={input}
              onChange={setInput}
              onKeyDown={handleKeyDown}
              placeholder={placeholder}
            />
          </div>
          <div className="flex items-center justify-between gap-2">
            {/* Attach a document → in-chat extraction */}
            <button
              type="button"
              aria-label="Attach a document"
              title="Attach a PDF or image"
              onClick={handleAttachClick}
              disabled={isRunning}
              className="shrink-0 flex items-center justify-center w-8 h-8 -ml-1.5 rounded-[var(--r-sm)] transition-colors duration-150 disabled:opacity-40 disabled:cursor-not-allowed hover:bg-[var(--canvas-hover)]"
              style={{ color: 'var(--ink-subtle)' }}
            >
              <Plus size={15} strokeWidth={1.75} />
            </button>

            {isRunning ? (
              <motion.button
                whileTap={{ scale: 0.94 }}
                transition={{ duration: 0.12, ease: [0.2, 0.8, 0.2, 1] }}
                type="button"
                aria-label="Stop agent"
                onClick={handleStop}
                className="shrink-0 flex items-center justify-center w-9 h-9 rounded-[var(--r-sm)] transition-colors duration-150"
                style={{
                  background: 'var(--danger-bg)',
                  color: 'var(--danger)',
                  border: '1px solid rgba(248, 113, 113, 0.25)',
                }}
              >
                <motion.span
                  animate={{ opacity: [1, 0.6, 1] }}
                  transition={{
                    duration: 1.2,
                    repeat: Infinity,
                    ease: 'easeInOut',
                  }}
                  className="flex items-center justify-center"
                >
                  <Square size={11} strokeWidth={2.5} fill="currentColor" />
                </motion.span>
              </motion.button>
            ) : (
              <motion.button
                whileTap={canSend ? { scale: 0.96 } : undefined}
                transition={{ duration: 0.12, ease: [0.2, 0.8, 0.2, 1] }}
                type="button"
                data-testid="chat-composer-send"
                aria-label="Send"
                onClick={handleSubmit}
                disabled={!canSend}
                className="shrink-0 h-9 px-4 rounded-[var(--r-sm)] text-[14px] font-medium transition-colors duration-150 disabled:cursor-not-allowed"
                style={SEND_STYLE(canSend)}
              >
                Send
              </motion.button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
