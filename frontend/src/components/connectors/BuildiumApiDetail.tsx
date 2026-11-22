/**
 * Buildium — API connection panel.
 *
 * The "Occupella reaches out to Buildium" half. PMs paste their Buildium
 * Client ID + Client Secret and pick sandbox/production. Per Buildium's
 * developer docs, those two values are the entire auth surface — no
 * account_id, no token exchange. The masked saved-state, helper text,
 * and segmented env toggle are tuned for non-technical operators
 * (the demo audience), not developers reading a debug screen.
 */

import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { Check, Eye, EyeOff, ExternalLink, Loader2, Lock, X } from 'lucide-react';

import {
  type BuildiumEnvironment,
  type BuildiumStatus,
  deleteBuildiumCreds,
  getBuildiumStatus,
  runBuildiumBackfill,
  saveBuildiumCreds,
  testBuildiumConnection,
} from '../../services/buildium';
import { captureApiKeyAdded, captureWebhookConfigured } from '../../services/analytics';
import { DetailShell } from './DetailShell';
import { count } from '../../lib/plural';

interface Props {
  onChange?: () => void;
  onClose: () => void;
}

export function BuildiumApiDetail({ onChange, onClose }: Props) {
  const [status, setStatus] = useState<BuildiumStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    getBuildiumStatus()
      .then((s) => {
        if (mounted) setStatus(s);
      })
      .catch((err) => {
        if (mounted) setStatusError(err.message ?? 'Failed to load Buildium status');
      });
    return () => {
      mounted = false;
    };
  }, []);

  const refreshStatus = async () => {
    try {
      setStatus(await getBuildiumStatus());
      onChange?.();
    } catch (err) {
      console.warn('Buildium status refresh failed', err);
    }
  };

  return (
    <DetailShell
      title={
        <span className="inline-flex items-center gap-2">
          <Lock size={16} style={{ color: 'var(--ink-muted)' }} />
          Buildium · API
        </span>
      }
      subtitle="Connect your Buildium account so Occupella can read leases, work orders, and tenants on your behalf."
      onClose={onClose}
      errorBanner={statusError}
      wide
    >
      <ApiPanel status={status} onChange={refreshStatus} />
    </DetailShell>
  );
}

