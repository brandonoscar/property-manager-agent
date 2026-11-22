/**
 * Buildium — Webhook receiver panel.
 *
 * The "Buildium reaches into Occupella" half. Surfaces the webhook URL,
 * a paste box for the signing secret Buildium generated, the events
 * Occupella listens for (grouped by category), and a tail of recent
 * ``buildium_events`` rows.
 *
 * Buildium generates the signing secret when the user creates a
 * webhook subscription on their side; the user copies it once from
 * Buildium's "Generate key" dialog and pastes it here. Occupella stores
 * it Fernet-encrypted and shows back only ``••••••••<last4>``. To
 * rotate, the user generates a new secret in Buildium and pastes that
 * one — the PUT endpoint replaces whatever was there.
 *
 * The recent-events fetch is best-effort — failures degrade silently
 * to an empty timeline so an unprovisioned ``buildium_events`` table
 * doesn't surface as a red banner above the empty state.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { Check, ExternalLink, RadioTower, RefreshCw } from 'lucide-react';

import {
  BUILDIUM_EVENT_GROUPS,
  type BuildiumEvent,
  type BuildiumStatus,
  buildiumWebhookUrl,
  deleteWebhookSecret,
  getBuildiumStatus,
  getRecentBuildiumEvents,
  setWebhookSecret,
} from '../../services/buildium';
import { useFocusTrap } from '../../hooks/useFocusTrap';
import { DetailShell } from './DetailShell';
import { CopyRow, RelativeTime, StatusBadge } from './ui';

interface Props {
  /** Closes the webhook panel and signals the parent to open the API
   *  panel for the same connector. Wired only for Buildium today. */
  onOpenApi?: () => void;
  onChange?: () => void;
  onClose: () => void;
}

export function BuildiumWebhookDetail({ onOpenApi, onChange, onClose }: Props) {
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
          <RadioTower size={16} style={{ color: 'var(--ink-muted)' }} />
          Buildium · Webhooks
        </span>
      }
      subtitle="Inbound events from Buildium into Occupella. We verify every event with HMAC-SHA256."
      onClose={onClose}
      errorBanner={statusError}
      wide
    >
      <WebhookPanel
        status={status}
        onChange={refreshStatus}
        onClose={onClose}
        onOpenApi={onOpenApi}
      />
    </DetailShell>
  );
}

