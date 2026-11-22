/**
 * Placeholder panel for connectors whose real flow isn't built yet.
 * Honest copy is the goal — the layout looks "done" but the body
 * makes clear that only Buildium is wired up today.
 */

import type { ConnectorCapability } from '../../services/connectors';
import { DetailShell } from './DetailShell';

interface Props {
  connectorName: string;
  capability: ConnectorCapability;
  onClose: () => void;
}

export function ComingSoonDetail({ connectorName, capability, onClose }: Props) {
  const subtitle =
    capability === 'api'
      ? 'Outbound API integration is on the roadmap.'
      : 'Inbound webhook integration is on the roadmap.';

  const message =
    capability === 'api'
      ? `Occupella API connect for ${connectorName} is coming soon — Buildium is live today.`
      : `Occupella webhook connect for ${connectorName} is coming soon — Buildium is live today.`;

  return (
    <DetailShell
      title={`${connectorName} · ${capability === 'api' ? 'API' : 'Webhooks'}`}
      subtitle={subtitle}
      onClose={onClose}
    >
      <div
        className="rounded-[var(--r-md)] p-5 flex flex-col gap-3 items-start"
        style={{
          background: 'var(--iris-tint)',
          border: '1px solid var(--iris-ring)',
        }}
      >
        <span
          className=" uppercase px-2 py-0.5 rounded-[var(--r-xs)]"
          style={{
            fontSize: 12,
            letterSpacing: '0.14em',
            background: 'var(--iris)',
            color: 'var(--canvas)',
          }}
        >
          Coming soon
        </span>
        <p className="text-[13px]" style={{ color: 'var(--ink)' }}>
          {message}
        </p>
        <p className="text-[12px]" style={{ color: 'var(--ink-muted)' }}>
          We'll wire {connectorName} {capability === 'api' ? 'API' : 'webhooks'} once
          the Buildium flow is exercised in production. Reach out if this is
          blocking your demo and we'll prioritise it.
        </p>
      </div>
    </DetailShell>
  );
}
