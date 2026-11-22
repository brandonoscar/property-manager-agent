/**
 * Rent Manager — API connection panel (Settings → Connectors).
 *
 * The routes behind it are `/api/v1/rentmanager/*` (`api/routes/rentmanager.py`);
 * the onboarding wizard connects through the same ones. Same flow as the other
 * property-management panels: save, then test straight away, every refusal
 * shown as the backend's own sentence (gotcha 137).
 *
 * What it asks for: the corp ID (the subdomain of the API address), the ONE
 * location this workspace reads (design R2), the Rent Manager user Occupella
 * signs in as, and a partner token only if Rent Manager issued one. The corp ID
 * and location are not secrets and are filled in from what is saved; the
 * username, password and token never come back from the server.
 */

import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { Check, Eye, EyeOff, Lock, X } from 'lucide-react';

import { captureApiKeyAdded } from '../../services/analytics';
import {
  type RentManagerStatus,
  deleteRentManagerCreds,
  getRentManagerStatus,
  runRentManagerSync,
  saveRentManagerCreds,
  testRentManagerConnection,
} from '../../services/rentmanager';
import { count } from '../../lib/plural';
import { Button } from '../Button';
import { DetailShell } from './DetailShell';
import { RelativeTime } from './ui';

interface Props {
  onChange?: () => void;
  onClose: () => void;
}

/** Singular and plural for each stage name `GET /rentmanager/sync` reports
 *  (the route's `_COUNTED`). A stage missing here is shown by its own name. */
const STAGE_NOUNS: Record<string, [string, string]> = {
  properties: ['property', 'properties'],
  units: ['unit', 'units'],
  tenants: ['tenant', 'tenants'],
  leases: ['lease', 'leases'],
  owners: ['owner', 'owners'],
  vendors: ['vendor', 'vendors'],
  work_orders: ['work order', 'work orders'],
  bills: ['bill', 'bills'],
  gl_accounts: ['GL account', 'GL accounts'],
  balances: ['open balance', 'open balances'],
};

/** "2 properties, 3 units, …" from the sync's rows-per-stage map. */
export function syncSummary(rows: Record<string, number> | undefined): string {
  const parts = Object.entries(rows ?? {}).map(([stage, n]) => {
    const nouns = STAGE_NOUNS[stage];
    return nouns ? count(n, nouns[0], nouns[1]) : `${n.toLocaleString()} ${stage.replace(/_/g, ' ')}`;
  });
  return parts.length ? `Synced ${parts.join(', ')}.` : 'Sync finished.';
}

/** The largest location id the backend stores (a Postgres integer). */
const MAX_LOCATION_ID = 2_147_483_647;

/** What the corp ID may hold before the backend's own check (`host_label`). */
const CORP_ID_RE = /^[A-Za-z0-9-]+$/;

/**
 * The backend's own sentences for a field it would refuse
 * (`api/routes/rentmanager.py` `_FIELD_ADVICE`), shown under the field while
 * what is typed cannot be saved, so a disabled Save says why (rule 16).
 * `tests/unit/test_rentmanager_routes.py` reads both sides.
 */
export const FIELD_ADVICE = {
  corp_id:
    'Enter your Rent Manager corp ID: the part before .api.rentmanager.com in your Rent Manager API address, in letters, digits and hyphens only.',
  location_id: 'Enter the Rent Manager location ID as a whole number above 0, such as 1.',
} as const;

/** The location as the number the backend takes, or `null` if it is not one. */
export function parseLocationId(text: string): number | null {
  const trimmed = text.trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const value = Number(trimmed);
  return value > 0 && value <= MAX_LOCATION_ID ? value : null;
}

export function RentManagerApiDetail({ onChange, onClose }: Props) {
  const [status, setStatus] = useState<RentManagerStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    getRentManagerStatus()
      .then((s) => {
        if (mounted) setStatus(s);
      })
      .catch((err: unknown) => {
        if (mounted) {
          setStatusError(
            err instanceof Error ? err.message : 'Could not load the Rent Manager connection.',
          );
        }
      });
    return () => {
      mounted = false;
    };
  }, []);

  const refreshStatus = async () => {
    try {
      setStatus(await getRentManagerStatus());
    } catch (err) {
      console.warn('Rent Manager status refresh failed', err);
    }
    onChange?.();
  };

  return (
    <DetailShell
      title={
        <span className="inline-flex items-center gap-2">
          <Lock size={16} style={{ color: 'var(--ink-muted)' }} />
          Rent Manager · API
        </span>
      }
      subtitle="Connect your Rent Manager account so Occupella can read your properties, units, leases, tenants, owners, vendors, work orders, bills and balances."
      onClose={onClose}
      errorBanner={statusError}
      wide
    >
      {status === null && statusError === null ? (
        <div className="flex justify-center py-8">
          <span className="spinner" />
        </div>
      ) : (
        <ApiPanel status={status} onChange={refreshStatus} />
      )}
    </DetailShell>
  );
}

