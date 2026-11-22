/**
 * Rentvine — API connection panel (Settings → Connectors).
 *
 * The routes behind it (`/api/v1/rentvine/*`) shipped with the connector; until
 * this panel, only the onboarding wizard called them. So a workspace whose key
 * Rentvine revoked had nowhere in the app to paste a new one, while chat told
 * people to do exactly that "on the Connectors page".
 *
 * Same flow as the wizard, on purpose: an access key and a secret, the account
 * code only when the backend cannot work it out (it answers 422 asking for it),
 * and every refusal shown as the backend's own sentence (gotcha 137).
 */

import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { Check, Eye, EyeOff, Lock, X } from 'lucide-react';

import { ApiError } from '../../services/api';
import { captureApiKeyAdded } from '../../services/analytics';
import {
  type RentvineStatus,
  deleteRentvineCreds,
  getRentvineStatus,
  runRentvineSync,
  saveRentvineCreds,
  testRentvineConnection,
} from '../../services/rentvine';
import { count } from '../../lib/plural';
import { Button } from '../Button';
import { DetailShell } from './DetailShell';
import { RentvineWebhookSection } from './RentvineWebhookSection';
import { RelativeTime } from './ui';

interface Props {
  onChange?: () => void;
  onClose: () => void;
}

/** Singular and plural for each stage name `POST /rentvine/sync` reports
 *  (`sync.SYNC_STATE_STAGE`). A stage missing here is shown by its own name. */
const STAGE_NOUNS: Record<string, [string, string]> = {
  properties: ['property', 'properties'],
  units: ['unit', 'units'],
  leases: ['lease record', 'lease records'],
  tenants: ['tenant', 'tenants'],
  owners: ['owner', 'owners'],
  vendors: ['vendor', 'vendors'],
  work_orders: ['work order', 'work orders'],
  bills: ['bill record', 'bill records'],
  balances: ['balance', 'balances'],
  gl_accounts: ['GL account', 'GL accounts'],
  applicants: ['applicant', 'applicants'],
  transactions: ['transaction', 'transactions'],
};

/** "13 properties, 157 units, …" from the sync's rows-per-stage map. */
export function syncSummary(rows: Record<string, number> | undefined): string {
  const parts = Object.entries(rows ?? {}).map(([stage, n]) => {
    const nouns = STAGE_NOUNS[stage];
    return nouns ? count(n, nouns[0], nouns[1]) : `${n.toLocaleString()} ${stage.replace(/_/g, ' ')}`;
  });
  return parts.length ? `Synced ${parts.join(', ')}.` : 'Sync finished.';
}

