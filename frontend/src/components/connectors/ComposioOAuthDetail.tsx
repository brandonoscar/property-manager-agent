/**
 * Connect / disconnect panel for Composio-managed OAuth connectors
 * (Google Calendar, Google Drive, Gmail, Slack).
 *
 * The flow:
 *   1. User clicks "Connect with {Provider}".
 *   2. Frontend POSTs to ``/api/v1/connectors/{id}/connect`` — backend
 *      asks Composio to initiate an OAuth connection and returns a
 *      ``redirectUrl``.
 *   3. We open ``redirectUrl`` in a new tab. The user grants consent
 *      on the provider's page; Composio hosts the OAuth redirect.
 *   4. User returns to Occupella; we poll ``listConnectors`` for ~30s
 *      and flip to "Connected" once Composio reports the connection
 *      as active.
 *
 * Disconnect just calls the existing ``disconnect`` endpoint which
 * deletes the Composio connection.
 *
 * Demo mode shortcut: ``redirectUrl`` will be empty, so we skip the
 * popup and just flip the local state via reloadConnectors().
 */

import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { ExternalLink, X, AlertTriangle } from 'lucide-react';

import {
  connectorRowState,
  initiateOAuthConnection,
  setConnectorConnection,
  type Connector,
} from '../../services/connectors';
import { captureConnectorConnected } from '../../services/analytics';
import { DetailShell } from './DetailShell';

interface Props {
  connector: Connector;
  onClose: () => void;
  onChange: () => void | Promise<void>;
}

