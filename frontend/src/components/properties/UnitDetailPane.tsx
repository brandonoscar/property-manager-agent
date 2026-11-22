/**
 * Unit detail (drill-down from the property cockpit).
 *
 * One unit's operational picture from the backend's entity mirror: the
 * current lease + tenants, outstanding balance, maintenance history, and
 * open tasks. Rendered inside PropertyCockpit when a unit is focused; the
 * Back control returns to the cockpit. DESIGN.md: token palette only,
 * 1px var(--line) card borders, no gradients, tabular figures via the
 * body default.
 */

import { useEffect, useState } from 'react';
import { ArrowLeft, FileText, Wallet, Wrench, History, ListChecks } from 'lucide-react';
import { fetchUnitDetail, type UnitDetail } from '../../services/properties';
import { formatCalendarDate } from '../../lib/calendarDate';
import { listOrNotSynced, notSynced } from '../../lib/notSynced';

interface Props {
  unitId: number;
  label: string;
  onBack: () => void;
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

const AGING_BUCKETS = [
  ['0–30 days', 'balance_0_30'],
  ['31–60 days', 'balance_31_60'],
  ['61–90 days', 'balance_61_90'],
  ['Over 90 days', 'balance_over_90'],
] as const;

export function UnitDetailPane({ unitId, label, onBack }: Props) {
  const [detail, setDetail] = useState<UnitDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setDetail(null);
    setError(null);
    fetchUnitDetail(unitId)
      .then((d) => alive && setDetail(d))
      .catch((e: unknown) =>
        alive && setError(e instanceof Error ? e.message : 'Failed to load unit'),
      );
    return () => {
      alive = false;
    };
  }, [unitId]);

  const u = detail?.unit;
  const lease = detail?.current_lease;
  const tasks = listOrNotSynced(detail?.open_tasks, 'No open tasks on this unit.', detail?.pms_name, 'tasks');

  return (
    <div data-testid="unit-detail-pane">
      <button
        type="button"
        onClick={onBack}
        className="inline-flex items-center gap-1.5 text-[13px] mb-3 hover:underline"
        style={{ color: 'var(--ink-secondary)' }}
      >
        <ArrowLeft size={13} strokeWidth={1.7} /> Back to property
      </button>

      <div className="flex items-center gap-2 mb-0.5">
        <h2 className="text-[16px] font-semibold" style={{ color: 'var(--ink)' }}>
          {u?.unit_number ? `Unit ${u.unit_number}` : label}
          {u?.property_name ? ` · ${u.property_name}` : ''}
        </h2>
        {u ? (
          <span
            className="text-[12px] px-1.5 py-0.5 rounded-[var(--r-sm)] border"
            style={{
              borderColor: 'var(--line)',
              color: u.is_occupied ? 'var(--positive)' : 'var(--ink-muted)',
            }}
          >
            {/* null is UNKNOWN (a Rentvine unit read from the export), never vacant */}
            {u.is_occupied == null ? 'Occupancy unknown' : u.is_occupied ? 'Occupied' : 'Vacant'}
          </span>
        ) : null}
      </div>
      <div className="text-[13px] mb-4" style={{ color: 'var(--ink-muted)' }}>
        {u ? `Market rent ${money(u.market_rent)}` : ' '}
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
      ) : !detail || !u ? (
        <div className="text-[13px]" style={{ color: 'var(--ink-muted)' }}>
          Loading…
        </div>
      ) : (
        <div className="flex flex-col gap-4">
          <Section
            icon={<FileText size={13} strokeWidth={1.7} style={{ color: 'var(--ink-muted)' }} />}
            title="Current lease"
            empty="No active lease on this unit."
            count={lease ? 1 : 0}
          >
            {lease ? (
              <>
                <div className="flex items-center justify-between py-1.5 text-[13px]">
                  <span style={{ color: 'var(--ink)' }}>
                    {lease.status ?? 'Lease'} · {date(lease.lease_from_date)} –{' '}
                    {date(lease.lease_to_date)}
                  </span>
                  <span style={{ color: 'var(--ink-muted)' }}>{money(lease.rent)}/mo</span>
                </div>
                {detail.tenants == null ? (
                  <div className="py-1.5 text-[13px]" style={{ color: 'var(--ink-muted)' }}>
                    {notSynced(detail.pms_name, 'which tenant is on which lease')}
                  </div>
                ) : null}
                {(detail.tenants ?? []).map((t) => (
                  <div
                    key={t.buildium_id}
                    className="flex items-center justify-between py-1.5 text-[13px]"
                  >
                    <span style={{ color: 'var(--ink)' }}>
                      {[t.first_name, t.last_name].filter(Boolean).join(' ') || `#${t.buildium_id}`}
                    </span>
                    <span style={{ color: 'var(--ink-muted)' }}>
                      {[t.email, t.phone].filter(Boolean).join(' · ')}
                    </span>
                  </div>
                ))}
              </>
            ) : null}
          </Section>

          <Section
            icon={<Wallet size={13} strokeWidth={1.7} style={{ color: 'var(--ink-muted)' }} />}
            title="Balance"
            empty={
              // ⚠ A missing balance row reads "nothing owed" only when the
              // system sends balances at all.
              detail.balances_sent === false
                ? notSynced(detail.pms_name, 'rent balances')
                : 'No outstanding balance on this lease.'
            }
            count={detail.balance ? 1 : 0}
          >
            {detail.balance ? (
              <>
                <div className="flex items-center justify-between py-1.5 text-[13px]">
                  <span style={{ color: 'var(--ink)' }}>Total outstanding</span>
                  <span style={{ color: 'var(--danger)' }}>
                    {money(detail.balance.total_balance)}
                  </span>
                </div>
                {AGING_BUCKETS.map(([bucketLabel, key]) =>
                  detail.balance && detail.balance[key] != null ? (
                    <div
                      key={key}
                      className="flex items-center justify-between py-1.5 text-[13px]"
                    >
                      <span style={{ color: 'var(--ink-muted)' }}>{bucketLabel}</span>
                      <span style={{ color: 'var(--ink)' }}>{money(detail.balance[key])}</span>
                    </div>
                  ) : null,
                )}
              </>
            ) : null}
          </Section>

          <Section
            icon={<Wrench size={13} strokeWidth={1.7} style={{ color: 'var(--ink-muted)' }} />}
            title="Open work orders"
            empty="No open work orders on this unit."
            count={detail.open_work_orders.length}
          >
            {detail.open_work_orders.map((wo) => (
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

          <Section
            icon={<History size={13} strokeWidth={1.7} style={{ color: 'var(--ink-muted)' }} />}
            title="Recent maintenance"
            empty="No maintenance history on this unit."
            count={detail.recent_work_orders.length}
          >
            {detail.recent_work_orders.map((wo) => (
              <div
                key={wo.buildium_id}
                className="flex items-center justify-between py-1.5 text-[13px]"
              >
                <span style={{ color: 'var(--ink)' }}>{wo.title ?? `#${wo.buildium_id}`}</span>
                <span style={{ color: 'var(--ink-muted)' }}>
                  {[wo.status, date(wo.updated_at)].filter(Boolean).join(' · ')}
                </span>
              </div>
            ))}
          </Section>

          <Section
            icon={<ListChecks size={13} strokeWidth={1.7} style={{ color: 'var(--ink-muted)' }} />}
            title="Open tasks"
            empty={tasks.empty}
            count={tasks.items.length}
          >
            {tasks.items.map((task) => (
              <div
                key={task.buildium_id}
                className="flex items-center justify-between py-1.5 text-[13px]"
              >
                <span style={{ color: 'var(--ink)' }}>{task.title ?? `#${task.buildium_id}`}</span>
                <span style={{ color: 'var(--ink-muted)' }}>
                  {[task.category_name, task.status, task.due_date ? `due ${date(task.due_date)}` : null]
                    .filter(Boolean)
                    .join(' · ')}
                </span>
              </div>
            ))}
          </Section>
        </div>
      )}
    </div>
  );
}