export function RentvineApiDetail({ onChange, onClose }: Props) {
  const [status, setStatus] = useState<RentvineStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    getRentvineStatus()
      .then((s) => {
        if (mounted) setStatus(s);
      })
      .catch((err: unknown) => {
        if (mounted) {
          setStatusError(err instanceof Error ? err.message : 'Could not load the Rentvine connection.');
        }
      });
    return () => {
      mounted = false;
    };
  }, []);

  const refreshStatus = async () => {
    try {
      setStatus(await getRentvineStatus());
    } catch (err) {
      console.warn('Rentvine status refresh failed', err);
    }
    onChange?.();
  };

  return (
    <DetailShell
      title={
        <span className="inline-flex items-center gap-2">
          <Lock size={16} style={{ color: 'var(--ink-muted)' }} />
          Rentvine · API
        </span>
      }
      subtitle="Connect your Rentvine account so Occupella can read your properties, leases, tenants, owners, vendors and work orders."
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
  status: RentvineStatus | null;
  onChange: () => Promise<void>;
}) {
  const [apiKey, setApiKey] = useState('');
  const [apiSecret, setApiSecret] = useState('');
  const [accountCode, setAccountCode] = useState('');
  const [showAccountCode, setShowAccountCode] = useState(false);
  const [showSecret, setShowSecret] = useState(false);
  const [busy, setBusy] = useState<'save' | 'test' | 'sync' | 'disconnect' | null>(null);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  const configured = !!status?.configured;
  // ⚠ Only an explicit `false` means the connector is off. A failed status
  // read is NOT that (gotcha 138), and neither is an older backend that does
  // not send the field: the save route answers 503 itself if it is off.
  const connectorOff = status?.connector_enabled === false;
  const formValid = apiKey.trim().length >= 4 && apiSecret.trim().length >= 8;

  const runTest = async (): Promise<boolean> => {
    const test = await testRentvineConnection();
    if (test.ok) {
      const found =
        test.properties_count === null ? '' : ` · ${count(test.properties_count, 'property')}`;
      setResult({
        ok: true,
        text: `Connected to Rentvine${found}. Occupella has started reading your portfolio.`,
      });
      return true;
    }
    setResult({ ok: false, text: test.error ?? "Rentvine didn't accept those keys." });
    return false;
  };

  const onSave = async () => {
    if (!formValid) {
      toast.error('Paste both the access key and the secret first.');
      return;
    }
    setBusy('save');
    setResult(null);
    try {
      await saveRentvineCreds({
        api_key: apiKey.trim(),
        api_secret: apiSecret.trim(),
        account_code: accountCode.trim() || undefined,
      });
      setApiKey('');
      setApiSecret('');
      setShowSecret(false);
      captureApiKeyAdded('rentvine');
      // Saving clears `last_tested_at` server-side, so an untested key reads
      // as "Reconnect" until it passes: test right away, as the wizard does.
      await runTest();
      await onChange();
    } catch (err) {
      // 422 is the backend asking for the account code it could not work out.
      if (err instanceof ApiError && err.status === 422) setShowAccountCode(true);
      setResult({
        ok: false,
        text: err instanceof Error ? err.message : "Couldn't reach Occupella to save the keys.",
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
      const sync = await runRentvineSync();
      if (sync.ok) {
        setResult({ ok: true, text: syncSummary(sync.rows) });
      } else {
        setResult({ ok: false, text: sync.error ?? "The Rentvine sync didn't finish." });
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
        'Disconnect Rentvine? Your keys will be removed and all synced Rentvine data ' +
          '(properties, leases, tenants, work orders) will be deleted from Occupella. ' +
          'This does not delete the key inside Rentvine; remove it there if you no longer ' +
          'need it. Reconnecting later re-syncs everything from Rentvine.',
      )
    ) {
      return;
    }
    setBusy('disconnect');
    setResult(null);
    try {
      await deleteRentvineCreds();
      toast.success('Rentvine disconnected.');
      setApiKey('');
      setApiSecret('');
      setAccountCode('');
      setShowSecret(false);
      await onChange();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to disconnect.');
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex flex-col gap-5">
      {configured && status && (
        <div
          className="rounded-[var(--r-md)] p-4 text-[13px] leading-relaxed"
          style={{ border: '1px solid var(--line)', color: 'var(--ink)' }}
          data-testid="rentvine-connection-summary"
        >
          <div>
            Account: <span className="font-mono">{status.account_code ?? 'unknown'}.rentvine.com</span>
            {status.api_key_last4 && (
              <>
                {' '}
                · Key <span className="font-mono">••••{status.api_key_last4}</span>
              </>
            )}
          </div>
          <div style={{ color: 'var(--ink-muted)' }}>
            {status.last_tested_at ? (
              <>
                Last passed a test <RelativeTime iso={status.last_tested_at} />.
              </>
            ) : (
              'These keys have not passed a test since they were saved. Run Test connection.'
            )}
          </div>
        </div>
      )}

      {connectorOff && (
        <div
          className="rounded-[var(--r-md)] p-4 text-[13px] leading-relaxed"
          style={{ background: 'var(--negative-bg)', color: 'var(--negative)' }}
          role="status"
          data-testid="rentvine-connector-off"
        >
          The Rentvine connector is switched off on this Occupella deployment, so new keys
          can&rsquo;t be saved or tested.
          {/* Only with keys stored is there a Disconnect button to point at (rule 16). */}
          {configured && ' You can still disconnect.'}
        </div>
      )}

      <div
        className="rounded-[var(--r-md)] p-4 text-[13px] leading-relaxed"
        style={{ border: '1px solid var(--line)', color: 'var(--ink)' }}
      >
        <p>
          <strong>Where do I find these?</strong>
        </p>
        <p className="mt-1">
          In Rentvine, create an API key under Settings → API. Copy the access key and the secret
          straight away: Rentvine shows the secret only once. If you no longer have it, create a new
          key and paste that pair.
        </p>
      </div>

      <FormField label="Access key" htmlFor="rv-access-key">
        <input
          type="text"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder={
            configured && status?.api_key_last4 ? `Saved (••••${status.api_key_last4})` : 'Paste from Rentvine'
          }
          autoComplete="off"
          spellCheck={false}
          id="rv-access-key"
          className="h-10 w-full px-3 rounded-[var(--r-sm)] text-[13px] font-mono outline-none"
          style={{ background: 'var(--canvas-elevated)', border: '1px solid var(--line)', color: 'var(--ink)' }}
        />
      </FormField>

      <FormField
        label="Secret"
        htmlFor="rv-secret"
        helper="Occupella encrypts this before saving and never shows it again."
      >
        <div className="relative">
          <input
            type={showSecret ? 'text' : 'password'}
            value={apiSecret}
            onChange={(e) => setApiSecret(e.target.value)}
            placeholder={configured ? 'Saved (hidden)' : 'Paste from Rentvine'}
            autoComplete="new-password"
            spellCheck={false}
            id="rv-secret"
            className="h-10 w-full pl-3 pr-10 rounded-[var(--r-sm)] text-[13px] font-mono outline-none"
            style={{ background: 'var(--canvas-elevated)', border: '1px solid var(--line)', color: 'var(--ink)' }}
          />
          <button
            type="button"
            onClick={() => setShowSecret((v) => !v)}
            className="absolute inset-y-0 right-0 px-3 flex items-center"
            aria-label={showSecret ? 'Hide secret' : 'Show secret'}
            style={{ color: 'var(--ink-muted)' }}
          >
            {showSecret ? <EyeOff size={14} /> : <Eye size={14} />}
          </button>
        </div>
      </FormField>

      {showAccountCode ? (
        <FormField
          label="Rentvine account code"
          htmlFor="rv-account-code"
          helper="The word before .rentvine.com in your browser's address bar when you're signed in to Rentvine."
        >
          <input
            type="text"
            value={accountCode}
            onChange={(e) => setAccountCode(e.target.value)}
            placeholder={status?.account_code ?? 'yourcompany'}
            autoComplete="off"
            spellCheck={false}
            id="rv-account-code"
            className="h-10 w-full px-3 rounded-[var(--r-sm)] text-[13px] font-mono outline-none"
            style={{ background: 'var(--canvas-elevated)', border: '1px solid var(--line)', color: 'var(--ink)' }}
          />
        </FormField>
      ) : (
        <div>
          <Button variant="ghost" size="sm" onClick={() => setShowAccountCode(true)}>
            I know my Rentvine account code
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
            data-testid="rentvine-save"
          >
            {busy === 'save' ? 'Saving and testing…' : configured ? 'Replace keys' : 'Save and test'}
          </Button>
          <Button
            variant="secondary"
            onClick={onTest}
            disabled={busy !== null || !configured || connectorOff}
            data-testid="rentvine-test"
          >
            {busy === 'test' ? 'Testing…' : 'Test connection'}
          </Button>
          <Button
            variant="secondary"
            onClick={onSync}
            disabled={busy !== null || !configured || connectorOff}
            title="Re-read Rentvine into Occupella now instead of waiting for the next scheduled sync"
            data-testid="rentvine-sync"
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
            data-testid="rentvine-disconnect"
          >
            Disconnect Rentvine
          </button>
        )}
      </div>

      {/* The signing key is stored on the connection's row, so it needs one. */}
      {configured && status && (
        <RentvineWebhookSection status={status} onChange={onChange} connectorOff={connectorOff} />
      )}
    </div>
  );
}

// ── Inline atoms (same shapes as the Buildium panel's) ────────────────

function FormField({
  label,
  htmlFor,
  helper,
  children,
}: {
  label: string;
  htmlFor: string;
  helper?: string;
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
      data-testid="rentvine-result"
    >
      {ok ? <Check size={14} /> : <X size={14} />}
      <span className="flex-1">{text}</span>
      <span className="text-[12px] opacity-60">dismiss</span>
    </button>
  );
}
