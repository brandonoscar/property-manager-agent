/**
 * DoorLoop — API connection panel (Settings → Connectors).
 *
 * The same flow as Propertyware's panel, for one value: save the API key, test
 * it at once (saving clears the last passed test), sync now, disconnect after
 * a confirm, and every refusal shown as the backend's own sentence (gotcha
 * 137).
 */

import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { Check, Eye, EyeOff, Lock, X } from 'lucide-react';

import { captureApiKeyAdded } from '../../services/analytics';
import {
  DOORLOOP_KEY_WHERE,
  MIN_API_KEY_LENGTH,
  type DoorLoopStatus,
  deleteDoorLoopCreds,
  getDoorLoopStatus,
  runDoorLoopSync,
  saveDoorLoopCreds,
  testDoorLoopConnection,
} from '../../services/doorloop';
import { count } from '../../lib/plural';
import { Button } from '../Button';
import { DetailShell } from './DetailShell';
import { RelativeTime } from './ui';

interface Props {
  onChange?: () => void;
  onClose: () => void;
}

/** Singular and plural for each table `POST /doorloop/sync` reports. A table
 *  missing here is shown by its own name. */
const TABLE_NOUNS: Record<string, [string, string]> = {
  properties: ['property', 'properties'],
  units: ['unit', 'units'],
  leases: ['lease', 'leases'],
  lease_tenants: ['tenant on a lease', 'tenants on leases'],
  tenants: ['tenant', 'tenants'],
  lease_balances: ['rent balance', 'rent balances'],
  lease_transactions: ['charge, payment or credit', 'charges, payments and credits'],
  owners: ['owner', 'owners'],
  vendors: ['vendor', 'vendors'],
  work_orders: ['work order', 'work orders'],
  tasks: ['task', 'tasks'],
  applicants: ['applicant', 'applicants'],
  bills: ['bill', 'bills'],
  bill_lines: ['bill line', 'bill lines'],
  gl_accounts: ['account', 'accounts'],
  turnovers: ['turnover opened', 'turnovers opened'],
};

/** "12 properties, 40 units, …" from the sync's rows-per-table map. */
export function syncSummary(rows: Record<string, number> | undefined): string {
  const parts = Object.entries(rows ?? {}).map(([table, n]) => {
    const nouns = TABLE_NOUNS[table];
    return nouns ? count(n, nouns[0], nouns[1]) : `${n.toLocaleString()} ${table.replace(/_/g, ' ')}`;
  });
  return parts.length ? `Synced ${parts.join(', ')}.` : 'Sync finished.';
}