function WebhookPanel({
  status,
  onChange,
  onClose,
  onOpenApi,
}: {
  status: BuildiumStatus | null;
  onChange: () => Promise<void>;
  onClose: () => void;
  onOpenApi?: () => void;
}) {
  const [events, setEvents] = useState<BuildiumEvent[] | null>(null);
  const [eventsError, setEventsError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const url = buildiumWebhookUrl();

  // Polling fetcher. The previous implementation ran the fetch exactly
  // once on the dep flip from undefined → true, which meant any webhook
  // that arrived after the initial fetch (which is the entire flow:
  // user opens panel, then triggers a test event in Buildium) was
  // invisible until a hard reload. Polling at 10s makes the panel
  // self-healing without a manual refresh.
  const loadEvents = useCallback(
    async (kind: 'initial' | 'poll' | 'manual') => {
      if (kind === 'manual') setRefreshing(true);
      try {
        const rows = await getRecentBuildiumEvents();
        setEvents(rows);
        setEventsError(null);
      } catch (err) {
        console.warn('recent-events fetch failed', err);
        if (kind === 'initial') {
          setEventsError(
            err instanceof Error
              ? err.message
              : 'Could not load recent events.',
          );
        }
        // For poll/manual we keep the last successful snapshot rather
        // than blanking the panel on a transient network blip.
      } finally {
        if (kind === 'manual') setRefreshing(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (!status?.configured) return;
    let cancelled = false;

    const tick = async (kind: 'initial' | 'poll') => {
      if (cancelled) return;
      await loadEvents(kind);
    };

    void tick('initial');
    const interval = window.setInterval(() => void tick('poll'), 10_000);

    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [status?.configured, loadEvents]);

  // Three branches: still loading → spinner; loaded but no API
  // creds → "save API creds first" prompt; loaded and configured →
  // the real panel. Conflating loading with unconfigured used to
  // surface the API-creds prompt during the initial load and on
  // post-save reopens (status null again briefly), which read as
  // "the webhook panel silently switched to the API connector".
  if (status === null) {
    return (
      <div className="flex justify-center py-10">
        <span className="spinner" />
      </div>
    );
  }

  if (!status.configured) {
    return (
      <div
        className="rounded-lg p-6 flex flex-col gap-3 items-start"
        style={{
          background: 'var(--canvas)',
          border: '1px dashed var(--card-edge)',
        }}
      >
        <div className="text-[14px] font-medium" style={{ color: 'var(--ink)' }}>
          Save your Buildium API credentials first
        </div>
        <p
          className="text-[13px] leading-relaxed"
          style={{ color: 'var(--ink-muted)' }}
        >
          The webhook signing secret is stored alongside your API credentials,
          so we need those before you can paste in the secret. Open the{' '}
          <strong>Buildium · API</strong> connector to add them.
        </p>
        <button
          type="button"
          onClick={() => {
            onClose();
            onOpenApi?.();
          }}
          className="h-10 px-4 rounded-md text-[13px] font-medium"
          style={{ background: 'var(--iris)', color: 'var(--canvas)' }}
        >
          Open API connector
        </button>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-5">
      <HelpBlock>
        <p>
          <strong>Where does this go?</strong>
        </p>
        <p className="mt-1">
          Copy the Webhook URL into Buildium:{' '}
          <a
            href="https://developer.buildium.com"
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 underline underline-offset-2"
            style={{ color: 'var(--iris)' }}
          >
            Settings → Developer Tools → Webhooks → Add Webhook
            <ExternalLink size={11} />
          </a>
          . Click <strong>Generate key</strong>, copy the secret, and paste it
          here — Buildium owns the secret, so Occupella can&rsquo;t generate one
          for you.
        </p>
      </HelpBlock>

      <FieldGroup label="Webhook URL">
        <CopyRow value={url} mono />
      </FieldGroup>

      <SecretField status={status} onChange={onChange} />

      <FieldGroup label="Events Occupella listens for">
        <div
          className="rounded-md p-3 flex flex-col gap-3"
          style={{
            background: 'var(--canvas-elevated)',
            border: '1px solid var(--line)',
          }}
        >
          {BUILDIUM_EVENT_GROUPS.map((group) => (
            <div key={group.label} className="flex flex-col gap-1">
              <div
                className="text-[12px] font-semibold uppercase tracking-wider"
                style={{ color: 'var(--ink-muted)' }}
              >
                {group.label}
              </div>
              <ul className="grid grid-cols-2 gap-x-3 gap-y-1">
                {group.events.map((evt) => (
                  <li
                    key={evt}
                    className="text-[13px] flex items-center gap-2"
                    style={{ color: 'var(--ink)' }}
                  >
                    <Check size={12} style={{ color: 'var(--positive)' }} />
                    <code className="font-mono">{evt}</code>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
        <p className="text-[12px] mt-1.5" style={{ color: 'var(--ink-muted)' }}>
          Subscribe to these events when you create the webhook in Buildium.
          Occupella ignores any extra events you subscribe to, so it&rsquo;s
          safe to enable everything.
        </p>
      </FieldGroup>

      <FieldGroup
        label={
          <span className="flex items-center justify-between gap-2">
            <span>Recent events</span>
            <button
              type="button"
              onClick={() => void loadEvents('manual')}
              disabled={refreshing}
              aria-label="Refresh recent events"
              className="inline-flex items-center gap-1 text-[12px]"
              style={{
                color: 'var(--iris)',
                opacity: refreshing ? 0.5 : 1,
              }}
            >
              <RefreshCw
                size={11}
                className={refreshing ? 'animate-spin' : ''}
              />
              Refresh
            </button>
          </span>
        }
      >
        {events === null && eventsError === null ? (
          <div
            className="rounded-md px-3 py-3 flex items-center justify-center"
            style={{
              background: 'var(--canvas-elevated)',
              border: '1px solid var(--line)',
            }}
          >
            <span className="spinner" />
          </div>
        ) : eventsError !== null && events === null ? (
          <div
            className="rounded-md px-3 py-3 text-[12px] flex items-center justify-between gap-3"
            style={{
              color: 'var(--negative)',
              background: 'var(--negative-bg)',
              border: '1px solid var(--negative)',
            }}
          >
            <span>{eventsError}</span>
            <button
              type="button"
              onClick={() => void loadEvents('manual')}
              className="text-[12px] font-medium underline"
              style={{ color: 'var(--negative)' }}
            >
              Retry
            </button>
          </div>
        ) : (events ?? []).length === 0 ? (
          <p
            className="rounded-md px-3 py-3 text-[12px]"
            style={{
              color: 'var(--ink-muted)',
              background: 'var(--canvas-elevated)',
              border: '1px solid var(--line)',
            }}
          >
            No recent events. Send a test webhook from Buildium to verify the
            connection — new events appear here within 10 seconds.
          </p>
        ) : (
          <ul
            className="rounded-md p-3 flex flex-col gap-2"
            style={{
              background: 'var(--canvas-elevated)',
              border: '1px solid var(--line)',
            }}
          >
            {(events ?? []).map((evt) => (
              <li
                key={evt.id}
                className="flex items-center justify-between text-[12px]"
              >
                <code className="font-mono" style={{ color: 'var(--ink)' }}>
                  {evt.event_name}
                </code>
                <span style={{ color: 'var(--ink-muted)' }}>
                  <RelativeTime iso={evt.event_time} />
                </span>
                <StatusBadge status={evt.status} />
              </li>
            ))}
          </ul>
        )}
      </FieldGroup>
    </div>
  );
}

function SecretField({
  status,
  onChange,
}: {
  status: BuildiumStatus;
  onChange: () => Promise<void>;
}) {
  const [editing, setEditing] = useState(!status.webhook_secret_last4);
  const [value, setValue] = useState('');
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  // Pure optimistic UI. The instant the user clicks Save we flip the
  // panel to the masked saved-state — the PUT runs in the background.
  // If it fails we revert and surface the error. This guarantees the
  // user never stares at "Saving…" regardless of network latency
  // (Render cold start, dropped pool conn, etc.). The previous
  // strategy — flip on PUT *resolution* — left the button visibly
  // pinned for the entire round-trip, which read as "stuck" even
  // when the request was simply slow.
  const [optimisticLast4, setOptimisticLast4] = useState<string | null>(null);

  const lastFour = optimisticLast4 ?? status.webhook_secret_last4;

  const onSave = () => {
    const trimmed = value.trim();
    if (trimmed.length < 8) {
      toast.error('Secret looks too short. Paste the value Buildium showed you.');
      return;
    }

    // Snapshot for revert-on-error.
    const previousOptimistic = optimisticLast4;
    const previousValue = value;
    const previousEditing = editing;
    const newLast4 = trimmed.slice(-4);

    // Flip UI immediately — no await, no spinner.
    setOptimisticLast4(newLast4);
    setValue('');
    setEditing(false);

    console.info('[buildium] PUT /webhook-secret start (last4=%s)', newLast4);
    setWebhookSecret(trimmed)
      .then(() => {
        console.info('[buildium] PUT /webhook-secret 2xx');
        toast.success('Secret saved.');
        void onChange();
      })
      .catch((err: unknown) => {
        console.error('[buildium] PUT /webhook-secret failed', err);
        // Revert the optimistic update so the user can retry.
        setOptimisticLast4(previousOptimistic);
        setValue(previousValue);
        setEditing(previousEditing);
        toast.error(err instanceof Error ? err.message : 'Failed to save secret.');
      });
  };

  const onDelete = () => {
    const previousOptimistic = optimisticLast4;
    const previousEditing = editing;

    // Optimistic: clear last4 and switch to paste form.
    setOptimisticLast4(null);
    setConfirmingDelete(false);
    setEditing(true);

    console.info('[buildium] DELETE /webhook-secret start');
    deleteWebhookSecret()
      .then(() => {
        console.info('[buildium] DELETE /webhook-secret 2xx');
        toast.success('Secret forgotten. Paste a new one before Buildium retries.');
        void onChange();
      })
      .catch((err: unknown) => {
        console.error('[buildium] DELETE /webhook-secret failed', err);
        setOptimisticLast4(previousOptimistic);
        setEditing(previousEditing);
        toast.error(err instanceof Error ? err.message : 'Failed to delete secret.');
      });
  };

  return (
    <FieldGroup label="Signing secret">
      <p
        className="text-[12px] mb-1.5"
        style={{ color: 'var(--ink-muted)' }}
      >
        Buildium generates this when you create the webhook in their
        dashboard. Click <strong>Generate key</strong>, copy the value, and
        paste it here.
      </p>

      {!editing && lastFour ? (
        <div
          className="flex items-center justify-between gap-3 px-3 py-2.5 rounded-md"
          style={{
            background: 'var(--canvas-elevated)',
            border: '1px solid var(--line)',
          }}
        >
          <code
            className="text-[13px] font-mono"
            style={{ color: 'var(--ink)' }}
          >
            ••••••••{lastFour}
          </code>
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => setEditing(true)}
              className="text-[12px]"
              style={{ color: 'var(--iris)' }}
            >
              Replace
            </button>
            <button
              type="button"
              onClick={() => setConfirmingDelete(true)}
              className="text-[12px]"
              style={{ color: 'var(--ink-muted)' }}
            >
              Forget
            </button>
          </div>
        </div>
      ) : (
        <div className="flex items-center gap-2">
          <input
            type="password"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="e.g. Zm9yLXRlc3RzLW9ubHktbm90LWEtcmVhbC1rZXkhISE="
            className="flex-1 h-10 px-3 rounded-md text-[13px] font-mono"
            style={{
              background: 'var(--canvas-elevated)',
              color: 'var(--ink)',
              border: '1px solid var(--line)',
            }}
            autoFocus
          />
          <button
            type="button"
            onClick={onSave}
            disabled={value.length < 8}
            className="h-10 px-4 rounded-md text-[13px] font-medium disabled:opacity-50"
            style={{ background: 'var(--iris)', color: 'var(--canvas)' }}
          >
            Save
          </button>
          {lastFour && (
            <button
              type="button"
              onClick={() => {
                setEditing(false);
                setValue('');
              }}
              className="text-[12px]"
              style={{ color: 'var(--ink-muted)' }}
            >
              Cancel
            </button>
          )}
        </div>
      )}

      {confirmingDelete && (
        <ConfirmForgetDialog
          onConfirm={onDelete}
          onCancel={() => setConfirmingDelete(false)}
        />
      )}
    </FieldGroup>
  );
}

function ConfirmForgetDialog({
  onConfirm,
  onCancel,
}: {
  onConfirm: () => void;
  onCancel: () => void;
}) {
  // ⚠ This is a DESTRUCTIVE confirm — saying yes breaks every inbound webhook
  // until somebody pastes a new secret — and it shipped with no keyboard
  // dismissal and no focus trap. Tab walked out of it into the connector page
  // behind, leaving the red "Forget secret" button behind in a dialog the user
  // could no longer see. Escape cancels: on a destructive gate the keyboard's
  // most reflexive key must mean "no", never nothing.
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') onCancel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel]);

  const panel = useRef<HTMLDivElement>(null);
  useFocusTrap(true, panel);

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center"
      style={{ background: 'rgba(0, 0, 0, 0.6)' }}
      onClick={onCancel}
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label="Forget the signing secret?"
        className="rounded-[var(--r-md)] p-6 flex flex-col gap-3"
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 'min(440px, 92vw)',
          background: 'var(--canvas)',
          border: '1px solid var(--line)',
        }}
      >
        <h3
          className="text-[16px] font-semibold"
          style={{ color: 'var(--ink)' }}
        >
          Forget the signing secret?
        </h3>
        <p className="text-[13px]" style={{ color: 'var(--ink-muted)' }}>
          Webhooks will fail with a 401 until you paste a new secret. Are you
          sure?
        </p>
        <div className="flex justify-end gap-2 pt-2">
          <button
            type="button"
            onClick={onCancel}
            className="h-10 px-4 rounded-md text-[13px]"
            style={{ color: 'var(--ink)' }}
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="h-10 px-4 rounded-md text-[13px] font-medium"
            style={{ background: 'var(--negative)', color: 'var(--canvas)' }}
          >
            Forget secret
          </button>
        </div>
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

function FieldGroup({
  label,
  children,
}: {
  label: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col">
      <div
        className="text-[13px] font-medium mb-1.5"
        style={{ color: 'var(--ink)' }}
      >
        {label}
      </div>
      {children}
    </div>
  );
}
