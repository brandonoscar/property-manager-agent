/**
 * Tenant detail (drill-down from the property cockpit).
 *
 * One tenant's picture from the backend's entity mirror: their leases (with
 * balances), recent payments and charges, and open maintenance on the units
 * of their active leases. Rendered inside PropertyCockpit when a tenant is
 * focused; the Back control returns to the cockpit. DESIGN.md: token palette
 * only, 1px var(--line) card borders, no gradients, tabular figures via the
 * body default.
 */

import { useEffect, useState } from 'react';
import { ArrowLeft, FileText, Receipt, Wrench } from 'lucide-react';
import { fetchTenantDetail, type TenantDetail } from '../../services/properties';
import { ContactLinks } from '../operations/ContactLinks';
import { formatCalendarDate } from '../../lib/calendarDate';
import { listOrNotSynced } from '../../lib/notSynced';

interface Props {
  tenantId: number;
  label: string;
  onBack: () => void;
  /** Where Back goes, in the words of the surface that opened this. The pane
   *  is now reachable from two places — the property cockpit and the
   *  Operations → Tenants table — and a control that says "Back to property"
   *  on a route the user never came through is a small lie about where they
   *  are. */
  backLabel?: string;
}

const money = (n: number | null | undefined) =>
  n == null
    ? '—'
    : n.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });

// Buildium calendar dates, not instants — see lib/calendarDate.
const date = (d: string | null | undefined) => formatCalendarDate(d);

function Section({
  icon,
  title,
  empty,
  children,
  count,
}: {
  icon: React.ReactNode;
  title: string;
  empty: string;
  count: number;
  children: React.ReactNode;
}) {
  return (
    <div
      className="rounded-[var(--r-md)] border"
      style={{ borderColor: 'var(--line)', background: 'var(--canvas-raised)' }}
    >
      <div
        className="flex items-center gap-2 px-4 py-2.5 border-b text-[13px] font-medium"
        style={{ borderColor: 'var(--line)', color: 'var(--ink)' }}
      >
        {icon}
        {title}
      </div>
      {count === 0 ? (
        <div className="px-4 py-3 text-[13px]" style={{ color: 'var(--ink-muted)' }}>
          {empty}
        </div>
      ) : (
        <div className="px-4 py-2">{children}</div>
      )}
    </div>
  );
}

