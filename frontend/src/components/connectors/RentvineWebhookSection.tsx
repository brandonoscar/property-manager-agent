/**
 * Rentvine webhooks on Settings → Connectors: the URL to paste into Rentvine,
 * the events to add, and the account's signing key.
 *
 * Optional by design. The regular sync keeps Rentvine data current; webhooks
 * only bring four record types in sooner (`docs/rentvine/webhook-setup.md`).
 * Before this section nothing could save the signing key, so the receiver
 * would have refused every delivery (gotcha 200).
 *
 * ⚠ The setup steps show only when the deployment can check Rentvine's
 * signature (`webhook_scheme_ready === true`). Until then every delivery is
 * refused, so steps that end in webhooks that can never be accepted are not an
 * action worth naming (rule 16), and an unknown answer must not read as ready
 * (rule 14).
 */

import { useState } from 'react';
import { Eye, EyeOff } from 'lucide-react';

import {
  type RentvineStatus,
  RENTVINE_WEBHOOK_EVENTS,
  deleteRentvineWebhookSecret,
  rentvineWebhookUrl,
  saveRentvineWebhookSecret,
} from '../../services/rentvine';
import { Button } from '../Button';
import { CopyRow } from './ui';

interface Props {
  status: RentvineStatus;
  /** Re-reads the connection status after a change. */
  onChange: () => Promise<void>;
  /** The connector is switched off: a key can be removed, not saved. */
  connectorOff: boolean;
}

export function RentvineWebhookSection({ status, onChange, connectorOff }: Props) {
  const [key, setKey] = useState('');
  const [showKey, setShowKey] = useState(false);
  const [busy, setBusy] = useState<'save' | 'remove' | null>(null);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  const ready = status.webhook_scheme_ready === true;
  const saved = status.webhook_secret_saved === true;
  const trimmed = key.trim();

  const onSave = async () => {
    setBusy('save');
    setResult(null);
    try {
      await saveRentvineWebhookSecret(trimmed);
      setKey('');
      setShowKey(false);
      setResult({
        ok: true,
        text: 'Signing key saved. Occupella now accepts Rentvine webhooks signed with it.',
      });
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

  const onRemove = async () => {
    if (
      !window.confirm(
        'Remove the signing key? Occupella will turn away every Rentvine webhook until you ' +
          'save one again. Your data still updates on the regular sync.',
      )
    ) {
      return;
    }
    setBusy('remove');
    setResult(null);
    try {
      await deleteRentvineWebhookSecret();
      setResult({ ok: true, text: 'Signing key removed.' });
      await onChange();
    } catch (err) {
      setResult({
        ok: false,
        text: err instanceof Error ? err.message : "Couldn't reach Occupella to remove the key.",
      });
    } finally {
      setBusy(null);
    }
  };

  return (
    <section
      className="rounded-[var(--r-md)] p-4 flex flex-col gap-3 text-[13px] leading-relaxed"
      style={{ border: '1px solid var(--line)', color: 'var(--ink)' }}
      data-testid="rentvine-webhooks"
    >
      <p>
        <strong>Webhooks (optional)</strong>
      </p>

      {ready ? (
        <>
          <p>
            Webhooks bring changes to properties, units, leases and work orders into Occupella
            within seconds, instead of at the next sync.
          </p>
          <ol className="list-decimal pl-5 flex flex-col gap-1">
            <li>
              In Rentvine, go to Actions → New Webhook and add one webhook for each event below,
              all with this URL.
            </li>
            <li>Copy the signing key from Actions → Webhooks and save it here.</li>
          </ol>
          {status.account_code && <CopyRow value={rentvineWebhookUrl(status.account_code)} mono />}
          <p style={{ color: 'var(--ink-muted)' }} data-testid="rentvine-webhook-events">
            Events: {RENTVINE_WEBHOOK_EVENTS.join(', ')}.
          </p>

          <div className="flex flex-col">
            <label htmlFor="rv-signing-key" className="text-[13px] font-medium mb-1.5">
              Signing key
            </label>
            <div className="flex items-center gap-2">
              <div className="relative flex-1">
                <input
                  type={showKey ? 'text' : 'password'}
                  value={key}
                  onChange={(e) => setKey(e.target.value)}
                  placeholder={saved ? 'Saved (hidden). Paste a new key to replace it.' : 'Paste from Rentvine'}
                  autoComplete="new-password"
                  spellCheck={false}
                  id="rv-signing-key"
                  className="h-10 w-full pl-3 pr-10 rounded-[var(--r-sm)] text-[13px] font-mono outline-none"
                  style={{
                    background: 'var(--canvas-elevated)',
                    border: '1px solid var(--line)',
                    color: 'var(--ink)',
                  }}
                />
                <button
                  type="button"
                  onClick={() => setShowKey((v) => !v)}
                  className="absolute inset-y-0 right-0 px-3 flex items-center"
                  aria-label={showKey ? 'Hide signing key' : 'Show signing key'}
                  style={{ color: 'var(--ink-muted)' }}
                >
                  {showKey ? <EyeOff size={14} /> : <Eye size={14} />}
                </button>
              </div>
              <Button
                variant="secondary"
                onClick={onSave}
                disabled={busy !== null || !trimmed || connectorOff}
                data-testid="rentvine-webhook-key-save"
              >
                {busy === 'save' ? 'Saving…' : saved ? 'Replace key' : 'Save key'}
              </Button>
            </div>
            <p className="text-[12px] mt-1.5" style={{ color: 'var(--ink-muted)' }}>
              Occupella encrypts this before saving and never shows it again.
            </p>
          </div>
        </>
      ) : (
        <p style={{ color: 'var(--ink-muted)' }} data-testid="rentvine-webhooks-not-ready">
          Occupella can&rsquo;t check Rentvine&rsquo;s webhook signatures yet, so it turns away
          every delivery. You don&rsquo;t need webhooks: the regular sync keeps your Rentvine data
          current.
        </p>
      )}

      {saved ? (
        <div className="flex items-center justify-between gap-3" data-testid="rentvine-webhook-key-status">
          <span>A signing key is saved.</span>
          <button
            type="button"
            onClick={onRemove}
            disabled={busy !== null}
            className="text-[12px] disabled:opacity-50"
            style={{ color: 'var(--negative)' }}
            data-testid="rentvine-webhook-key-remove"
          >
            {busy === 'remove' ? 'Removing…' : 'Remove key'}
          </button>
        </div>
      ) : (
        ready && (
          <p style={{ color: 'var(--ink-muted)' }} data-testid="rentvine-webhook-key-status">
            No signing key saved yet. Until you save one, Occupella turns away every Rentvine
            webhook.
          </p>
        )
      )}

      {result && (
        <p
          role={result.ok ? 'status' : 'alert'}
          style={{ color: result.ok ? 'var(--positive)' : 'var(--negative)' }}
          data-testid="rentvine-webhook-result"
        >
          {result.text}
        </p>
      )}
    </section>
  );
}
