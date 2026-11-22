/**
 * "It looks wrong on my screen" — the copyable answer to it.
 *
 * When a customer reports something odd, the questions support actually needs
 * are always the same: which workspace, which user, what role, which build,
 * which browser. Asking for those one at a time over email costs a day; a
 * button that puts them on the clipboard costs a click.
 *
 * ⚠ **This is deliberately the thing that is NOT impersonation.** The obvious
 * fix for "let me see what they see" is an operator switch into a customer's
 * account, and it is a genuine cross-tenant read path in a multi-tenant
 * product. This solves the same support problem from the other end — the
 * CUSTOMER hands over the context, deliberately, and no privilege escalation
 * exists to be misused or compromised. See `SupportDiagnosticsRow` for the
 * decision that is deliberately not being made here.
 *
 * ⚠ **NOTHING SECRET, EVER.** The whole value is that this can be pasted into
 * an email, a chat, a screenshot, a public forum thread. So no access token,
 * no refresh token, no API key, no anon key, and no other person's details —
 * only facts about the person clicking and the workspace they are in. The
 * test for this asserts on the OUTPUT, not on intent, because "I did not mean
 * to include a token" is not a property a reader can check.
 */

export interface DiagnosticsInput {
  userId: string | null;
  email: string | null;
  companyId: string | null;
  companyRole: string | null;
  companyCount: number;
  appVersion: string;
  userAgent: string;
  /** ISO. Passed in rather than read here so the output is testable. */
  now: string;
  /** The app's current view, which is usually where the problem is. */
  view?: string | null;
}

/** Keys that must never appear in the output, whatever anyone adds later. */
const FORBIDDEN = [
  'token',
  'secret',
  'password',
  'apikey',
  'api_key',
  'anon',
  'bearer',
  'authorization',
  'jwt',
];

/**
 * A plain-text block. Not JSON: this gets pasted into an email by a property
 * manager, and a wall of braces reads as something they were not supposed to
 * send.
 */
export function buildDiagnostics(input: DiagnosticsInput): string {
  const lines = [
    'Occupella support details',
    `Time:      ${input.now}`,
    `Build:     ${input.appVersion || 'unknown'}`,
    `User:      ${input.email ?? 'not signed in'}`,
    `User ID:   ${input.userId ?? '—'}`,
    `Workspace: ${input.companyId ?? '—'}`,
    `Role:      ${input.companyRole ?? 'unknown'}`,
    `Workspaces:${' '}${input.companyCount}`,
    `Screen:    ${input.view ?? '—'}`,
    `Browser:   ${input.userAgent || 'unknown'}`,
  ];
  return lines.join('\n');
}

/**
 * Belt-and-braces: the same check the test makes, callable at runtime.
 *
 * ⚠ Not decoration. The failure this catches is somebody adding a genuinely
 * useful-looking field later — a session id, a "current token for debugging" —
 * and it reaching a clipboard nobody audits. Returning the offending word
 * rather than a boolean means the failure names itself.
 */
export function forbiddenTermIn(text: string): string | null {
  const lower = text.toLowerCase();
  for (const term of FORBIDDEN) {
    if (lower.includes(term)) return term;
  }
  return null;
}