function ApiPanel({
  status,
  onChange,
}: {
  status: BuildiumStatus | null;
  onChange: () => Promise<void>;
}) {
  const [clientId, setClientId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
  const [showSecret, setShowSecret] = useState(false);
  const [environment, setEnvironment] = useState<BuildiumEnvironment>('sandbox');
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [backfilling, setBackfilling] = useState(false);
  const [savedFlash, setSavedFlash] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; text: string } | null>(
    null,
  );

  useEffect(() => {
    if (status?.configured) {
      setEnvironment(status.environment ?? 'sandbox');
    }
  }, [status?.configured, status?.environment]);

  const formValid = clientId.trim().length >= 4 && clientSecret.trim().length >= 8;
  const configured = !!status?.configured;
  const masked =
    configured && status?.client_id_last4 ? `••••${status.client_id_last4}` : '';

  const onSave = async () => {
    if (!formValid) {
      toast.error('Paste both Client ID and Client Secret first.');
      return;
    }
    setSaving(true);
    setTestResult(null);
    try {
      await saveBuildiumCreds({
        client_id: clientId.trim(),
        client_secret: clientSecret.trim(),
        environment,
      });
      setClientId('');
      setClientSecret('');
      setShowSecret(false);
      setSavedFlash(true);
      setTimeout(() => setSavedFlash(false), 3000);
      captureApiKeyAdded('buildium'); // activation funnel: "connect Buildium"
      await onChange();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to save.');
    } finally {
      setSaving(false);
    }
  };

  const onTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const result = await testBuildiumConnection();
      if (result.ok) {
        // The very first sentence a new customer reads after saving their
        // credentials. "1 properties" there is a bad first impression for
        // exactly the smallest customer.
        const summary =
          `Connected to ${result.account_name ?? 'Buildium'} · ` +
          count(result.properties_count ?? 0, 'property');
        setTestResult({ ok: true, text: summary });
        captureWebhookConfigured('buildium'); // funnel: key verified → webhook routing live
        await onChange();
      } else {
        setTestResult({ ok: false, text: result.error ?? 'Test failed.' });
      }
    } catch (err) {
      setTestResult({
        ok: false,
        text: err instanceof Error ? err.message : 'Network error.',
      });
    } finally {
      setTesting(false);
    }
  };

  const onBackfill = async () => {
    setBackfilling(true);
    setTestResult(null);
    try {
      const result = await runBuildiumBackfill();
      if (result.ok || result.properties > 0) {
        // ⚠ Every one of these can be 1 — a manager with a single building is
        // this product's smallest real customer, and this line is the first
        // thing they read after connecting Buildium. The multi-word ones pass
        // their plural explicitly because `plural` refuses to guess which word
        // of a phrase is the head noun.
        const parts = [
          count(result.properties, 'property'),
          count(result.units, 'unit'),
          count(result.tenants, 'tenant'),
          count(result.leases, 'lease'),
          count(result.work_orders ?? 0, 'work order', 'work orders'),
          count(result.applicants ?? 0, 'applicant'),
          count(result.vendors ?? 0, 'vendor'),
          count(result.owners ?? 0, 'owner'),
          count(result.tasks ?? 0, 'task'),
          count(result.bills ?? 0, 'bill'),
          `${result.gl_accounts ?? 0} GL accounts`,
          `${result.bank_accounts ?? 0} bank accounts`,
        ];
        const suffix = result.errors.length
          ? ` · ${result.errors.length} stage error${result.errors.length > 1 ? 's' : ''}`
          : '';
        setTestResult({ ok: true, text: `Synced ${parts.join(', ')}${suffix}` });
      } else {
        setTestResult({
          ok: false,
          text: result.error ?? result.errors.join('; ') ?? 'Sync failed.',
        });
      }
    } catch (err) {
      setTestResult({
        ok: false,
        text: err instanceof Error ? err.message : 'Network error.',
      });
    } finally {
      setBackfilling(false);
    }
  };

  const onDisconnect = async () => {
    if (
      !window.confirm(
        'Disconnect Buildium API? Your credentials will be removed and all synced ' +
          'Buildium data (properties, leases, bills, tasks) will be deleted from ' +
          "Occupella — Buildium's API terms require it. Reconnecting later re-syncs " +
          'everything from Buildium.',
      )
    ) {
      return;
    }
    try {
      await deleteBuildiumCreds();
      toast.success('Buildium API disconnected.');
      setClientId('');
      setClientSecret('');
      setShowSecret(false);
      setEnvironment('sandbox');
      setTestResult(null);
      await onChange();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to disconnect.');
    }
  };

  return (
    <div className="flex flex-col gap-5">
      <HelpBlock>
        <p>
          <strong>Where do I find these?</strong>
        </p>
        <p className="mt-1">
          In Buildium:{' '}
          <a
            href="https://developer.buildium.com"
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 underline underline-offset-2"
            style={{ color: 'var(--iris)' }}
          >
            Settings → Developer Tools → Create API Key
            <ExternalLink size={11} />
          </a>
          . Copy the Client ID and Secret immediately — Buildium only shows the
          Secret once.
        </p>
      </HelpBlock>

      <FormField
        label="Client ID"
        helper="Looks like xxxx-xxxx-xxxx-xxxx."
      >
        <input
          type="text"
          value={clientId}
          onChange={(e) => setClientId(e.target.value)}
          placeholder={configured ? `Saved (${masked})` : 'Paste from Buildium'}
          autoComplete="off"
          spellCheck={false}
          className="h-11 w-full px-3 py-2 rounded-md text-[13px] font-mono outline-none transition-colors"
          style={{
            background: 'var(--canvas-elevated)',
            border: '1px solid var(--line)',
            color: 'var(--ink)',
          }}
        />
      </FormField>

      <FormField
        label="Client Secret"
        helper="Long random string. Occupella encrypts this before saving."
      >
        <div className="relative">
          <input
            type={showSecret ? 'text' : 'password'}
            value={clientSecret}
            onChange={(e) => setClientSecret(e.target.value)}
            placeholder={configured ? 'Saved (hidden)' : 'Paste from Buildium'}
            autoComplete="off"
            spellCheck={false}
            className="h-11 w-full pl-3 pr-10 py-2 rounded-md text-[13px] font-mono outline-none transition-colors"
            style={{
              background: 'var(--canvas-elevated)',
              border: '1px solid var(--line)',
              color: 'var(--ink)',
            }}
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

      <FormField label="Environment">
        <div
          className="inline-flex rounded-md overflow-hidden"
          style={{ border: '1px solid var(--line)' }}
        >
          {(['sandbox', 'production'] as const).map((env) => (
            <button
              key={env}
              type="button"
              onClick={() => setEnvironment(env)}
              className="h-10 px-4 text-[13px] font-medium capitalize transition-colors"
              style={{
                background:
                  environment === env ? 'var(--iris)' : 'var(--canvas-elevated)',
                color: environment === env ? 'white' : 'var(--ink)',
              }}
            >
              {env}
            </button>
          ))}
        </div>
      </FormField>

      {testResult && (
        <FeedbackBox
          ok={testResult.ok}
          text={testResult.text}
          onDismiss={() => setTestResult(null)}
        />
      )}

      <div
        className="flex items-center justify-between gap-3 pt-4"
        style={{ borderTop: '1px solid var(--line)' }}
      >
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onSave}
            disabled={saving || !formValid}
            className="h-11 px-5 rounded-md text-[13px] font-medium disabled:opacity-50 inline-flex items-center gap-2"
            style={{ background: 'var(--iris)', color: 'var(--canvas)' }}
          >
            {saving && <Loader2 size={13} className="animate-spin" />}
            {saving ? 'Saving…' : 'Save credentials'}
          </button>
          <button
            type="button"
            onClick={onTest}
            disabled={testing || !configured}
            className="h-11 px-5 rounded-md text-[13px] font-medium disabled:opacity-50 inline-flex items-center gap-2"
            style={{
              background: 'var(--canvas-elevated)',
              color: 'var(--ink)',
              border: '1px solid var(--line)',
            }}
          >
            {testing && <Loader2 size={13} className="animate-spin" />}
            {testing ? 'Testing…' : 'Test connection'}
          </button>
          <button
            type="button"
            data-testid="buildium-backfill-button"
            onClick={onBackfill}
            disabled={backfilling || !configured}
            title="Pull properties, units, tenants, leases, and work orders into Occupella so the property pages have data"
            className="h-11 px-5 rounded-md text-[13px] font-medium disabled:opacity-50 inline-flex items-center gap-2"
            style={{
              background: 'var(--canvas-elevated)',
              color: 'var(--ink)',
              border: '1px solid var(--line)',
            }}
          >
            {backfilling && <Loader2 size={13} className="animate-spin" />}
            {backfilling ? 'Syncing…' : 'Sync data'}
          </button>
          {savedFlash && (
            <span
              className="inline-flex items-center gap-1 px-2 py-1 rounded text-[12px] font-medium"
              style={{ background: 'var(--positive-bg)', color: 'var(--positive)' }}
            >
              <Check size={11} />
              Saved
            </span>
          )}
        </div>
        {configured && (
          <button
            type="button"
            onClick={onDisconnect}
            className="text-[12px]"
            style={{ color: 'var(--negative)' }}
          >
            Disconnect Buildium
          </button>
        )}
      </div>
    </div>
  );
}

// ── Inline atoms ───────────────────────────────────────────────────

function HelpBlock({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="rounded-lg p-4 text-[13px] leading-relaxed"
      style={{
        background: 'var(--canvas-elevated)',
        color: 'var(--ink)',
        border: '1px solid var(--line)',
      }}
    >
      {children}
    </div>
  );
}

function FormField({
  label,
  helper,
  children,
}: {
  label: string;
  helper?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col">
      <label
        className="text-[13px] font-medium mb-1.5"
        style={{ color: 'var(--ink)' }}
      >
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

function FeedbackBox({
  ok,
  text,
  onDismiss,
}: {
  ok: boolean;
  text: string;
  onDismiss: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onDismiss}
      className="px-3 py-2.5 rounded-md text-[13px] flex items-center gap-2 text-left w-full"
      style={{
        background: ok ? 'var(--positive-bg)' : 'var(--negative-bg)',
        color: ok ? 'var(--positive)' : 'var(--negative)',
      }}
    >
      {ok ? <Check size={14} /> : <X size={14} />}
      <span className="flex-1">{text}</span>
      <span className="text-[12px] opacity-60">dismiss</span>
    </button>
  );
}