function ApiPanel({
  status,
  onChange,
}: {
  status: RentManagerStatus | null;
  onChange: () => Promise<void>;
}) {
  const [corpId, setCorpId] = useState(status?.corp_id ?? '');
  const [locationId, setLocationId] = useState(
    status?.location_id != null ? String(status.location_id) : '',
  );
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [partnerToken, setPartnerToken] = useState('');
  const [showPartner, setShowPartner] = useState(!!status?.partner_token_saved);
  const [showPassword, setShowPassword] = useState(false);
  const [busy, setBusy] = useState<'save' | 'test' | 'sync' | 'disconnect' | null>(null);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  const configured = !!status?.configured;
  // ⚠ Only an explicit `false` means the connector is off. A failed status
  // read is NOT that (gotcha 138), and neither is a backend that does not send
  // the field: the save route answers 503 itself if it is off.
  const connectorOff = status?.connector_enabled === false;
  const location = parseLocationId(locationId);
  // Said only once something is typed: an empty field is unfinished, not wrong.
  const corpBad = corpId !== '' && !CORP_ID_RE.test(corpId.trim());
  const locationBad = locationId !== '' && location === null;
  const formValid =
    CORP_ID_RE.test(corpId.trim()) &&
    location !== null &&
    username.trim().length > 0 &&
    password.trim().length > 0;

  const runTest = async (): Promise<boolean> => {
    const test = await testRentManagerConnection();
    if (test.ok) {
      const found =
        test.properties_count === null ? '' : ` · ${count(test.properties_count, 'property')}`;
      setResult({
        ok: true,
        text: `Connected to Rent Manager${found}. Occupella has started reading your portfolio.`,
      });
      return true;
    }
    setResult({ ok: false, text: test.error ?? "Rent Manager didn't accept those details." });
    return false;
  };

  const onSave = async () => {
    if (!formValid || location === null) {
      toast.error('Fill in the corp ID, a location ID such as 1, the username and the password.');
      return;
    }
    setBusy('save');
    setResult(null);
    try {
      await saveRentManagerCreds({
        corp_id: corpId.trim(),
        location_id: location,
        username: username.trim(),
        password,
        partner_token: partnerToken.trim() || undefined,
      });
      setUsername('');
      setPassword('');
      setPartnerToken('');
      setShowPassword(false);
      captureApiKeyAdded('rentmanager');
      // Saving clears `last_tested_at` server-side, so untested details read as
      // "Reconnect" until they pass: test right away, as the wizard does.
      await runTest();
      await onChange();
    } catch (err) {
      setResult({
        ok: false,
        text: err instanceof Error ? err.message : "Couldn't reach Occupella to save the details.",
      });
    } finally {
      setBusy(null);
    }
  };

  const onTest = async () => {
    setBusy('test');
    setResult(null);
    try {
      await runTest();
      await onChange();
    } catch (err) {
      setResult({
        ok: false,
        text: err instanceof Error ? err.message : "Couldn't reach Occupella to run the test.",
      });
    } finally {
      setBusy(null);
    }
  };

  const onSync = async () => {
    setBusy('sync');
    setResult(null);
    try {
      const sync = await runRentManagerSync();
      if (sync.ok) {
        setResult({ ok: true, text: syncSummary(sync.rows) });
      } else {
        setResult({ ok: false, text: sync.error ?? "The Rent Manager sync didn't finish." });
      }
      await onChange();
    } catch (err) {
      setResult({
        ok: false,
        text: err instanceof Error ? err.message : "Couldn't reach Occupella to start the sync.",
      });
    } finally {
      setBusy(null);
    }
  };

  const onDisconnect = async () => {
    if (
      !window.confirm(
        'Disconnect Rent Manager? The saved details will be removed and all synced Rent ' +
          'Manager data (properties, leases, tenants, work orders) will be deleted from ' +
          'Occupella. Nothing changes inside Rent Manager: the user Occupella signed in as ' +
          'keeps working there, so disable it or change its password in Rent Manager if you ' +
          'no longer need it. Reconnecting later re-syncs everything from Rent Manager.',
      )
    ) {
      return;
    }
    setBusy('disconnect');
    setResult(null);
    try {
      await deleteRentManagerCreds();
      toast.success('Rent Manager disconnected.');
      setUsername('');
      setPassword('');
      setPartnerToken('');
      setShowPassword(false);
      await onChange();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to disconnect.');
    } finally {
      setBusy(null);
    }
  };

  const inputStyle = {
    background: 'var(--canvas-elevated)',
    border: '1px solid var(--line)',
    color: 'var(--ink)',
  };
  const inputClass = 'h-10 w-full px-3 rounded-[var(--r-sm)] text-[13px] font-mono outline-none disabled:opacity-60';

  return (
    <div className="flex flex-col gap-5">
      {/* First, before any field: nobody types a password into a form that
          this deployment cannot save. */}
      {connectorOff && (
        <div
          className="rounded-[var(--r-md)] p-4 text-[13px] leading-relaxed"
          style={{ background: 'var(--negative-bg)', color: 'var(--negative)' }}
          role="status"
          data-testid="rentmanager-connector-off"
        >
          The Rent Manager connector is switched off on this Occupella deployment, so details
          can&rsquo;t be saved, tested or synced.
          {/* Only with details stored is there a Disconnect button to point at (rule 16). */}
          {configured && ' You can still disconnect.'}
        </div>
      )}

      {configured && status && (
        <div
          className="rounded-[var(--r-md)] p-4 text-[13px] leading-relaxed"
          style={{ border: '1px solid var(--line)', color: 'var(--ink)' }}
          data-testid="rentmanager-connection-summary"
        >
          <div>
            Account:{' '}
            <span className="font-mono">{status.corp_id ?? 'unknown'}.api.rentmanager.com</span>
            {status.location_id != null && <> · Location {status.location_id}</>}
          </div>
          <div>
            {status.username_last4 ? (
              <>
                User <span className="font-mono">••••{status.username_last4}</span>
              </>
            ) : (
              'User saved'
            )}
            {status.partner_token_saved && ' · Partner token saved'}
          </div>
          <div style={{ color: 'var(--ink-muted)' }}>
            {status.last_tested_at ? (
              <>
                Last passed a test <RelativeTime iso={status.last_tested_at} />.
              </>
            ) : (
              'These details have not passed a test since they were saved. Run Test connection.'
            )}
          </div>
        </div>
      )}

      <div
        className="rounded-[var(--r-md)] p-4 text-[13px] leading-relaxed"
        style={{ border: '1px solid var(--line)', color: 'var(--ink)' }}
      >
        <p>
          <strong>What to enter</strong>
        </p>
        <p className="mt-1">
          Create a Rent Manager user just for Occupella, with a role that can read your properties,
          units, tenants, leases, owners, vendors, work orders and bills, and enter its username and
          password here. Rent Manager&rsquo;s API has to be turned on for your company; Rent
          Manager support can confirm it is.
        </p>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <FormField
          label="Corp ID"
          htmlFor="rm-corp-id"
          helper="The part before .api.rentmanager.com in your Rent Manager API address."
          error={corpBad ? FIELD_ADVICE.corp_id : undefined}
          errorTestId="rentmanager-corp-error"
        >
          <input
            type="text"
            value={corpId}
            onChange={(e) => setCorpId(e.target.value)}
            placeholder="yourcompany"
            autoComplete="off"
            spellCheck={false}
            disabled={connectorOff}
            id="rm-corp-id"
            aria-invalid={corpBad || undefined}
            aria-describedby={corpBad ? 'rm-corp-id-error' : undefined}
            className={inputClass}
            style={inputStyle}
          />
        </FormField>

        <FormField
          label="Location ID"
          htmlFor="rm-location-id"
          helper="The number of the one Rent Manager location this workspace reads."
          error={locationBad ? FIELD_ADVICE.location_id : undefined}
          errorTestId="rentmanager-location-error"
        >
          <input
            type="text"
            inputMode="numeric"
            value={locationId}
            onChange={(e) => setLocationId(e.target.value)}
            placeholder="1"
            autoComplete="off"
            spellCheck={false}
            disabled={connectorOff}
            id="rm-location-id"
            aria-invalid={locationBad || undefined}
            aria-describedby={locationBad ? 'rm-location-id-error' : undefined}
            className={inputClass}
            style={inputStyle}
          />
        </FormField>
      </div>

      <FormField label="Username" htmlFor="rm-username">
        <input
          type="text"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder={
            configured && status?.username_last4
              ? `Saved (••••${status.username_last4})`
              : configured
                ? 'Saved (hidden)'
                : 'The Rent Manager user Occupella signs in as'
          }
          autoComplete="off"
          spellCheck={false}
          disabled={connectorOff}
          id="rm-username"
          className={inputClass}
          style={inputStyle}
        />
      </FormField>

      <FormField
        label="Password"
        htmlFor="rm-password"
        helper="Occupella encrypts this before saving and never shows it again."
      >
        <div className="relative">
          <input
            type={showPassword ? 'text' : 'password'}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={configured ? 'Saved (hidden)' : "That user's password"}
            autoComplete="new-password"
            spellCheck={false}
            disabled={connectorOff}
            id="rm-password"
            className={`${inputClass} pr-10`}
            style={inputStyle}
          />
          <button
            type="button"
            onClick={() => setShowPassword((v) => !v)}
            className="absolute inset-y-0 right-0 px-3 flex items-center"
            aria-label={showPassword ? 'Hide password' : 'Show password'}
            style={{ color: 'var(--ink-muted)' }}
          >
            {showPassword ? <EyeOff size={14} /> : <Eye size={14} />}
          </button>
        </div>
      </FormField>

      {showPartner ? (
        <FormField
          label="Partner token"
          htmlFor="rm-partner-token"
          helper={
            status?.partner_token_saved
              ? 'A partner token is saved. Saving new details replaces it: paste it again to keep it, or leave this empty to remove it.'
              : 'Only if Rent Manager issued a partner token for this connection. Leave it empty otherwise.'
          }
        >
          <input
            type="password"
            value={partnerToken}
            onChange={(e) => setPartnerToken(e.target.value)}
            placeholder={status?.partner_token_saved ? 'Saved (hidden)' : 'Optional'}
            autoComplete="new-password"
            spellCheck={false}
            disabled={connectorOff}
            id="rm-partner-token"
            className={inputClass}
            style={inputStyle}
          />
        </FormField>
      ) : (
        <div>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setShowPartner(true)}
            disabled={connectorOff}
            data-testid="rentmanager-partner-toggle"
          >
            Rent Manager gave me a partner token
          </Button>
        </div>
      )}

      {result && <FeedbackBox ok={result.ok} text={result.text} onDismiss={() => setResult(null)} />}

      <div
        className="flex flex-wrap items-center justify-between gap-3 pt-4"
        style={{ borderTop: '1px solid var(--line)' }}
      >
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="primary"
            onClick={onSave}
            disabled={busy !== null || !formValid || connectorOff}
            data-testid="rentmanager-save"
          >
            {busy === 'save' ? 'Saving and testing…' : configured ? 'Replace details' : 'Save and test'}
          </Button>
          <Button
            variant="secondary"
            onClick={onTest}
            disabled={busy !== null || !configured || connectorOff}
            data-testid="rentmanager-test"
          >
            {busy === 'test' ? 'Testing…' : 'Test connection'}
          </Button>
          <Button
            variant="secondary"
            onClick={onSync}
            disabled={busy !== null || !configured || connectorOff}
            title="Re-read Rent Manager into Occupella now instead of waiting for the next scheduled sync"
            data-testid="rentmanager-sync"
          >
            {busy === 'sync' ? 'Syncing, this can take a few minutes…' : 'Sync now'}
          </Button>
        </div>
        {configured && (
          <button
            type="button"
            onClick={onDisconnect}
            disabled={busy !== null}
            className="text-[12px] disabled:opacity-50"
            style={{ color: 'var(--negative)' }}
            data-testid="rentmanager-disconnect"
          >
            Disconnect Rent Manager
          </button>
        )}
      </div>
    </div>
  );
}

