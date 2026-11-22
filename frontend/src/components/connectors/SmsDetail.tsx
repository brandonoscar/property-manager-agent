/**
 * Text messaging panel — consent ledger management (Stage 3).
 *
 * ⚠ **CURRENTLY UNREACHABLE.** The Settings connector row that opened this was
 * removed when messaging was white-labeled out of the customer's hands, so
 * nothing imports this file today. It is kept rather than deleted because the
 * capability is real and still needed: this is the only surface where a human
 * can SEE and revoke TCPA consent. Consent enforcement is unaffected --
 * STOP/START write the ledger from the inbound webhook and `send_sms` fails
 * closed without a live row -- but the review UI needs a new home. Delete this
 * file only after deciding consent review does not belong in the product.
 *
 * ⚠ White-labeled: no user-visible string here names the carrier. The
 * account is ours; the customer has a number on it and a consent ledger
 * they own. Internal identifiers still say twilio, correctly — an engineer
 * reading this needs to know which vendor it is.
 *
 * Opened from the Settings → Connectors "Text messaging" row (SettingsPage
 * routes id 'twilio' here instead of ComingSoonDetail). Three parts:
 *
 * 1. Status line — Dark (awaiting carrier approval) vs Live, from
 *    ``GET /sms/status`` (the A2P flag + configured creds).
 * 2. Consent table — the company's TCPA ledger (name, phone, kind,
 *    Active/Revoked, date) with a confirm-gated Revoke per active row.
 *    Revoking never deletes: the row stays, marked revoked.
 * 3. Add form — phone / name / kind / timezone, whose submit is disabled
 *    until the explicit consent-language checkbox is ticked (the A2P
 *    campaign's declared opt-in flow for 'app_settings' consent).
 */

import { useEffect, useState } from 'react';
import { toast } from 'sonner';

import {
  addSmsConsent,
  fetchSmsStatus,
  listSmsConsent,
  revokeSmsConsent,
  OUTCOME_LABEL,
  RECIPIENT_KINDS,
  US_TIMEZONES,
  type RecipientKind,
  type SmsConsent,
  type SmsStatus,
} from '../../services/sms';
import { DetailShell } from './DetailShell';

// The four required consent disclosures (message type, frequency, rates,
// opt-out) with the Sept-2026 verbatim opt-out phrase "Reply STOP to cancel".
const CONSENT_LANGUAGE =
  'This person has agreed to receive account-notification text messages ' +
  'from Occupella. Message frequency varies, message and data rates may ' +
  'apply. Reply STOP to cancel, HELP for help.';

const inputCls = 'h-9 px-3 rounded-[var(--r-sm)] border text-[13px] outline-none';
const inputStyle = {
  background: 'var(--canvas)',
  color: 'var(--ink)',
  borderColor: 'var(--line)',
} as const;

const KIND_LABELS: Record<RecipientKind, string> = {
  manager: 'Manager',
  tenant: 'Tenant',
  owner: 'Owner',
  vendor: 'Vendor',
};

