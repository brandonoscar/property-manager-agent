/**
 * Propertyware — API connection panel (Settings → Connectors).
 *
 * The same flow as Rentvine's panel: save the keys, test them at once (saving
 * clears the last passed test), sync now, disconnect after a confirm, and
 * every refusal shown as the backend's own sentence (gotcha 137). Propertyware
 * takes three values, all required: the organization ID, which identifies the
 * account, and the API key's client ID and secret.
 */

import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { Check, Eye, EyeOff, Lock, X } from 'lucide-react';

import { captureApiKeyAdded } from '../../services/analytics';
import {
  MIN_CLIENT_ID_LENGTH,
  MIN_CLIENT_SECRET_LENGTH,
  PROPERTYWARE_READ_PERMISSIONS,
  type PropertywareStatus,
  deletePropertywareCreds,
  getPropertywareStatus,
  runPropertywareSync,
  savePropertywareCreds,
  testPropertywareConnection,
} from '../../services/propertyware';
import { count } from '../../lib/plural';
import { Button } from '../Button';
import { DetailShell } from './DetailShell';
import { RelativeTime } from './ui';

interface Props {
  onChange?: () => void;
  onClose: () => void;
}

/** Singular and plural for each table `POST /propertyware/sync` reports, in
 *  Propertyware's words. A table missing here is shown by its own name. */
const TABLE_NOUNS: Record<string, [string, string]> = {
  properties: ['building', 'buildings'],
  units: ['unit', 'units'],
  leases: ['lease', 'leases'],
  lease_tenants: ['tenant on a lease', 'tenants on leases'],
  tenants: ['tenant', 'tenants'],
  lease_balances: ['rent balance', 'rent balances'],
  owners: ['owner', 'owners'],
  vendors: ['vendor', 'vendors'],
  work_orders: ['work order', 'work orders'],
  bills: ['bill', 'bills'],
  bill_lines: ['bill line', 'bill lines'],
  gl_accounts: ['GL account', 'GL accounts'],
  turnovers: ['turnover opened', 'turnovers opened'],
};

/** "12 buildings, 40 units, …" from the sync's rows-per-table map. */
export function syncSummary(rows: Record<string, number> | undefined): string {
  const parts = Object.entries(rows ?? {}).map(([table, n]) => {
    const nouns = TABLE_NOUNS[table];
    return nouns ? count(n, nouns[0], nouns[1]) : `${n.toLocaleString()} ${table.replace(/_/g, ' ')}`;
  });
  return parts.length ? `Synced ${parts.join(', ')}.` : 'Sync finished.';
}

