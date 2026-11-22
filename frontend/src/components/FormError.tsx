/**
 * The one way this app tells somebody a form failed.
 *
 * ⚠ **Thirteen error messages announced nothing and were red and nothing
 * else.** Two separate failures, in the same markup, at the same thirteen
 * places:
 *
 *  * A screen reader is never told. The message appears in the DOM after a
 *    failed submit, focus is still on the button, and nothing reads it out —
 *    so the form silently did nothing, which is exactly the state a person
 *    reacts to by pressing the button again. `role="alert"` is what makes an
 *    inserted node get announced.
 *  * Colour is the ONLY signal that it is an error. Small `--danger` text
 *    against `--ink` is a hue difference, and roughly one in twelve men cannot
 *    reliably see it — for them the failure reads as an ordinary caption. The
 *    icon is the second channel.
 *
 * ⚠ The icon is `aria-hidden`. `role="alert"` already announces the text, and a
 * screen reader saying "alert, warning triangle, that code was not accepted"
 * spends the listener's attention on decoration. The icon is there for the eye
 * that cannot use the colour, and for no one else.
 *
 * ⚠ Use this for a message a person needs to ACT on. It is deliberately not a
 * general "red text" component: `role="alert"` interrupts whatever a screen
 * reader is currently saying, and spending that on a hint or a caption is how
 * people learn to tune the role out.
 */

import { AlertCircle } from 'lucide-react';

export function FormError({
  children,
  className = '',
}: {
  children: React.ReactNode;
  /** Layout only — spacing at the call site. Never colour. */
  className?: string;
}) {
  // Nothing rendered means nothing announced, which is correct: an empty alert
  // region that exists permanently is announced on every unrelated re-render.
  if (!children) return null;
  return (
    <p
      role="alert"
      className={`flex items-start gap-1.5 text-[12px] leading-snug ${className}`}
      style={{ color: 'var(--danger)' }}
      data-testid="form-error"
    >
      <AlertCircle
        size={13}
        strokeWidth={1.9}
        aria-hidden="true"
        className="shrink-0 mt-[1px]"
      />
      <span>{children}</span>
    </p>
  );
}