function formatDate(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function StatusLine({ status }: { status: SmsStatus | null }) {
  if (status === null) {
    return (
      <div className="text-[13px]" style={{ color: 'var(--ink-subtle)' }}>
        Checking channel status…
      </div>
    );
  }
  const live = status.enabled && status.configured;
  return (
    <div className="flex items-center gap-2">
      <span
        className=" uppercase px-1.5 py-0.5 rounded-[var(--r-xs)]"
        style={{
          fontSize: 12,
          letterSpacing: '0.14em',
          background: live ? 'var(--positive-bg)' : 'var(--canvas-raised)',
          color: live ? 'var(--positive)' : 'var(--ink-muted)',
        }}
        data-testid="sms-status-badge"
      >
        {live ? 'Live' : 'Dark'}
      </span>
      <span className="text-[13px]" style={{ color: 'var(--ink-muted)' }}>
        {live
          ? 'Outbound texting is live — consented numbers receive account notifications.'
          : status.configured
            ? 'Awaiting carrier approval of the A2P campaign. Consent collected now applies at launch.'
            : 'Texting is not switched on for this account yet. Consent collected now applies at launch.'}
      </span>
    </div>
  );
}

export function SmsDetail({ onClose }: { onClose: () => void }) {
  const [status, setStatus] = useState<SmsStatus | null>(null);
  const [consents, setConsents] = useState<SmsConsent[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Add form.
  const [phone, setPhone] = useState('');
  const [name, setName] = useState('');
  const [kind, setKind] = useState<RecipientKind>('manager');
  const [timezone, setTimezone] = useState('America/New_York');
  const [agreed, setAgreed] = useState(false);
  // "This is my own phone." Never inferred from who is signed in — see the
  // route's note: recording the submitter as the owner is wrong for every
  // number entered on somebody else's behalf, and would revoke a colleague's
  // alerts when the person who typed their number leaves.
  const [ownNumber, setOwnNumber] = useState(false);
  const [saving, setSaving] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

  const refresh = () => {
    Promise.all([fetchSmsStatus(), listSmsConsent()])
      .then(([s, rows]) => {
        setStatus(s);
        setConsents(rows);
        setLoadError(null);
      })
      .catch((err) => {
        setLoadError(err instanceof Error ? err.message : 'Could not load SMS settings.');
      });
  };

  useEffect(refresh, []);

  const canSubmit = agreed && phone.trim().length > 0 && !saving;

  const handleAdd = async () => {
    if (!canSubmit) return;
    setSaving(true);
    try {
      await addSmsConsent({
        phone: phone.trim(),
        display_name: name.trim() || undefined,
        recipient_kind: kind,
        timezone,
        is_own_number: ownNumber,
      });
      toast.success('Consent recorded.');
      setPhone('');
      setName('');
      setAgreed(false);
      setOwnNumber(false);
      refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not record consent.', {
        duration: 8000,
      });
    } finally {
      setSaving(false);
    }
  };

  const handleRevoke = async (row: SmsConsent) => {
    if (busyId) return;
    const who = row.display_name || row.phone;
    if (!window.confirm(`Revoke text-message consent for ${who}? They will no longer receive SMS.`))
      return;
    setBusyId(row.id);
    try {
      await revokeSmsConsent(row.id);
      toast.success(`Revoked consent for ${who}.`);
      refresh();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not revoke consent.', {
        duration: 8000,
      });
    } finally {
      setBusyId(null);
    }
  };

  return (
    <DetailShell
      title="Text messaging"
      subtitle="Account-notification texting under the registered A2P campaign — consent-gated, templates-only."
      onClose={onClose}
      errorBanner={loadError}
      wide
    >
      <div className="mb-5">
        <StatusLine status={status} />
      </div>

      {/* Consent ledger */}
      <div
        className="text-[12px] font-medium uppercase tracking-wider mb-2"
        style={{ color: 'var(--ink-muted)' }}
      >
        Consent ledger
      </div>
      {consents === null && !loadError && (
        <div className="text-[13px] py-2" style={{ color: 'var(--ink-subtle)' }}>
          Loading…
        </div>
      )}
      {consents !== null && consents.length === 0 && (
        <div className="text-[13px] py-2" style={{ color: 'var(--ink-subtle)' }}>
          No one has consented yet. Add a number below.
        </div>
      )}
      {consents !== null && consents.length > 0 && (
        <div
          className="rounded-[var(--r-md)] border overflow-x-auto mb-2"
          style={{ borderColor: 'var(--card-edge)', background: 'var(--canvas)' }}
        >
          <table className="w-full text-[13px]" style={{ color: 'var(--ink)' }}>
            <thead>
              <tr
                className="text-left uppercase"
                style={{ fontSize: 12, letterSpacing: '0.12em', color: 'var(--ink-subtle)' }}
              >
                <th className="px-3 py-2 font-medium">Name</th>
                <th className="px-3 py-2 font-medium">Phone</th>
                <th className="px-3 py-2 font-medium">Kind</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 font-medium">Last message</th>
                <th className="px-3 py-2 font-medium">Date</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody>
              {consents.map((row) => {
                const revoked = row.revoked_at !== null;
                return (
                  <tr key={row.id} className="border-t" style={{ borderColor: 'var(--line)' }}>
                    <td className="px-3 py-2">{row.display_name || '—'}</td>
                    <td className="px-3 py-2 font-mono tabular-nums">{row.phone}</td>
                    <td className="px-3 py-2" style={{ color: 'var(--ink-muted)' }}>
                      {KIND_LABELS[row.recipient_kind as RecipientKind] ?? row.recipient_kind}
                      {/* A live STAFF number nobody has claimed is one that
                          keeps receiving the manager digest no matter who
                          leaves — the ledger does not know whose it is, so no
                          departure can revoke it. Saying so is the honest
                          counterpart to the automatic revocation; a gap the
                          product hides is one nobody closes. */}
                      {!revoked &&
                        row.recipient_kind === 'manager' &&
                        !row.owner_user_id && (
                          <span
                            className="block"
                            data-testid="sms-unattributed"
                            style={{ fontSize: 12, color: 'var(--ink-subtle)' }}
                            title="Nobody has claimed this number, so it keeps receiving alerts even if its owner leaves."
                          >
                            unassigned
                          </span>
                        )}
                    </td>
                    <td className="px-3 py-2">
                      <span
                        className=" uppercase px-1.5 py-0.5 rounded-[var(--r-xs)]"
                        style={{
                          fontSize: 12,
                          letterSpacing: '0.14em',
                          background: revoked ? 'var(--canvas-raised)' : 'var(--positive-bg)',
                          color: revoked ? 'var(--ink-muted)' : 'var(--positive)',
                        }}
                      >
                        {revoked ? 'Revoked' : 'Active'}
                      </span>
                    </td>
                    {/* `helixis_sms_log` carried the delivery outcome from
                        day one and nothing read it: a landline came back
                        `undelivered` (30006), was written down, and the number
                        went on sitting here looking healthy. This is the place
                        somebody can act on it — a bad number gets replaced, a
                        silent one gets re-sent, and those are different fixes.

                        ⚠ Null means NO MESSAGES SENT, not "fine": rendering an
                        untested number as delivered is the same lie the log
                        was already telling by being unread. */}
                    <td className="px-3 py-2" data-testid="sms-last-outcome">
                      {row.last_outcome ? (
                        <span
                          style={{
                            color:
                              row.last_outcome === 'delivered'
                                ? 'var(--positive)'
                                : row.last_outcome === 'pending'
                                  ? 'var(--ink-muted)'
                                  : 'var(--caution)',
                          }}
                          title={
                            row.last_error_code
                              ? `Carrier error ${row.last_error_code}${
                                  row.last_error_code === '30006'
                                    ? ' — this looks like a landline, which cannot receive texts.'
                                    : ''
                                }`
                              : row.last_outcome === 'no_receipt'
                                ? 'It was accepted for delivery and never confirmed. Carriers filter messages silently, so no receipt comes back.'
                                : undefined
                          }
                        >
                          {OUTCOME_LABEL[row.last_outcome]}
                        </span>
                      ) : (
                        <span style={{ color: 'var(--ink-subtle)' }}>No messages sent</span>
                      )}
                    </td>
                    <td className="px-3 py-2 tabular-nums" style={{ color: 'var(--ink-muted)' }}>
                      {formatDate(revoked ? row.revoked_at : row.consented_at)}
                    </td>
                    <td className="px-3 py-2 text-right">
                      {!revoked && (
                        <button
                          type="button"
                          onClick={() => void handleRevoke(row)}
                          disabled={busyId === row.id}
                          className="h-7 px-2.5 rounded-[var(--r-sm)] border text-[12px] font-medium transition-colors disabled:opacity-50"
                          style={{
                            borderColor: 'var(--line-strong)',
                            background: 'var(--canvas-elevated)',
                            color: 'var(--danger)',
                          }}
                        >
                          Revoke
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <p className="text-[12px] mb-5" style={{ color: 'var(--ink-subtle)' }}>
        Revoking keeps the row on record — the ledger proves both the consent and the
        revocation. A recipient texting STOP is revoked automatically.
      </p>

      {/* Add form */}
      <div
        className="text-[12px] font-medium uppercase tracking-wider mb-2"
        style={{ color: 'var(--ink-muted)' }}
      >
        Add a number
      </div>
      <div className="flex items-center gap-2 flex-wrap mb-2">
        <input
          className={`${inputCls} w-[160px]`}
          style={inputStyle}
          placeholder="(555) 123-4567"
          value={phone}
          onChange={(e) => setPhone(e.target.value)}
          aria-label="Phone number"
          autoComplete="off"
        />
        <input
          className={`${inputCls} flex-1 min-w-[140px]`}
          style={inputStyle}
          placeholder="Name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          aria-label="Name"
          autoComplete="off"
        />
        <select
          className="h-9 px-2.5 rounded-[var(--r-sm)] border text-[13px] outline-none cursor-pointer"
          style={inputStyle}
          value={kind}
          onChange={(e) => setKind(e.target.value as RecipientKind)}
          aria-label="Recipient kind"
        >
          {RECIPIENT_KINDS.map((k) => (
            <option key={k} value={k}>
              {KIND_LABELS[k]}
            </option>
          ))}
        </select>
        <select
          className="h-9 px-2.5 rounded-[var(--r-sm)] border text-[13px] outline-none cursor-pointer"
          style={inputStyle}
          value={timezone}
          onChange={(e) => setTimezone(e.target.value)}
          aria-label="Timezone"
        >
          {US_TIMEZONES.map((z) => (
            <option key={z.value} value={z.value}>
              {z.label}
            </option>
          ))}
        </select>
      </div>
      {/* Only shown for a STAFF number: a departure revokes manager-kind rows,
          because employment and tenancy are different relationships and a
          manager who also rents must not lose texts about their own home. */}
      {kind === 'manager' && (
        <label
          className="flex items-start gap-2.5 mb-2 cursor-pointer text-[13px] leading-relaxed"
          style={{ color: 'var(--ink-muted)' }}
        >
          <input
            type="checkbox"
            checked={ownNumber}
            onChange={(e) => setOwnNumber(e.target.checked)}
            className="mt-0.5"
            data-testid="sms-own-number"
            aria-label="This is my own phone"
          />
          <span>
            This is my own phone — stop these alerts automatically if I leave
            the company.
          </span>
        </label>
      )}
      <label
        className="flex items-start gap-2.5 mb-3 cursor-pointer text-[13px] leading-relaxed"
        style={{ color: 'var(--ink-muted)' }}
      >
        <input
          type="checkbox"
          checked={agreed}
          onChange={(e) => setAgreed(e.target.checked)}
          className="mt-0.5"
          aria-label="Consent confirmation"
        />
        <span>
          {CONSENT_LANGUAGE}{' '}
          {/* ToS + Privacy links adjacent to the consent checkbox — required
              by the A2P unified content policy (rejection code 30564).

              ⚠ These pointed at this app's own host with a bare `/terms`
              and `/privacy`, which show the LOGIN SCREEN — an SPA renders
              index.html for every path vercel.json does not exclude, and the
              exclusion list is `assets/ api/ browser/ legal/`, so `/terms` is
              not on it. (That host was the apex then and is
              app.occupella.com since the 2026-09-15 swap; a same-origin path
              is correct under both.) Founder-reported on the
              signed-out screen 2026-08-20 and fixed THERE; this copy kept the
              broken spelling, which is gotcha 25's shape on a link that a
              carrier reviewer is specifically told to click.
              Same-origin `/legal/*.html` is what SettingsPage already uses:
              real static files in this repo, served without auth, and correct
              on whatever domain the app is deployed to rather than on one
              hardcoded guess. */}
          <a
            href="/legal/terms.html"
            target="_blank"
            rel="noopener noreferrer"
            style={{ color: 'var(--iris)', textDecoration: 'underline' }}
          >
            Terms
          </a>{' '}
          ·{' '}
          <a
            href="/legal/privacy.html"
            target="_blank"
            rel="noopener noreferrer"
            style={{ color: 'var(--iris)', textDecoration: 'underline' }}
          >
            Privacy Policy
          </a>
        </span>
      </label>
      <button
        type="button"
        onClick={() => void handleAdd()}
        disabled={!canSubmit}
        className="h-9 px-4 rounded-[var(--r-sm)] text-[13px] font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        style={{ background: 'var(--iris)', color: 'var(--canvas)' }}
      >
        {saving ? 'Recording…' : 'Record consent'}
      </button>
    </DetailShell>
  );
}
