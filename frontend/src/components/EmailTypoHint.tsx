/**
 * "Did you mean sam@gmail.com?" — one line, under an email field.
 *
 * ⚠ **Quiet by construction**, which is the only reason it is shippable.
 * `suggestEmail` returns null for anything merely UNFAMILIAR, so a real
 * company domain never sees this line — it fires on a near-miss against a
 * known consumer domain and on a mistyped TLD, and on nothing else. A hint
 * that fires on correct input is one people learn to ignore, and then it is
 * not there on the day it would have helped.
 *
 * ⚠ Extracted from `LoginForm` rather than copied into the second caller.
 * The interesting half is that the two places it belongs have very different
 * costs. On sign-in a typo shows up immediately: the password fails and you
 * try again. On an INVITE nothing fails — the address is well-formed, the API
 * accepts it, an email goes to a mailbox that does not exist, and the team
 * screen carries an "Invite pending" row for somebody who was never asked.
 * Nobody finds out until they wonder why Sam never joined.
 */

import { useMemo } from 'react';

import { suggestEmail } from '../lib/emailTypo';

export function EmailTypoHint({
  email,
  onAccept,
  className = '',
}: {
  email: string;
  /** Called with the corrected address when the person accepts it. */
  onAccept: (value: string) => void;
  className?: string;
}) {
  const suggestion = useMemo(() => suggestEmail(email), [email]);
  if (!suggestion) return null;
  return (
    <div
      className={`text-[13px] -mt-1 pl-0.5 ${className}`}
      style={{ color: 'var(--ink-muted)' }}
      data-testid="email-typo-hint"
    >
      Did you mean{' '}
      <button
        type="button"
        onClick={() => onAccept(suggestion)}
        className="underline underline-offset-2 font-medium transition-colors"
        style={{ color: 'var(--iris)' }}
      >
        {suggestion}
      </button>
      ?
    </div>
  );
}