export function ComposioOAuthDetail({ connector, onClose, onChange }: Props) {
  const [phase, setPhase] = useState<'idle' | 'opening' | 'waiting'>('idle');
  const [error, setError] = useState<string | null>(null);
  // One home, shared with the Settings list row — `needsReauth` already
  // carries the precedence (`stateUnknown` outranks it), so nothing here
  // restates it. See connectorRowState in services/connectors.ts.
  const { isConnected, needsReauth, stateUnknown } = connectorRowState(connector);

  // Activation funnel: fire "connector connected" once, on the
  // not-connected → connected transition (the poll below flips
  // connector.apiConnected via onChange). Seeded with the mount-time
  // value so an already-connected panel doesn't re-fire on open.
  const wasConnected = useRef(isConnected);
  useEffect(() => {
    if (connector.apiConnected && !wasConnected.current) {
      captureConnectorConnected(connector.id);
    }
    wasConnected.current = !!connector.apiConnected;
  }, [connector.apiConnected, connector.id]);

  const handleConnect = async () => {
    setError(null);
    setPhase('opening');
    try {
      const init = await initiateOAuthConnection(connector.id);
      if (init.redirectUrl) {
        // Open in a new tab so the user doesn't lose the Occupella
        // session. Composio's hosted page handles the provider
        // redirect; on return the next reload will reflect the
        // new connection state.
        const popup = window.open(init.redirectUrl, '_blank', 'noopener,noreferrer');
        if (!popup) {
          // Popup blocked — surface a clickable link so the user
          // can complete consent manually.
          setError(
            'Popup was blocked. Click the link below to grant access manually.',
          );
        }
      }
      setPhase('waiting');
      // Poll for ~30s, then stop. Composio's connection state
      // typically lands within 5-10s of the user finishing consent.
      const start = Date.now();
      const poll = async (): Promise<void> => {
        if (Date.now() - start > 30_000) return;
        await onChange();
        await new Promise((r) => setTimeout(r, 2500));
        await poll();
      };
      void poll().finally(() => setPhase('idle'));
    } catch (err) {
      const message =
        err instanceof Error ? err.message : 'Failed to start OAuth flow';
      setError(message);
      setPhase('idle');
      toast.error(`Connect failed: ${message}`);
    }
  };

  const handleDisconnect = async () => {
    setError(null);
    setPhase('opening');
    try {
      await setConnectorConnection(connector.id, false);
      await onChange();
      toast.success(`Disconnected ${connector.name}.`);
      setPhase('idle');
    } catch (err) {
      const message =
        err instanceof Error ? err.message : 'Failed to disconnect';
      setError(message);
      setPhase('idle');
      toast.error(`Disconnect failed: ${message}`);
    }
  };

  return (
    <DetailShell
      title={`${connector.name} · API`}
      subtitle={
        isConnected
          ? `Connected via Composio · ${connector.accountInfo ?? 'OAuth grant active'}`
          : stateUnknown
            ? `${connector.name} is unavailable on this account right now.`
            : needsReauth
              ? `Connection expired — reconnect to restore ${connector.name}.`
              : `Connect ${connector.name} via Composio's hosted OAuth.`
      }
      onClose={onClose}
    >
      <div
        className="rounded-[var(--r-md)] p-5 flex flex-col gap-4"
        style={{
          background: 'var(--canvas-elevated)',
          border: '1px solid var(--line)',
        }}
      >
        {/* What you're granting */}
        <div className="flex flex-col gap-1.5">
          <div
            className="text-[12px] uppercase tracking-wider"
            style={{ color: 'var(--ink-muted)', letterSpacing: '0.08em' }}
          >
            What Occupella can do
          </div>
          <p className="text-[13px]" style={{ color: 'var(--ink)' }}>
            {scopeCopy(connector.id)}
          </p>
        </div>

        {/* Lapsed-grant notice — the connection existed but its OAuth grant
            expired/dropped (the #183 orphan class or a 7-day testing-mode
            revoke). Tell the user why it went quiet before the action. */}
        {/* We could not read the state. Say whose problem it is and give NO
            instruction — "connect" and "reconnect" both fail here, and an
            instruction that cannot work is worse than none. */}
        {stateUnknown && (
          <div
            className="flex items-start gap-2 rounded-[var(--r-sm)] px-3 py-2.5 text-[13px]"
            style={{
              background: 'var(--canvas-raised)',
              border: '1px solid var(--line)',
              color: 'var(--ink)',
            }}
          >
            <AlertTriangle
              size={14}
              style={{ color: 'var(--ink-muted)', marginTop: 1, flexShrink: 0 }}
            />
            <span>
              We can&apos;t check this connection right now — that&apos;s a
              setup problem on our side, not something wrong with your account.
              It needs an administrator; nothing for you to change here.
            </span>
          </div>
        )}

        {needsReauth && (
          <div
            className="flex items-start gap-2 rounded-[var(--r-sm)] px-3 py-2.5 text-[13px]"
            style={{
              background: 'var(--caution-bg)',
              border: '1px solid var(--caution)',
              color: 'var(--ink)',
            }}
          >
            <AlertTriangle
              size={14}
              style={{ color: 'var(--caution)', marginTop: 1, flexShrink: 0 }}
            />
            <span>
              This connection expired and stopped syncing. Reconnect to
              restore access — nothing else needs to change.
            </span>
          </div>
        )}

        {/* Action */}
        {!isConnected ? (
          <button
            type="button"
            disabled={phase !== 'idle' || stateUnknown}
            onClick={() => void handleConnect()}
            data-testid={`composio-connect-${connector.id}`}
            className="inline-flex items-center justify-center gap-2 h-10 px-4 rounded-[var(--r-sm)] text-[14px] font-medium transition-opacity"
            style={{
              background: 'var(--iris)',
              color: 'var(--canvas)',
              opacity: phase !== 'idle' || stateUnknown ? 0.7 : 1,
              cursor: stateUnknown
                ? 'not-allowed'
                : phase !== 'idle'
                  ? 'wait'
                  : 'pointer',
            }}
          >
            <ExternalLink size={14} />
            {stateUnknown
              ? 'Unavailable right now'
              : phase === 'opening'
                ? 'Opening…'
                : phase === 'waiting'
                  ? 'Waiting for consent…'
                  : needsReauth
                    ? `Reconnect with ${providerShortName(connector.id)}`
                    : `Connect with ${providerShortName(connector.id)}`}
          </button>
        ) : (
          <button
            type="button"
            disabled={phase !== 'idle'}
            onClick={() => void handleDisconnect()}
            data-testid={`composio-disconnect-${connector.id}`}
            className="inline-flex items-center justify-center gap-2 h-10 px-4 rounded-[var(--r-sm)] text-[14px] font-medium transition-opacity"
            style={{
              background: 'var(--canvas-raised)',
              color: 'var(--ink)',
              border: '1px solid var(--line)',
              opacity: phase !== 'idle' ? 0.7 : 1,
              cursor: phase !== 'idle' ? 'wait' : 'pointer',
            }}
          >
            <X size={14} />
            {phase === 'opening' ? 'Disconnecting…' : 'Disconnect'}
          </button>
        )}

        {error && (
          <div
            className="rounded-[var(--r-sm)] p-3 text-[13px]"
            style={{
              background: 'var(--negative-bg)',
              border: '1px solid var(--negative)',
              color: 'var(--negative)',
            }}
            role="alert"
          >
            {error}
          </div>
        )}

        <p
          className="text-[12px] leading-relaxed"
          style={{ color: 'var(--ink-subtle)' }}
        >
          Occupella uses Composio to handle the OAuth grant. Your provider
          credentials never touch Occupella directly — Composio stores the
          refresh token and exposes scoped tools to the agent.
        </p>
      </div>
    </DetailShell>
  );
}


function providerShortName(connectorId: string): string {
  switch (connectorId) {
    case 'google_calendar':
    case 'google_drive':
      return 'Google';
    case 'gmail':
      return 'Gmail';
    case 'slack':
      return 'Slack';
    default:
      return connectorId;
  }
}


function scopeCopy(connectorId: string): string {
  switch (connectorId) {
    case 'google_calendar':
      return 'Read and create calendar events — schedule inspections, walkthroughs, and vendor visits.';
    case 'google_drive':
      return 'Read your SOPs, lease docs, and property notes so the agent can quote them in chat and inbox actions.';
    case 'gmail':
      return 'Read tenant emails and draft / send replies on your behalf.';
    case 'slack':
      return 'Post notifications and reply to thread messages.';
    default:
      return `Authorize Occupella to act on your ${connectorId} account.`;
  }
}