// ── Inline atoms (the same shapes as the other connector panels') ─────

function FormField({
  label,
  htmlFor,
  helper,
  error,
  errorTestId,
  children,
}: {
  label: string;
  htmlFor: string;
  helper?: string;
  /** Why what is typed here cannot be saved, in the backend's words. */
  error?: string;
  errorTestId?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col">
      <label htmlFor={htmlFor} className="text-[13px] font-medium mb-1.5" style={{ color: 'var(--ink)' }}>
        {label}
      </label>
      {children}
      {helper && (
        <p className="text-[12px] mt-1.5" style={{ color: 'var(--ink-muted)' }}>
          {helper}
        </p>
      )}
      {error && (
        <p
          id={`${htmlFor}-error`}
          className="text-[12px] mt-1"
          style={{ color: 'var(--negative)' }}
          data-testid={errorTestId}
        >
          {error}
        </p>
      )}
    </div>
  );
}

function FeedbackBox({ ok, text, onDismiss }: { ok: boolean; text: string; onDismiss: () => void }) {
  return (
    <button
      type="button"
      onClick={onDismiss}
      role={ok ? 'status' : 'alert'}
      className="px-3 py-2.5 rounded-[var(--r-sm)] text-[13px] flex items-center gap-2 text-left w-full"
      style={{
        background: ok ? 'var(--positive-bg)' : 'var(--negative-bg)',
        color: ok ? 'var(--positive)' : 'var(--negative)',
      }}
      data-testid="rentmanager-result"
    >
      {ok ? <Check size={14} /> : <X size={14} />}
      <span className="flex-1">{text}</span>
      <span className="text-[12px] opacity-60">dismiss</span>
    </button>
  );
}