export function TenantDetailPane({
  tenantId,
  label,
  onBack,
  backLabel = 'Back to property',
}: Props) {
  const [detail, setDetail] = useState<TenantDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setDetail(null);
    setError(null);
    fetchTenantDetail(tenantId)
      .then((d) => alive && setDetail(d))
      .catch((e: unknown) =>
        alive && setError(e instanceof Error ? e.message : 'Failed to load tenant'),
      );
    return () => {
      alive = false;
    };
  }, [tenantId]);

  const t = detail?.tenant;
  // Every list here is reached through the tenant's lease links, so a system
  // that doesn't send those leaves all three unknown rather than empty.
  const pms = detail?.pms_name;
  const links = 'which tenant is on which lease';
  const leases = listOrNotSynced(detail?.leases, 'No leases mirrored for this tenant.', pms, links);
  const transactions = listOrNotSynced(
    detail?.recent_transactions,
    'No transactions mirrored for this tenant.',
    pms,
    detail?.leases == null ? links : 'payments and charges',
  );
  const workOrders = listOrNotSynced(
    detail?.open_work_orders,
    'No open work orders on their units.',
    pms,
    links,
  );
  const name = t
    ? [t.first_name, t.last_name].filter(Boolean).join(' ') || `#${t.buildium_id}`
    : label;

  return (
    <div data-testid="tenant-detail-pane">
      <button
        type="button"
        onClick={onBack}
        className="inline-flex items-center gap-1.5 text-[13px] mb-3 hover:underline"
        style={{ color: 'var(--ink-secondary)' }}
      >
        <ArrowLeft size={13} strokeWidth={1.7} /> {backLabel}
      </button>

      <h2 className="text-[16px] font-semibold mb-0.5" style={{ color: 'var(--ink)' }}>
        {name}
      </h2>
      <div className="mb-4 min-h-[20px]">
        {/* Was a dot-joined string of the same two facts, selectable and
            nothing else. Same data, now actionable. */}
        {t && <ContactLinks email={t.email} phone={t.phone} variant="detail" testIdPrefix="tenant-detail" />}
      </div>

      {error ? (
        <div
          className="rounded-[var(--r-md)] border px-4 py-3 text-[13px]"
          style={{ borderColor: 'var(--line)', color: 'var(--ink-secondary)' }}
        >
          {/* The backend's own sentence names this workspace's system and its
              sync button (`not_synced_yet`, api/routes/properties.py). */}
          {error}
        </div>
      ) : !detail || !t ? (
        <div className="text-[13px]" style={{ color: 'var(--ink-muted)' }}>
          Loading…
        </div>
      ) : (
        <div className="flex flex-col gap-4">
          <Section
            icon={<FileText size={13} strokeWidth={1.7} style={{ color: 'var(--ink-muted)' }} />}
            title="Leases"
            empty={leases.empty}
            count={leases.items.length}
          >
            {leases.items.map((lease) => (
              <div
                key={lease.buildium_id}
                className="flex items-center justify-between py-1.5 text-[13px]"
              >
                <span style={{ color: 'var(--ink)' }}>
                  {[lease.property_name, lease.unit_number ? `Unit ${lease.unit_number}` : null]
                    .filter(Boolean)
                    .join(' · ') || `Lease #${lease.buildium_id}`}
                  <span style={{ color: 'var(--ink-muted)' }}>
                    {' '}
                    · {lease.status ?? '—'} · {date(lease.lease_from_date)} –{' '}
                    {date(lease.lease_to_date)}
                  </span>
                </span>
                <span style={{ color: 'var(--ink-muted)' }}>
                  {money(lease.rent)}/mo
                  {lease.total_balance != null && lease.total_balance > 0 ? (
                    <span style={{ color: 'var(--danger)' }}>
                      {' '}
                      · {money(lease.total_balance)} owed
                    </span>
                  ) : null}
                </span>
              </div>
            ))}
          </Section>

          <Section
            icon={<Receipt size={13} strokeWidth={1.7} style={{ color: 'var(--ink-muted)' }} />}
            title="Recent payments & charges"
            empty={transactions.empty}
            count={transactions.items.length}
          >
            {transactions.items.map((tx, i) => (
              <div
                key={`${tx.lease_id}-${tx.transaction_date}-${i}`}
                className="flex items-center justify-between py-1.5 text-[13px]"
              >
                <span style={{ color: 'var(--ink)' }}>
                  {tx.transaction_type ?? 'Transaction'}
                  <span style={{ color: 'var(--ink-muted)' }}> · {date(tx.transaction_date)}</span>
                </span>
                <span
                  style={{
                    color: tx.transaction_type === 'Payment' ? 'var(--positive)' : 'var(--ink)',
                  }}
                >
                  {money(tx.total_amount)}
                </span>
              </div>
            ))}
          </Section>

          <Section
            icon={<Wrench size={13} strokeWidth={1.7} style={{ color: 'var(--ink-muted)' }} />}
            title="Open work orders"
            empty={workOrders.empty}
            count={workOrders.items.length}
          >
            {workOrders.items.map((wo) => (
              <div
                key={wo.buildium_id}
                className="flex items-center justify-between py-1.5 text-[13px]"
              >
                <span style={{ color: 'var(--ink)' }}>{wo.title ?? `#${wo.buildium_id}`}</span>
                <span
                  style={{ color: wo.priority === 'High' ? 'var(--caution)' : 'var(--ink-muted)' }}
                >
                  {[wo.priority, wo.status].filter(Boolean).join(' · ')}
                </span>
              </div>
            ))}
          </Section>
        </div>
      )}
    </div>
  );
}