export function PropertywareApiDetail({ onChange, onClose }: Props) {
  const [status, setStatus] = useState<PropertywareStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    getPropertywareStatus()
      .then((s) => {
        if (mounted) setStatus(s);
      })
      .catch((err: unknown) => {
        if (mounted) {
          setStatusError(
            err instanceof Error ? err.message : 'Could not load the Propertyware connection.',
          );
        }
      });
    return () => {
      mounted = false;
    };
  }, []);

  const refreshStatus = async () => {
    try {
      setStatus(await getPropertywareStatus());
    } catch (err) {
      console.warn('Propertyware status refresh failed', err);
    }
    onChange?.();
  };

  return (
    <DetailShell
      title={
        <span className="inline-flex items-center gap-2">
          <Lock size={16} style={{ color: 'var(--ink-muted)' }} />
          Propertyware · API
        </span>
      }
      subtitle="Connect your Propertyware account so Occupella can read your buildings, units, leases, tenants, owners, vendors, work orders and bills."
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
  status: PropertywareStatus | null;
  onChange: () => Promise<void>;
}) {
  // The organization ID is not a secret, so a saved one is filled in: replacing
  // a key then needs only the new client ID and secret.
  const [systemId, setSystemId] = useState(status?.system_id ?? '');
  const [clientId, setClientId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
  const [showSecret, setShowSecret] = useState(false);
  const [busy, setBusy] = useState<'save' | 'test' | 'sync' | 'disconnect' | null>(null);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  const configured = !!status?.configured;
  // ⚠ Only an explicit `false` means the connector is off. A failed status
  // read is NOT that (gotcha 138): the save route answers 503 itself if it is.
  const connectorOff = status?.connector_enabled === false;
  const formValid =
    systemId.trim().length > 0 &&
    clientId.trim().length >= MIN_CLIENT_ID_LENGTH &&
    clientSecret.trim().length >= MIN_CLIENT_SECRET_LENGTH;

  const runTest = async (): Promise<boolean> => {
    const test = await testPropertywareConnection();
    if (test.ok) {
      const found =
        test.properties_count === null
          ? ''
          : ` · ${count(test.properties_count, 'building')}`;
      setResult({
        ok: true,
        text: `Propertyware accepted the keys${found}. Occupella has started reading your portfolio.`,
      });
      return true;
    }
    setResult({ ok: false, text: test.error ?? "Propertyware didn't accept those keys." });
    return false;
  };

  const onSave = async () => {
    if (!formValid) {
      toast.error('Enter the organization ID, and paste the client ID and secret first.');
      return;
    }
    setBusy('save');
    setResult(null);
    try {
      await savePropertywareCreds({
        system_id: systemId.trim(),
        client_id: clientId.trim(),
        client_secret: clientSecret.trim(),
      });
      setClientId('');
      setClientSecret('');
      setShowSecret(false);
      captureApiKeyAdded('propertyware');
      // Saving clears `last_tested_at` server-side, so test right away.
      await runTest();
      await onChange();
    } catch (err) {
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
      const sync = await runPropertywareSync();
      if (sync.ok) {
        setResult({ ok: true, text: syncSummary(sync.rows) });
      } else {
        setResult({ ok: false, text: sync.error ?? "The Propertyware sync didn't finish." });
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
        'Disconnect Propertyware? Your keys will be removed and all synced Propertyware data ' +
          '(buildings, leases, tenants, work orders, bills) will be deleted from Occupella. ' +
          'This does not delete the API key inside Propertyware; delete it there (Setup → ' +
          'Administration Setup → API Keys) if you no longer need it. Reconnecting later ' +
          're-syncs everything from Propertyware.',
      )
    ) {
      return;
    }
    setBusy('disconnect');
    setResult(null);
    try {
      await deletePropertywareCreds();
      toast.success('Propertyware disconnected.');
      setClientId('');
      setClientSecret('');
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
          data-testid="propertyware-connection-summary"
        >
          <div>
            Organization ID: <span className="font-mono">{status.system_id ?? 'unknown'}</span>
            {status.client_id_last4 && (
              <>
                {' '}
                · Client ID <span className="font-mono">••••{status.client_id_last4}</span>
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
          data-testid="propertyware-connector-off"
        >
          The Propertyware connector is switched off on this Occupella deployment, so new keys
          can&rsquo;t be saved or tested.
          {/* Only with keys stored is there a Disconnect button to point at (rule 16). */}
          {configured && ' You can still disconnect.'}
        </div>
      )}

      <div
        className="rounded-[var(--r-md)] p-4 text-[13px] leading-relaxed"
        style={{ border: '1px solid var(--line)', color: 'var(--ink)' }}
        data-testid="propertyware-where"
      >
        <p>
          <strong>Where do I find these?</strong>
        </p>
        <p className="mt-1">
          In Propertyware, open Setup, then Administration Setup → API Keys, and click Create API
          Key. Give the key these permissions: {PROPERTYWARE_READ_PERMISSIONS.join(', ')}.
          Propertyware shows the client ID and secret when the key is generated, and the secret
          only then: copy both straight away. If you no longer have the secret, create a new key.
        </p>
        <p className="mt-1">
          The organization ID identifies your Propertyware account. If you can&rsquo;t find it,
          ask Propertyware support for it.
        </p>
      </div>

      <FormField label="Organization ID" htmlFor="pw-system-id">
        <input
          type="text"
          value={systemId}
          onChange={(e) => setSystemId(e.target.value)}
          placeholder="Your Propertyware organization ID"
          autoComplete="off"
          spellCheck={false}
          id="pw-system-id"
          className="h-10 w-full px-3 rounded-[var(--r-sm)] text-[13px] font-mono outline-none"
          style={{ background: 'var(--canvas-elevated)', border: '1px solid var(--line)', color: 'var(--ink)' }}
        />
      </FormField>

      <FormField label="Client ID" htmlFor="pw-client-id">
        <input
          type="text"
          value={clientId}
          onChange={(e) => setClientId(e.target.value)}
          placeholder={
            configured && status?.client_id_last4
              ? `Saved (••••${status.client_id_last4})`
              : 'Paste from Propertyware'
          }
          autoComplete="off"
          spellCheck={false}
          id="pw-client-id"
          className="h-10 w-full px-3 rounded-[var(--r-sm)] text-[13px] font-mono outline-none"
          style={{ background: 'var(--canvas-elevated)', border: '1px solid var(--line)', color: 'var(--ink)' }}
        />
      </FormField>

      <FormField
        label="Client secret"
        htmlFor="pw-client-secret"
        helper="Occupella encrypts this before saving and never shows it again."
      >
        <div className="relative">
          <input
            type={showSecret ? 'text' : 'password'}
            value={clientSecret}
            onChange={(e) => setClientSecret(e.target.value)}
            placeholder={configured ? 'Saved (hidden)' : 'Paste from Propertyware'}
            autoComplete="new-password"
            spellCheck={false}
            id="pw-client-secret"
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
            data-testid="propertyware-save"
          >
            {busy === 'save' ? 'Saving and testing…' : configured ? 'Replace keys' : 'Save and test'}
          </Button>
          <Button
            variant="secondary"
            onClick={onTest}
            disabled={busy !== null || !configured || connectorOff}
            data-testid="propertyware-test"
          >
            {busy === 'test' ? 'Testing…' : 'Test connection'}
          </Button>
          <Button
            variant="secondary"
            onClick={onSync}
            disabled={busy !== null || !configured || connectorOff}
            title="Re-read Propertyware into Occupella now instead of waiting for the next scheduled sync"
            data-testid="propertyware-sync"
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
            data-testid="propertyware-disconnect"
          >
            Disconnect Propertyware
          </button>
        )}
      </div>
    </div>
  );
}

// ── Inline atoms (the same shapes as the Rentvine panel's) ────────────

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
      data-testid="propertyware-result"
    >
      {ok ? <Check size={14} /> : <X size={14} />}
      <span className="flex-1">{text}</span>
      <span className="text-[12px] opacity-60">dismiss</span>
    </button>
  );
}
