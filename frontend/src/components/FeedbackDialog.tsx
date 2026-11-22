/**
 * Feedback dialog (BR #19) — category chips + a textarea, nothing else.
 * Opened from the sidebar footer; submits to POST /feedback and toasts.
 * DESIGN.md: token palette, 1px var(--line) borders, plain copy.
 *
 * Rendered through a PORTAL to document.body — the opener lives inside the
 * sidebar, which animates width/left; a `fixed` overlay rendered inside an
 * animated/positioned ancestor can get trapped in that container's box
 * (broken half-overlay, controls off-screen, no way out but a refresh).
 * The portal takes the overlay out of that subtree entirely, and Escape +
 * backdrop-click guarantee there is always a way to close it.
 */

import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import { toast } from 'sonner';
import {
  FeedbackRejected,
  MAX_MESSAGE_CHARS,
  MIN_MESSAGE_CHARS,
  submitFeedback,
  type FeedbackCategory,
} from '../services/feedback';
import { useFocusTrap } from '../hooks/useFocusTrap';
import { useStore } from '../stores/store';
import { FormError } from './FormError';

const CATEGORIES: { value: FeedbackCategory; label: string }[] = [
  { value: 'bug', label: 'Something broke' },
  { value: 'idea', label: 'An idea' },
  { value: 'other', label: 'Other' },
];

/**
 * A caller can open this dialog already pointed at one kind of feedback —
 * today, "which integration should we build next" from the Connectors panel.
 *
 * ⚠ `category` deliberately stays inside the THREE the backend accepts.
 * `helixis_feedback.category` carries a CHECK constraint (migration 0048), and
 * migrations here are applied by hand with no runner (gotcha 8) — a fourth
 * value would 422 on production from the moment this shipped until somebody
 * remembered to run the SQL. So a connector request is an `idea` tagged by
 * `view`, which is free text, and triage filters on that.
 */
export interface FeedbackPreset {
  category: FeedbackCategory;
  /** Overrides the current app view as the row's `view` — the triage tag. */
  view: string;
  title: string;
  placeholder: string;
  /** One line under the field saying what happens to the answer. */
  hint?: string;
  /** Hide the category chips when the caller has already decided. */
  lockCategory?: boolean;
}

