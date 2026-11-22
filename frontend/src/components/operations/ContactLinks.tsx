/**
 * Call / text / email a person, from wherever their row is shown.
 *
 * Every Operations tab already had the address on the row and rendered it as
 * dead grey text, so reaching a tenant meant selecting an email by hand and
 * pasting it somewhere else. The data was fetched and thrown away at the last
 * step — `TenantOpsOut`, `VendorOpsOut` and `TeamOpsOut` have carried `email`
 * (and `phone`, for the first two) the whole time.
 *
 * ⚠ THESE ARE HANDOFFS TO THE USER'S OWN DEVICE, NOT SENDS.
 * `tel:` / `sms:` / `mailto:` open the manager's phone or mail client with the
 * recipient filled in; the manager types and sends it themselves. That is a
 * different thing from Occupella sending, and the distinction is load-bearing
 * rather than pedantic:
 *
 *   - `comms/sms.py` is dark (`HELIXIS_TWILIO__ENABLED` false, pending A2P
 *     approval) and `helixis_sms_consent` holds ZERO rows, so `send_sms` fails
 *     closed for every recipient in the product today. A "Text" control wired
 *     to our own send path could not work for anybody.
 *   - Nothing here touches the consent ledger, writes `helixis_sms_log`, or
 *     claims a message was delivered. Do not later re-point these at
 *     `send_text_message` without the consent gate that tool already carries.
 *
 * So the label is what the control does — it opens the app — and the row never
 * asserts that a message went anywhere.
 *
 * A missing channel renders NOTHING rather than a disabled control: 75 of 137
 * tenants have no phone on file, and a greyed-out "Call" on three quarters of
 * a table reads as breakage rather than as absence.
 */

import { Mail, MessageSquare, Phone } from 'lucide-react';

export interface ContactLinksProps {
  email?: string | null;
  phone?: string | null;
  /** ``row`` is the compact inline form used inside a table cell; ``detail``
   *  is the larger form for a header, where the address is spelled out. */
  variant?: 'row' | 'detail';
  testIdPrefix?: string;
}

/**
 * Strip a phone number down to what `tel:` / `sms:` accept.
 *
 * ⚠ Returns null rather than a guess. Mirror phone values are whatever
 * Buildium held — "(555) 010-9999 ext 4" and "n/a" both occur in free-text
 * fields — and a `tel:` built from junk dials junk. Requires at least 7 digits
 * (the shortest real subscriber number) so a stray "1" or an extension
 * fragment cannot become a call button.
 */
export function telHref(phone: string | null | undefined): string | null {
  if (!phone) return null;
  const digits = phone.replace(/[^\d+]/g, '');
  const bare = digits.replace(/\D/g, '');
  if (bare.length < 7) return null;
  return digits.startsWith('+') ? `+${bare}` : bare;
}

/**
 * ⚠ Presence of an "@" is not validity, and this deliberately does not try to
 * be RFC 5322. It rejects the shapes the mirror actually produces for "no
 * email" — an empty string, a bare word, a trailing "@" — so a `mailto:` never
 * opens a compose window addressed to nothing.
 */
export function mailHref(email: string | null | undefined): string | null {
  if (!email) return null;
  const t = email.trim();
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(t)) return null;
  return t;
}

export function ContactLinks({
  email,
  phone,
  variant = 'row',
  testIdPrefix = 'contact',
}: ContactLinksProps) {
  const tel = telHref(phone);
  const mail = mailHref(email);
  if (!tel && !mail) return null;

  const detail = variant === 'detail';
  const size = detail ? 13 : 12;
  const cls = detail
    ? 'inline-flex items-center gap-1.5 text-[13px]'
    : 'inline-flex items-center gap-1 text-[12px]';

  // Iris is DESIGN.md's link colour. It stays on the ICON only in the row
  // form: a table of 137 rows each carrying three accent-coloured words is
  // the "accent on two competing elements" the spec bans, and the column that
  // deserves the accent there is the name.
  const linkStyle = { color: detail ? 'var(--iris)' : 'var(--ink-muted)' };

  return (
    <span
      className={detail ? 'flex flex-wrap items-center gap-4' : 'flex flex-wrap items-center gap-3'}
      data-testid={`${testIdPrefix}-links`}
    >
      {tel && (
        <a href={`tel:${tel}`} className={cls} style={linkStyle} data-testid={`${testIdPrefix}-call`}>
          <Phone size={size} strokeWidth={1.75} aria-hidden />
          {detail ? phone : 'Call'}
        </a>
      )}
      {tel && (
        <a href={`sms:${tel}`} className={cls} style={linkStyle} data-testid={`${testIdPrefix}-text`}>
          <MessageSquare size={size} strokeWidth={1.75} aria-hidden />
          Text
        </a>
      )}
      {mail && (
        <a
          href={`mailto:${mail}`}
          className={cls}
          style={linkStyle}
          data-testid={`${testIdPrefix}-email`}
          // The address itself in the detail header (it is the useful fact
          // there); the verb in a row, where the column is 200px wide.
          title={mail}
        >
          <Mail size={size} strokeWidth={1.75} aria-hidden />
          {detail ? mail : 'Email'}
        </a>
      )}
    </span>
  );
}
