/**
 * Property cockpit detail (Workstream A, master #5).
 *
 * The "click a property → a real page" view: the starter-six operational
 * picture for one property, computed from the backend's Buildium entity
 * mirror (no live Buildium calls on this path). Rendered inside
 * PropertiesPage when a row's name is clicked; the Back control returns to
 * the grid. DESIGN.md: token palette only, 1px var(--line) card borders,
 * no gradients, numbers read in tabular figures via the body default.
 */

import { useEffect, useState } from 'react';
import { ArrowLeft, Wrench, AlertTriangle, Users } from 'lucide-react';
import { fetchCockpitDetail, type CockpitDetail } from '../services/properties';
import { delinquencyTile, listOrNotSynced, notSynced } from '../lib/notSynced';
import { occupancySub } from '../lib/occupancy';
import { RenewalsSection } from './properties/RenewalsSection';
import { TurnoversSection } from './properties/TurnoversSection';
import { UnitDetailPane } from './properties/UnitDetailPane';
import { TenantDetailPane } from './properties/TenantDetailPane';

interface Props {
  propertyId: number;
  propertyName: string;
  onBack: () => void;
}

const money = (n: number | null | undefined) =>
  n == null
    ? '—'
    : n.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div
      className="rounded-[var(--r-md)] border px-4 py-3"
      style={{ borderColor: 'var(--card-edge)', background: 'var(--canvas)' }}
    >
      <div
        className="text-[12px] uppercase tracking-[0.08em] mb-1"
        style={{ color: 'var(--ink-muted)' }}
      >
        {label}
      </div>
      <div className="text-[20px] font-semibold" style={{ color: 'var(--ink)' }}>
        {value}
      </div>
      {sub ? (
        <div className="text-[12px] mt-0.5" style={{ color: 'var(--ink-secondary)' }}>
          {sub}
        </div>
      ) : null}
    </div>
  );
}

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
      style={{ borderColor: 'var(--card-edge)', background: 'var(--canvas)' }}
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