export function FeedbackDialog({
  onClose,
  preset,
}: {
  onClose: () => void;
  preset?: FeedbackPreset;
}) {
  const currentView = useStore((s) => s.view);
  const view = preset?.view ?? currentView;
  // ⚠ Read from the store, not passed in by every caller. A "report this"
  // affordance added later must not have to remember to thread the id through
  // — the thing that makes a report diagnosable has to be automatic, or it is
  // present on the reports that needed it least.
  const sessionId = useStore((s) => s.activeThreadId);
  const [category, setCategory] = useState<FeedbackCategory>(preset?.category ?? 'bug');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const trimmed = message.trim();
  const over = message.length - MAX_MESSAGE_CHARS;

  const send = async () => {
    if (busy) return;
    // ⚠ Both refusals SAY something. The old guard returned silently on a
    // short message, so the button moved and nothing happened — which is
    // indistinguishable from a send that failed, and the person's next move is
    // to click it again (gotcha 44).
    if (trimmed.length < MIN_MESSAGE_CHARS) {
      setError(`Add a bit more detail — at least ${MIN_MESSAGE_CHARS} characters.`);
      return;
    }
    if (over > 0) {
      setError(
        `That is ${over.toLocaleString()} characters over the limit. ` +
          `Trim it, or email team@occupella.com if it needs to be this long.`,
      );
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await submitFeedback(category, message.trim(), view, sessionId);
      // ⚠ NOT "thank you" and nothing else. This is the only support channel
      // inside the product, and the old wording implied a queue somebody is
      // watching — a report lands in a table and nothing pages anyone, so a
      // customer stuck at 9pm who sent this and waited would be waiting on a
      // thing that does not happen. Naming the address gives them a route
      // that genuinely reaches a person, and saying "next business day" is a
      // bound they can plan against instead of a silence they have to guess
      // at. Both halves stay true only while the address is read by a human;
      // if that changes, this copy changes with it.
      toast.success(
        'Sent — thanks. We read these and reply by the next business day. ' +
          'If it is urgent, email team@occupella.com.',
        { duration: 8000 },
      );
      onClose();
    } catch (e) {
      // ⚠ "Try again" is the right advice for a dropped connection and the
      // WRONG advice for a message the server will refuse every time. Telling
      // somebody to retry something that cannot succeed is how a report gets
      // abandoned rather than emailed (gotcha 46's class, one surface over).
      setError(
        e instanceof FeedbackRejected
          ? e.message
          : "Couldn't send that — please try again, or email team@occupella.com.",
      );
      setBusy(false);
    }
  };

  // Escape always closes — the dialog must never require a page refresh.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  // Tab stays inside the card, and focus goes back to whatever opened it.
  const panel = useRef<HTMLDivElement>(null);
  useFocusTrap(true, panel);

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: 'color-mix(in srgb, var(--canvas) 60%, transparent)' }}
      data-testid="feedback-dialog"
      onClick={(e) => {
        // Backdrop click closes; clicks inside the card don't bubble here.
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="w-full max-w-[440px] rounded-[var(--r-md)] border"
        style={{
          borderColor: 'var(--card-edge)',
          background: 'var(--canvas)',
          boxShadow: 'var(--shadow-md)',
        }}
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label={preset?.title ?? 'Send feedback'}
      >
        <div
          className="flex items-center justify-between px-4 py-2.5 border-b"
          style={{ borderColor: 'var(--line)' }}
        >
          <span className="text-[13px] font-medium" style={{ color: 'var(--ink)' }}>
            {preset?.title ?? 'Send feedback'}
          </span>
          <button type="button" aria-label="Close" onClick={onClose} style={{ color: 'var(--ink-muted)' }}>
            <X size={15} strokeWidth={1.7} />
          </button>
        </div>

        <div className="px-4 py-3 flex flex-col gap-3">
          {!preset?.lockCategory && (
          <div className="flex gap-2">
            {CATEGORIES.map((c) => (
              <button
                key={c.value}
                type="button"
                onClick={() => setCategory(c.value)}
                className="rounded-[var(--r-sm)] border px-2.5 py-1 text-[12px]"
                style={{
                  borderColor: category === c.value ? 'var(--iris)' : 'var(--line)',
                  color: category === c.value ? 'var(--ink)' : 'var(--ink-muted)',
                }}
              >
                {c.label}
              </button>
            ))}
          </div>
          )}
          <textarea
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            rows={5}
            autoFocus
            placeholder={
              preset?.placeholder ?? 'What happened, or what would make Occupella better?'
            }
            className="w-full rounded-[var(--r-sm)] border px-2 py-1.5 text-[13px] resize-y"
            style={{ borderColor: 'var(--line)', background: 'var(--canvas)', color: 'var(--ink)' }}
          />
          {preset?.hint && (
            <div className="text-[12px]" style={{ color: 'var(--ink-faint)' }}>
              {preset.hint}
            </div>
          )}
          {/* ⚠ The counter appears only near the limit. A permanent "0 / 5,000"
              on a box nobody will fill is noise that trains people to ignore
              the row — and the row's whole job is to be noticed by the one
              person writing a report long enough to be refused. */}
          {message.length > MAX_MESSAGE_CHARS * 0.9 && (
            <div
              className="text-[12px] tabular-nums"
              style={{ color: over > 0 ? 'var(--danger)' : 'var(--ink-faint)' }}
              data-testid="feedback-counter"
            >
              {over > 0
                ? `${over.toLocaleString()} over the ${MAX_MESSAGE_CHARS.toLocaleString()}-character limit`
                : `${message.length.toLocaleString()} / ${MAX_MESSAGE_CHARS.toLocaleString()}`}
            </div>
          )}
          {error && <FormError>{error}</FormError>}
        </div>

        <div
          className="flex items-center justify-between gap-3 px-4 py-2.5 border-t"
          style={{ borderColor: 'var(--line)' }}
        >
          {/* The build, visible and attached. Two jobs in one line: it is the
              app's only "version number somewhere" (nothing else showed one,
              so a report could never say which build it came from), and it
              tells the person what is being sent with their words. A silent
              attachment of the browser string is the kind of thing a user
              should be able to see, not discover. */}
          <span
            className="text-[12px] tabular-nums"
            style={{ color: 'var(--ink-faint)' }}
            title="Sent with your report so we can find the exact build and conversation"
          >
            Build {__APP_VERSION__}
            {sessionId ? ' · this chat' : ''}
          </span>
          <button
            type="button"
            onClick={send}
            // ⚠ Only `busy`, which shows "Sending…" so the state is legible.
            // Length is deliberately NOT a disabled condition: a disabled
            // button swallows the click before any code runs, so there is
            // nowhere left to put the explanation, and the person is left
            // clicking a dead control with no idea what is wrong. That rule is
            // InviteUsersDialog's, written down in its own header — every
            // refusal has to be something a person can read.
            disabled={busy}
            className="rounded-[var(--r-sm)] border px-3 py-1.5 text-[13px] font-medium hover:opacity-80 disabled:opacity-50"
            style={{ borderColor: 'var(--line)', background: 'var(--canvas)', color: 'var(--ink)' }}
          >
            {busy ? 'Sending…' : 'Send'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