export function DoorLoopApiDetail({ onChange, onClose }: Props) {
  const [status, setStatus] = useState<DoorLoopStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    getDoorLoopStatus()
      .then((s) => {
        if (mounted) setStatus(s);
      })
      .catch((err: unknown) => {
        if (mounted) {
          setStatusError(err instanceof Error ? err.message : 'Could not load the DoorLoop connection.');
        }
      });
    return () => {
      mounted = false;
    };
  }, []);

  const refreshStatus = async () => {
    try {
      setStatus(await getDoorLoopStatus());
    } catch (err) {
      console.warn('DoorLoop status refresh failed', err);
    }
    onChange?.();
  };

  return (
    <DetailShell
      title={
        <span className="inline-flex items-center gap-2">
          <Lock size={16} style={{ color: 'var(--ink-muted)' }} />
          DoorLoop · API
        </span>
      }
      subtitle="Connect your DoorLoop account so Occupella can read your properties, units, leases, tenants, applicants, owners, vendors, work orders, tasks, bills, chart of accounts, rent balances, and charges and payments."
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
  status: DoorLoopStatus | null;
  onChange: () => Promise<void>;
}) {
  const [apiKey, setApiKey] = useState('');
  const [showKey, setShowKey] = useState(false);
  const [busy, setBusy] = useState<'save' | 'test' | 'sync' | 'disconnect' | null>(null);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  const configured = !!status?.configured;
  // ⚠ Only an explicit `false` means the connector is off. A failed status
  // read is NOT that (gotcha 138): the save route answers 503 itself if it is.
  const connectorOff = status?.connector_enabled === false;
  const formValid = apiKey.trim().length >= MIN_API_KEY_LENGTH;

  const runTest = async (): Promise<boolean> => {
    const test = await testDoorLoopConnection();
    if (test.ok) {
      const found =
        test.properties_count === null ? '' : ` · ${count(test.properties_count, 'property', 'properties')}`;
      // A passed test starts the first sync (`POST /doorloop/test`).
      setResult({
        ok: true,
        text: `DoorLoop accepted the key${found}. Occupella has started reading your portfolio.`,
      });
      return true;
    }
    setResult({ ok: false, text: test.error ?? "DoorLoop didn't accept that key." });
    return false;
  };

  const onSave = async () => {
    if (!formValid) {
      toast.error('Paste the whole DoorLoop API key first.');
      return;
    }
    setBusy('save');
    setResult(null);
    try {
      await saveDoorLoopCreds({ api_key: apiKey.trim() });
      setApiKey('');
      setShowKey(false);
      captureApiKeyAdded('doorloop');
      // Saving clears `last_tested_at` server-side, so test right away.
      await runTest();
      await onChange();
    } catch (err) {
      setResult({
        ok: false,
        text: err instanceof Error ? err.message : "Couldn't reach Occupella to save the key.",
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
      const sync = await runDoorLoopSync();
      if (sync.ok) {
        setResult({ ok: true, text: syncSummary(sync.rows) });
      } else {
        setResult({ ok: false, text: sync.error ?? "The DoorLoop sync didn't finish." });
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
        'Disconnect DoorLoop? Your key will be removed and all synced DoorLoop data ' +
          '(properties, leases, tenants, work orders, bills, charges and payments) will be deleted ' +
          'from Occupella. This does not delete the API key inside DoorLoop; delete it there ' +
          '(Settings → Zapier & API Keys) if you no longer need it. Reconnecting later ' +
          're-syncs everything from DoorLoop.',
      )
    ) {
      return;
    }
    setBusy('disconnect');
    setResult(null);
    try {
      await deleteDoorLoopCreds();
      toast.success('DoorLoop disconnected.');
      setApiKey('');
      setShowKey(false);
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
          data-testid="doorloop-connection-summary"
        >
          <div>
            API key{' '}
            <span className="font-mono">
              {status.api_key_last4 ? `••••${status.api_key_last4}` : 'saved'}
            </span>
          </div>
          <div style={{ color: 'var(--ink-muted)' }}>
            {status.last_tested_at ? (
              <>
                Last passed a test <RelativeTime iso={status.last_tested_at} />.
              </>
            ) : (
              'This key has not passed a test since it was saved. Run Test connection.'
            )}
          </div>
        </div>
      )}

      {connectorOff && (
        <div
          className="rounded-[var(--r-md)] p-4 text-[13px] leading-relaxed"
          style={{ background: 'var(--negative-bg)', color: 'var(--negative)' }}
          role="status"
          data-testid="doorloop-connector-off"
        >
          The DoorLoop connector is switched off on this Occupella deployment, so a new key
          can&rsquo;t be saved or tested.
          {/* Only with a key stored is there a Disconnect button to point at (rule 16). */}
          {configured && ' You can still disconnect.'}
        </div>
      )}

      <div
        className="rounded-[var(--r-md)] p-4 text-[13px] leading-relaxed"
        style={{ border: '1px solid var(--line)', color: 'var(--ink)' }}
        data-testid="doorloop-where"
      >
        <p>
          <strong>Where do I find this?</strong>
        </p>
        <p className="mt-1">{DOORLOOP_KEY_WHERE}</p>
        <p className="mt-1">
          DoorLoop gives every key an access role. If the test says the key can&rsquo;t read
          properties, give it a role that can, or create a key that has one.
        </p>
      </div>

      <FormField
        label="API key"
        htmlFor="dl-api-key"
        helper="Occupella encrypts this before saving and never shows it again."
      >
        <div className="relative">
          <input
            type={showKey ? 'text' : 'password'}
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={
              configured && status?.api_key_last4
                ? `Saved (••••${status.api_key_last4})`
                : 'Paste from DoorLoop'
            }
            autoComplete="new-password"
            spellCheck={false}
            id="dl-api-key"
            className="h-10 w-full pl-3 pr-10 rounded-[var(--r-sm)] text-[13px] font-mono outline-none"
            style={{ background: 'var(--canvas-elevated)', border: '1px solid var(--line)', color: 'var(--ink)' }}
          />
          <button
            type="button"
            onClick={() => setShowKey((v) => !v)}
            className="absolute inset-y-0 right-0 px-3 flex items-center"
            aria-label={showKey ? 'Hide key' : 'Show key'}
            style={{ color: 'var(--ink-muted)' }}
          >
            {showKey ? <EyeOff size={14} /> : <Eye size={14} />}
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
            data-testid="doorloop-save"
          >
            {busy === 'save' ? 'Saving and testing…' : configured ? 'Replace key' : 'Save and test'}
          </Button>
          <Button
            variant="secondary"
            onClick={onTest}
            disabled={busy !== null || !configured || connectorOff}
            data-testid="doorloop-test"
          >
            {busy === 'test' ? 'Testing…' : 'Test connection'}
          </Button>
          <Button
            variant="secondary"
            onClick={onSync}
            disabled={busy !== null || !configured || connectorOff}
            title="Re-read DoorLoop into Occupella now instead of waiting for the next scheduled sync"
            data-testid="doorloop-sync"
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
            data-testid="doorloop-disconnect"
          >
            Disconnect DoorLoop
          </button>
        )}
      </div>
    </div>
  );
}

// ── Inline atoms (the same shapes as the Propertyware panel's) ────────

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
      data-testid="doorloop-result"
    >
      {ok ? <Check size={14} /> : <X size={14} />}
      <span className="flex-1">{text}</span>
      <span className="text-[12px] opacity-60">dismiss</span>
    </button>
  );
}