export function PropertyCockpit({ propertyId, propertyName, onBack }: Props) {
  const [detail, setDetail] = useState<CockpitDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [focus, setFocus] = useState<{ kind: 'unit' | 'tenant'; id: number; label: string } | null>(
    null,
  );

  useEffect(() => {
    let alive = true;
    setDetail(null);
    setError(null);
    fetchCockpitDetail(propertyId)
      .then((d) => alive && setDetail(d))
      .catch((e: unknown) =>
        alive && setError(e instanceof Error ? e.message : 'Failed to load property'),
      );
    return () => {
      alive = false;
    };
  }, [propertyId]);

  const s = detail?.summary;
  const pms = detail?.pms_name;
  const balances = listOrNotSynced(
    detail?.delinquency_list,
    'No outstanding balances.',
    pms,
    'rent balances',
  );
  const tenants = listOrNotSynced(detail?.tenants, 'No active tenants mirrored.', pms, 'which tenant is on which lease');

  if (focus?.kind === 'unit') {
    return <UnitDetailPane unitId={focus.id} label={focus.label} onBack={() => setFocus(null)} />;
  }
  if (focus?.kind === 'tenant') {
    return (
      <TenantDetailPane tenantId={focus.id} label={focus.label} onBack={() => setFocus(null)} />
    );
  }

  return (
    <div data-testid="property-cockpit">
      <button
        type="button"
        onClick={onBack}
        className="inline-flex items-center gap-1.5 text-[13px] mb-3 hover:underline"
        style={{ color: 'var(--ink-secondary)' }}
      >
        <ArrowLeft size={13} strokeWidth={1.7} /> All properties
      </button>

      <h2 className="text-[16px] font-semibold mb-0.5" style={{ color: 'var(--ink)' }}>
        {s?.name ?? propertyName}
      </h2>
      <div className="text-[13px] mb-4" style={{ color: 'var(--ink-muted)' }}>
        {s ? [s.address_line1, s.city, s.state].filter(Boolean).join(', ') : ' '}
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
      ) : !detail || !s ? (
        <div className="text-[13px]" style={{ color: 'var(--ink-muted)' }}>
          Loading…
        </div>
      ) : (
        <div className="flex flex-col gap-4">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <Stat
              label="Occupancy"
              value={s.unit_count ? `${s.occupied_count}/${s.unit_count}` : '—'}
              sub={occupancySub(s)}
            />
            <Stat
              label="Rent roll"
              value={money(detail.rent_scheduled)}
              sub={
                // An unknown rent is said, never summed as $0 (rule 14).
                detail.rent_unknown_leases
                  ? `Rent not known for ${detail.rent_unknown_leases} active lease${
                      detail.rent_unknown_leases === 1 ? '' : 's'
                    }`
                  : detail.rent_collected_mtd == null
                    ? notSynced(detail.pms_name, 'payments')
                    : `${money(detail.rent_collected_mtd)} collected this month`
              }
            />
            <Stat
              label="Open work orders"
              value={String(s.open_work_orders)}
              sub={s.open_work_orders ? undefined : 'All clear'}
            />
            {/* ⚠ Through delinquencyTile: a null count used to fall into
                the '$0' / 'Everyone current' branch for every Rentvine property. */}
            <Stat
              label="Delinquent"
              {...delinquencyTile(s.delinquent_leases, s.delinquent_total, detail.pms_name, money)}
            />
          </div>

          <Section
            icon={<Wrench size={13} strokeWidth={1.7} style={{ color: 'var(--ink-muted)' }} />}
            title="Open work orders"
            empty="No open work orders."
            count={detail.open_work_order_list.length}
          >
            {detail.open_work_order_list.map((wo) =>
              wo.unit_id != null ? (
                <button
                  key={wo.buildium_id}
                  type="button"
                  onClick={() =>
                    setFocus({ kind: 'unit', id: wo.unit_id!, label: `Unit #${wo.unit_id}` })
                  }
                  className="flex w-full items-center justify-between py-1.5 text-[13px] cursor-pointer text-left"
                >
                  <span className="hover:underline" style={{ color: 'var(--iris)' }}>
                    {wo.title ?? `#${wo.buildium_id}`}
                  </span>
                  <span style={{ color: wo.priority === 'High' ? 'var(--caution)' : 'var(--ink-muted)' }}>
                    {[wo.priority, wo.status].filter(Boolean).join(' · ')}
                  </span>
                </button>
              ) : (
                <div
                  key={wo.buildium_id}
                  className="flex items-center justify-between py-1.5 text-[13px]"
                >
                  <span style={{ color: 'var(--ink)' }}>{wo.title ?? `#${wo.buildium_id}`}</span>
                  <span style={{ color: wo.priority === 'High' ? 'var(--caution)' : 'var(--ink-muted)' }}>
                    {[wo.priority, wo.status].filter(Boolean).join(' · ')}
                  </span>
                </div>
              ),
            )}
          </Section>

          <RenewalsSection propertyId={propertyId} />

          <TurnoversSection propertyId={propertyId} />

          <Section
            icon={
              <AlertTriangle size={13} strokeWidth={1.7} style={{ color: 'var(--ink-muted)' }} />
            }
            title="Outstanding balances"
            empty={balances.empty}
            count={balances.items.length}
          >
            {balances.items.map((b) =>
              b.unit_id != null ? (
                <button
                  key={b.lease_id}
                  type="button"
                  onClick={() =>
                    setFocus({
                      kind: 'unit',
                      id: b.unit_id!,
                      label: b.unit_number ? `Unit ${b.unit_number}` : `Unit #${b.unit_id}`,
                    })
                  }
                  className="flex w-full items-center justify-between py-1.5 text-[13px] cursor-pointer text-left"
                >
                  <span className="hover:underline" style={{ color: 'var(--iris)' }}>
                    {b.unit_number ? `Unit ${b.unit_number}` : `Lease #${b.lease_id}`}
                  </span>
                  <span style={{ color: 'var(--danger)' }}>{money(b.total_balance)}</span>
                </button>
              ) : (
                <div
                  key={b.lease_id}
                  className="flex items-center justify-between py-1.5 text-[13px]"
                >
                  <span style={{ color: 'var(--ink)' }}>
                    {b.unit_number ? `Unit ${b.unit_number}` : `Lease #${b.lease_id}`}
                  </span>
                  <span style={{ color: 'var(--danger)' }}>{money(b.total_balance)}</span>
                </div>
              ),
            )}
          </Section>

          <Section
            icon={<Users size={13} strokeWidth={1.7} style={{ color: 'var(--ink-muted)' }} />}
            title="Tenants"
            empty={tenants.empty}
            count={tenants.items.length}
          >
            {tenants.items.map((t) => {
              const name =
                [t.first_name, t.last_name].filter(Boolean).join(' ') || `#${t.buildium_id}`;
              return (
                <div
                  key={t.buildium_id}
                  className="flex items-center justify-between py-1.5 text-[13px]"
                >
                  <button
                    type="button"
                    onClick={() => setFocus({ kind: 'tenant', id: t.buildium_id, label: name })}
                    className="cursor-pointer text-left hover:underline"
                    style={{ color: 'var(--iris)' }}
                  >
                    {name}
                  </button>
                  {t.unit_number && t.unit_id != null ? (
                    <button
                      type="button"
                      onClick={() =>
                        setFocus({
                          kind: 'unit',
                          id: t.unit_id!,
                          label: `Unit ${t.unit_number}`,
                        })
                      }
                      className="cursor-pointer hover:underline"
                      style={{ color: 'var(--ink-muted)' }}
                    >
                      {`Unit ${t.unit_number}`}
                    </button>
                  ) : (
                    <span style={{ color: 'var(--ink-muted)' }}>
                      {t.unit_number ? `Unit ${t.unit_number}` : ''}
                    </span>
                  )}
                </div>
              );
            })}
          </Section>
        </div>
      )}
    </div>
  );
}
