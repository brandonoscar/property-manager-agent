/**
 * Operations — the key-entity hub that replaced the Properties page
 * (founder direction, 2026-08-19): "where you find properties, tenants,
 * vendors, team members … this is where you would see key analytics too."
 *
 * Four tabs, each ONE sortable table where the columns are the analytics
 * (OpsTable) — no separate dashboard:
 *   Properties → the cockpit rollups; row click opens PropertyCockpit.
 *   Tenants    → balance, aging bucket, rent, lease end, property/unit.
 *   Vendors    → open/total WOs, jobs 90d, avg days to complete, 12mo spend.
 *   Team       → Buildium staff with open WOs/tasks + done-this-week; the
 *                old ×Roles routing grid stays reachable from here as
 *                "Assignment rules" (PropertiesPage, unchanged).
 *
 * All data is the local Buildium mirror — instant, no live API on render.
 */

import { useEffect, useMemo, useState } from 'react';
import { ArrowLeft, Settings2 } from 'lucide-react';
import { toast } from 'sonner';

import { fetchCockpitOverview, type CockpitSummary } from '../services/properties';
import { knownOccupancyRatio } from '../lib/occupancy';
import { runBuildiumBackfill } from '../services/buildium';
import { runPropertywareSync } from '../services/propertyware';
import { runDoorLoopSync } from '../services/doorloop';
import { runRentManagerSync } from '../services/rentmanager';
import { runRentvineSync } from '../services/rentvine';
import { pmsInfo, type PmsId } from '../lib/pms';
import { ApiError } from '../services/api';
import { useStore } from '../stores/store';
import {
  fetchTeamOps,
  fetchTenantOps,
  fetchVendorOps,
  type TeamOps,
  type TenantOps,
  type VendorOps,
} from '../services/operations';
import { ContactLinks } from './operations/ContactLinks';
import { OpsTable, type OpsColumn } from './operations/OpsTable';
import { RefreshButton } from './RefreshButton';
import { PropertyCockpit } from './PropertyCockpit';
import { PropertiesPage } from './PropertiesPage';
import { TenantDetailPane } from './properties/TenantDetailPane';
import { DataFreshness } from './DataFreshness';
import { formatCalendarDate } from '../lib/calendarDate';
import { NotSyncedRows, notSynced, sumKnown } from '../lib/notSynced';

type Tab = 'properties' | 'tenants' | 'vendors' | 'team';

const TABS: Array<{ id: Tab; label: string }> = [
  { id: 'properties', label: 'Properties' },
  { id: 'tenants', label: 'Tenants' },
  { id: 'vendors', label: 'Vendors' },
  { id: 'team', label: 'Team' },
];

const money = (n: number | null | undefined): string =>
  n == null
    ? '—'
    : n.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });

// Buildium calendar dates, not instants — see lib/calendarDate.
const dateShort = (iso: string | null): string => formatCalendarDate(iso, 'short');

const personName = (first: string | null, last: string | null): string =>
  [first, last].filter(Boolean).join(' ') || '—';

/**
 * What Refresh pulls from, per system: that system's own sync, never another's.
 * ⚠ Keyed by every `PmsId`, so a system added to lib/pms.ts fails `tsc` here
 * until someone decides what Refresh does for it. Before this map, any system
 * that was not Rentvine got the Buildium backfill, which answered a working
 * connection "No saved credentials."
 */
const PULL: Readonly<Record<PmsId, () => Promise<{ ok: boolean; error?: string | null }>>> = {
  buildium: () => runBuildiumBackfill(),
  rentvine: () => runRentvineSync(),
  rentmanager: () => runRentManagerSync(),
  propertyware: () => runPropertywareSync(),
  doorloop: () => runDoorLoopSync(),
};

function BucketPill({ bucket }: { bucket: string | null }) {
  if (!bucket) return <span style={{ color: 'var(--ink-faint)' }}>current</span>;
  const hot = bucket === '90+' || bucket === '61-90';
  return (
    <span
      className="font-mono px-1.5 py-0.5 rounded-[var(--r-xs)]"
      style={{
        fontSize: 12,
        background: hot ? 'var(--danger-bg)' : 'var(--caution-bg)',
        color: hot ? 'var(--danger)' : 'var(--caution)',
      }}
    >
      {bucket} days
    </span>
  );
}

export function OperationsPage() {
  const [tab, setTab] = useState<Tab>('properties');
  const [properties, setProperties] = useState<CockpitSummary[] | null>(null);
  const [tenants, setTenants] = useState<TenantOps[] | NotSyncedRows | null>(null);
  const [vendors, setVendors] = useState<VendorOps[] | null>(null);
  const [team, setTeam] = useState<TeamOps[] | NotSyncedRows | null>(null);
  const [cockpit, setCockpit] = useState<{ id: number; name: string } | null>(null);
  const [focusedTenant, setFocusedTenant] = useState<{ id: number; label: string } | null>(null);
  const [showRouting, setShowRouting] = useState(false);
  const [resyncing, setResyncing] = useState(false);
  // Bumped after a resync so the freshness line re-reads. It fetches once on
  // mount, so without this the stamp the user just moved keeps its old value
  // on screen — the exact "did that do anything" gap the resync exists to
  // close (RefreshButton's own note).
  const [freshnessKey, setFreshnessKey] = useState(0);
  // Which system this workspace syncs from. The Re-sync and every sentence
  // naming the source follow it. ⚠ Unknown (the lookup failed, or a backend
  // that predates the field) keeps the Buildium behaviour this page always
  // had, rather than guessing Rentvine.
  // Read from the freshness caption's own fetch (`onLoad` below).
  const [source, setSource] = useState<{ pms: string | null; name: string | null }>({
    pms: null,
    name: null,
  });
  // `null` for an unknown or missing id, which keeps the Buildium behaviour.
  const pms = pmsInfo(source.pms);
  // Staff routing and the created-date note are Buildium's alone: every other
  // system is read from the synced copy and does not send them.
  const isBuildium = pms === null || pms.liveTools;
  const sourceName = isBuildium ? 'Buildium' : (source.name ?? pms.name);
  const openSettings = useStore((s) => s.openSettings);

  /**
   * Pull this workspace's system into the mirror ({@link PULL}), then redraw
   * this tab from it.
   *
   * ⚠ **Every pull is ADMIN-ONLY** (`POST /buildium/backfill`, each connector's
   * `POST /sync`), so a manager clicking this
   * gets a 403 on a button they can see. That is not an error to shout about:
   * the tab reload still works and is still worth doing, so a 403 degrades to
   * exactly the old behaviour with a line saying who can do the other half.
   * Throwing a red toast at somebody for lacking a permission they cannot
   * grant themselves is noise.
   */
  const resync = async () => {
    if (resyncing) return;
    setResyncing(true);
    let synced = false;
    try {
      // An unknown system keeps the Buildium behaviour this page always had.
      const result = await PULL[pms?.id ?? 'buildium']();
      synced = result.ok;
      // ⚠ The sentence is the connector's own, written for Settings →
      // Connectors: it can say "Run Test connection" or "Sync now", and
      // neither button is on this page. The toast carries the way there, so
      // the step it names is one the user can take (CLAUDE.md rule 16).
      if (!result.ok && result.error) {
        toast.error(result.error, {
          duration: 10_000,
          action: { label: 'Open Connectors', onClick: () => openSettings('connectors') },
        });
      }
    } catch (err) {
      const forbidden = err instanceof ApiError && err.status === 403;
      toast[forbidden ? 'message' : 'error'](
        forbidden
          ? `Refreshed this page. Pulling fresh data from ${sourceName} needs an admin.`
          : err instanceof Error
            ? err.message
            : `Could not reach ${sourceName}.`,
      );
    } finally {
      setResyncing(false);
    }
    // ⚠ Unconditional. A failed Buildium pull leaves the mirror exactly as it
    // was, and the user still asked to see the current rows — skipping the
    // reload on failure would make the button do nothing at all on the path
    // where it already did least.
    if (tab === 'properties') setProperties(null);
    else if (tab === 'tenants') setTenants(null);
    else if (tab === 'vendors') setVendors(null);
    else if (tab === 'team') setTeam(null);
    if (synced) setFreshnessKey((k) => k + 1);
  };

  // Each tab loads once, on first visit, then sticks (mirror data is
  // cheap but there's no reason to refetch on every tab flip).
  useEffect(() => {
    const fail = (what: string) => (err: unknown) =>
      toast.error(err instanceof Error ? err.message : `Could not load ${what}.`, {
        duration: 8000,
      });
    if (tab === 'properties' && properties === null)
      fetchCockpitOverview().then(setProperties).catch(fail('properties'));
    if (tab === 'tenants' && tenants === null)
      fetchTenantOps().then(setTenants).catch(fail('tenants'));
    if (tab === 'vendors' && vendors === null)
      fetchVendorOps().then(setVendors).catch(fail('vendors'));
    if (tab === 'team' && team === null) fetchTeamOps().then(setTeam).catch(fail('team'));
  }, [tab, properties, tenants, vendors, team]);

  const propertyCols = useMemo<Array<OpsColumn<CockpitSummary>>>(
    () => [
      {
        key: 'name',
        label: 'Property',
        value: (r) => r.name ?? '',
        render: (r) => (
          <div>
            <div className="font-medium" style={{ color: 'var(--iris)' }}>
              {r.name ?? `#${r.buildium_id}`}
            </div>
            <div style={{ color: 'var(--ink-faint)', fontSize: 12 }}>
              {[r.address_line1, r.city].filter(Boolean).join(', ')}
            </div>
          </div>
        ),
      },
      {
        key: 'occupancy',
        label: 'Occupancy',
        numeric: true,
        // Sorts on occupied over the units whose occupancy is KNOWN: an
        // unknown unit never ranks as vacant (rule 14, `lib/occupancy.ts`).
        value: (r) => (r.unit_count ? knownOccupancyRatio(r) : null),
        render: (r) =>
          r.unit_count
            ? `${r.occupied_count}/${r.unit_count}` +
              (r.occupancy_unknown_count ? ` · ${r.occupancy_unknown_count} unknown` : '')
            : '—',
        // ⚠ Occupancy is the reason `total` is a FUNCTION and not a `sum: true`
        // flag. A flag would add sixty ratios together and print something like
        // "12.4", which is meaningless and still looks like an answer. The
        // portfolio figure is the POOLED one — every occupied unit over every
        // unit — and only the column knows that.
        total: (rows) => {
          const units = rows.reduce((n, r) => n + (r.unit_count ?? 0), 0);
          if (!units) return '—';
          const unknown = rows.reduce((n, r) => n + (r.occupancy_unknown_count ?? 0), 0);
          const pooled = `${rows.reduce((n, r) => n + (r.occupied_count ?? 0), 0)}/${units}`;
          return unknown ? `${pooled} · ${unknown} unknown` : pooled;
        },
      },
      {
        key: 'open_wos',
        label: 'Open WOs',
        numeric: true,
        value: (r) => r.open_work_orders,
        total: (rows) => rows.reduce((n, r) => n + (r.open_work_orders ?? 0), 0),
      },
      {
        key: 'expiring',
        label: 'Leases end 90d',
        numeric: true,
        value: (r) => r.expiring_leases_90d,
        total: (rows) => rows.reduce((n, r) => n + (r.expiring_leases_90d ?? 0), 0),
      },
      {
        key: 'delinquent',
        label: 'Delinquent',
        numeric: true,
        value: (r) => r.delinquent_total,
        // Money owed across the portfolio — the figure a manager is actually
        // scanning for, and the one that took adding sixty rows by eye.
        // Formatted with the SAME helper the cells use: a total in a different
        // shape from the column above it reads as a different kind of number.
        // ⚠ `sumKnown`, not `?? 0`: one property whose system sends no
        // balances makes the portfolio total unknown, not smaller.
        total: (rows) => {
          const owed = sumKnown(rows, (r) => r.delinquent_total);
          return owed != null && owed > 0 ? money(owed) : '—';
        },
        render: (r) =>
          r.delinquent_total == null ? (
            <span
              title={notSynced(r.pms_name, 'rent balances')}
              style={{ color: 'var(--ink-faint)' }}
            >
              not synced
            </span>
          ) : (
            <span style={{ color: r.delinquent_total > 0 ? 'var(--danger)' : undefined }}>
              {r.delinquent_total > 0
                ? `${money(r.delinquent_total)} · ${r.delinquent_leases}`
                : '—'}
            </span>
          ),
      },
    ],
    [],
  );

  const tenantCols = useMemo<Array<OpsColumn<TenantOps>>>(
    () => [
      {
        key: 'name',
        label: 'Tenant',
        value: (r) => `${r.last_name ?? ''} ${r.first_name ?? ''}`,
        // Iris + a row click, matching the Property column. Before this the
        // Tenants / Vendors / Team tables had NO `onRowClick` at all — only
        // Properties did — so a name that looked like a heading was the end
        // of the road, and `TenantDetailPane` was reachable only by going
        // through a property first.
        render: (r) => (
          <div>
            <div className="font-medium" style={{ color: 'var(--iris)' }}>
              {personName(r.first_name, r.last_name)}
            </div>
            <div className="mt-0.5">
              <ContactLinks
                email={r.email}
                phone={r.phone}
                testIdPrefix={`tenant-${r.tenant_buildium_id}`}
              />
            </div>
          </div>
        ),
      },
      {
        key: 'where',
        label: 'Property · Unit',
        value: (r) => r.property_name ?? '',
        render: (r) =>
          [r.property_name, r.unit_number].filter(Boolean).join(' · ') || '—',
      },
      {
        key: 'balance',
        label: 'Balance',
        numeric: true,
        value: (r) => r.balance,
        render: (r) =>
          r.balance == null ? (
            <span style={{ color: 'var(--ink-faint)' }}>not synced</span>
          ) : (
            <span style={{ color: r.balance > 0 ? 'var(--danger)' : undefined }}>
              {r.balance > 0 ? money(r.balance) : '—'}
            </span>
          ),
        // ⚠ Only POSITIVE balances are added. A credit balance is somebody who
        // paid ahead, and letting it cancel out arrears would understate what
        // is actually owed — the one direction this number must not be wrong
        // in. The cells hide credits too (`> 0 ? … : '—'`), so a total that
        // included them could not be reconciled against the rows above it.
        total: (rows) => {
          const owed = sumKnown(rows, (r) => (r.balance == null ? null : Math.max(r.balance, 0)));
          return owed != null && owed > 0 ? money(owed) : '—';
        },
      },
      {
        key: 'aging',
        label: 'Aging',
        value: (r) => r.oldest_bucket,
        render: (r) => <BucketPill bucket={r.oldest_bucket} />,
      },
      {
        key: 'rent',
        label: 'Rent',
        numeric: true,
        value: (r) => r.rent,
        render: (r) => money(r.rent),
        // The monthly rent roll for whoever is listed. ⚠ `money(null)` renders
        // "—", so a tenant with no rent on file contributes nothing rather
        // than a zero — a missing figure must not read as a real one.
        total: (rows) => money(rows.reduce((n, r) => n + (r.rent ?? 0), 0)),
      },
      {
        key: 'lease_end',
        label: 'Lease ends',
        numeric: true,
        value: (r) => r.lease_to_date,
        render: (r) => dateShort(r.lease_to_date),
      },
    ],
    [],
  );

  const vendorCols = useMemo<Array<OpsColumn<VendorOps>>>(
    () => [
      {
        key: 'name',
        label: 'Vendor',
        value: (r) => r.name ?? '',
        render: (r) => (
          <div>
            <div className="font-medium">
              {r.name ?? `#${r.buildium_id}`}
              {r.is_active === false && (
                <span
                  className="ml-2 uppercase"
                  style={{ fontSize: 12, color: 'var(--ink-faint)', letterSpacing: '0.1em' }}
                >
                  inactive
                </span>
              )}
            </div>
            <div style={{ color: 'var(--ink-faint)', fontSize: 12 }}>
              {r.category_name ?? ''}
            </div>
            {/* ⚠ No row click here. There is no vendor detail pane, and a name
                styled like a link that opens nothing is worse than a plain
                one — so the vendor's name stays ink and only the contact
                affordances are live. Same for Team. */}
            <div className="mt-0.5">
              <ContactLinks
                email={r.email}
                phone={r.phone}
                testIdPrefix={`vendor-${r.buildium_id}`}
              />
            </div>
          </div>
        ),
      },
      {
        key: 'open',
        label: 'Open WOs',
        numeric: true,
        value: (r) => r.open_work_orders,
        total: (rows) => sumKnown(rows, (r) => r.open_work_orders) ?? '—',
      },
      {
        key: 'jobs90',
        label: 'Jobs 90d',
        numeric: true,
        value: (r) => r.jobs_90d,
        total: (rows) => sumKnown(rows, (r) => r.jobs_90d) ?? '—',
      },
      {
        key: 'speed',
        label: 'Avg days to complete',
        numeric: true,
        value: (r) => r.avg_days_to_complete,
        render: (r) =>
          r.avg_days_to_complete == null ? '—' : r.avg_days_to_complete.toFixed(1),
        // ⚠ NO total, on purpose. Unlike occupancy this one cannot be pooled
        // either: a correct portfolio average needs each vendor's COMPLETED-job
        // count as the weight, and the row carries `jobs_90d` (jobs started in
        // a window), which is a different denominator. Averaging the averages
        // would over-weight a vendor who finished one job. The honest move is
        // to show nothing rather than a plausible wrong number.
      },
      {
        key: 'spend',
        // ⚠ "Billed", not "Spend". The mirror holds UNPAID bills only — the
        // sync cannot fetch paid ones without both paid-date bounds — so this
        // is what a vendor invoiced, not what we paid them. Live-verified
        // 2026-08-29: 0 of 67 mirrored bills are marked paid, so the column
        // labelled "Spend 12mo" was showing money nobody has recorded paying.
        label: 'Billed 12mo',
        numeric: true,
        value: (r) => r.spend_12m,
        render: (r) => (r.spend_12m != null && r.spend_12m > 0 ? money(r.spend_12m) : '—'),
        total: (rows) => {
          const billed = sumKnown(rows, (r) => r.spend_12m);
          return billed != null && billed > 0 ? money(billed) : '—';
        },
      },
      {
        key: 'bills',
        label: 'Bills 12mo',
        numeric: true,
        value: (r) => r.bill_count_12m,
        total: (rows) => sumKnown(rows, (r) => r.bill_count_12m) ?? '—',
      },
      {
        key: 'last_bill',
        label: 'Last bill',
        numeric: true,
        value: (r) => r.last_bill_date,
        render: (r) => dateShort(r.last_bill_date),
      },
    ],
    [],
  );

  const teamCols = useMemo<Array<OpsColumn<TeamOps>>>(
    () => [
      {
        key: 'name',
        label: 'Team member',
        value: (r) => `${r.last_name ?? ''} ${r.first_name ?? ''}`,
        render: (r) => (
          <div>
            <div className="font-medium">{personName(r.first_name, r.last_name)}</div>
            <div style={{ color: 'var(--ink-faint)', fontSize: 12 }}>
              {r.user_role_name ?? ''}
            </div>
            {/* Team rows carry no phone — `TeamOpsOut` has `email` only, and
                Buildium's staff record is where that stops. So this renders
                one link, not a greyed-out Call beside it. */}
            <div className="mt-0.5">
              <ContactLinks email={r.email} testIdPrefix={`team-${r.buildium_user_id}`} />
            </div>
          </div>
        ),
      },
      // The three team totals are what a manager is looking for on this tab:
      // what the team is carrying, and what it cleared this week.
      {
        key: 'open_wos',
        label: 'Open WOs',
        numeric: true,
        value: (r) => r.open_work_orders,
        total: (rows) => rows.reduce((n, r) => n + (r.open_work_orders ?? 0), 0),
      },
      {
        key: 'open_tasks',
        label: 'Open tasks',
        numeric: true,
        value: (r) => r.open_tasks,
        total: (rows) => rows.reduce((n, r) => n + (r.open_tasks ?? 0), 0),
      },
      {
        key: 'done_7d',
        label: 'Done this week',
        numeric: true,
        value: (r) => r.completed_tasks_7d,
        total: (rows) => rows.reduce((n, r) => n + (r.completed_tasks_7d ?? 0), 0),
      },
    ],
    [],
  );

  // The old ×Roles routing grid, reachable from the Team tab. Rendered in
  // place of the hub (PropertiesPage brings its own full-page layout).
  if (showRouting) {
    return (
      <div className="flex-1 flex flex-col min-h-0" data-testid="operations-routing">
        <div className="px-12 pt-6">
          <button
            type="button"
            onClick={() => setShowRouting(false)}
            className="inline-flex items-center gap-1.5 text-[13px] font-medium"
            style={{ color: 'var(--ink-muted)' }}
            data-testid="operations-routing-back"
          >
            <ArrowLeft size={13} /> Back to Operations
          </button>
        </div>
        <PropertiesPage />
      </div>
    );
  }

  // Checked BEFORE `cockpit`, because the pane's own Back goes to whichever
  // surface opened it — a tenant focused from this table returns to the
  // table, not into a property the user was never looking at.
  if (focusedTenant) {
    return (
      <div className="flex-1 overflow-y-auto px-12 pt-10 pb-24" data-testid="operations-page">
        <div className="w-full max-w-[900px] mx-auto">
          <TenantDetailPane
            tenantId={focusedTenant.id}
            label={focusedTenant.label}
            backLabel="Back to Operations"
            onBack={() => setFocusedTenant(null)}
          />
        </div>
      </div>
    );
  }

  if (cockpit) {
    return (
      <div className="flex-1 overflow-y-auto px-12 pt-10 pb-24" data-testid="operations-page">
        <div className="w-full max-w-[900px] mx-auto">
          <PropertyCockpit
            propertyId={cockpit.id}
            propertyName={cockpit.name}
            onBack={() => setCockpit(null)}
          />
        </div>
      </div>
    );
  }

  const loading =
    (tab === 'properties' && properties === null) ||
    (tab === 'tenants' && tenants === null) ||
    (tab === 'vendors' && vendors === null) ||
    (tab === 'team' && team === null);

  return (
    <div className="flex-1 overflow-y-auto px-12 pt-10 pb-24" data-testid="operations-page">
      <div className="w-full max-w-[1200px] mx-auto">
        <div className="flex items-end justify-between mb-1">
          <h1
            className="text-[20px] font-semibold tracking-tight"
            style={{ color: 'var(--ink)', fontFamily: 'var(--font-display)' }}
          >
            Operations
          </h1>
          <span className="flex-1" />
          {/* ⚠ **This button RESYNCS FROM THE WORKSPACE'S SYSTEM (`PULL`),
              not just the tab.** It used to clear this tab's cached rows and refetch them from our
              own mirror — so the grid redrew, every number was identical, and
              the "properties last synced 191h ago" line directly above it did
              not move. A refresh control sitting under a staleness warning
              that it cannot clear is worse than no control: the one thing the
              user came to do is the one thing it did not do (founder call,
              2026-09-08).

              ⚠ The mirror read still happens, and second — a resync that
              lands nothing new must still redraw, or a successful sync looks
              like a no-op. */}
          <RefreshButton
            onClick={() => void resync()}
            busy={
              resyncing ||
              (tab === 'properties' && properties === null) ||
              (tab === 'tenants' && tenants === null) ||
              (tab === 'vendors' && vendors === null) ||
              (tab === 'team' && team === null)
            }
            label={`from ${sourceName}`}
            testId="operations-refresh"
          />
          {/* The routing grid assigns Buildium staff, which no other system
              sends, so it has nothing to show a workspace on one. */}
          {tab === 'team' && isBuildium && (
            <button
              type="button"
              onClick={() => setShowRouting(true)}
              data-testid="operations-assignment-rules"
              className="inline-flex items-center gap-1.5 h-8 px-3 rounded-[var(--r-sm)] border text-[13px] font-medium"
              style={{
                borderColor: 'var(--line-strong)',
                background: 'var(--canvas)',
                color: 'var(--ink)',
              }}
            >
              <Settings2 size={13} strokeWidth={1.75} />
              Assignment rules
            </button>
          )}
        </div>
        <p className="text-[13px]" style={{ color: 'var(--ink-muted)' }}>
          Your portfolio's moving parts — sort any column to find who needs attention.
        </p>
        {/* ⚠ Every number on this page is a cached copy of Buildium, and until
            now the page never said so. Chat has carried an "as of" footer since
            migration 0057; the grid rendering the same rows carried nothing, so
            a stale sync looked identical to a fresh one. */}
        <DataFreshness
          className="mb-5"
          reloadKey={freshnessKey}
          onLoad={(f) => setSource({ pms: f.pms ?? null, name: f.pmsName ?? null })}
        />

        {/* Tab strip */}
        <div
          className="inline-flex items-center gap-1 mb-4 p-0.5 rounded-[var(--r-md)]"
          style={{ background: 'var(--canvas-elevated)' }}
          role="tablist"
        >
          {TABS.map((t) => {
            const active = tab === t.id;
            return (
              <button
                key={t.id}
                type="button"
                role="tab"
                aria-selected={active}
                data-testid={`operations-tab-${t.id}`}
                onClick={() => setTab(t.id)}
                className="h-7 px-3.5 rounded-[calc(var(--r-md)-2px)] text-[13px] font-medium transition-colors"
                style={{
                  background: active ? 'var(--canvas)' : 'transparent',
                  color: active ? 'var(--ink)' : 'var(--ink-muted)',
                  boxShadow: active ? 'var(--shadow-sm)' : undefined,
                }}
              >
                {t.label}
              </button>
            );
          })}
        </div>

        {loading && (
          <div className="text-[13px] py-8" style={{ color: 'var(--ink-subtle)' }}>
            Loading…
          </div>
        )}

        {!loading && tab === 'properties' && (
          <OpsTable
            testId="ops-properties"
            columns={propertyCols}
            rows={properties ?? []}
            rowKey={(r) => r.buildium_id}
            onRowClick={(r) => setCockpit({ id: r.buildium_id, name: r.name ?? `#${r.buildium_id}` })}
            defaultSort="delinquent"
            emptyText={
              isBuildium
                ? 'No properties in the mirror yet — connect Buildium and run a sync.'
                : `No properties synced from ${sourceName} yet. Use Refresh to pull them now.`
            }
          />
        )}
        {!loading && tab === 'tenants' && (
          <OpsTable
            testId="ops-tenants"
            columns={tenantCols}
            rows={Array.isArray(tenants) ? tenants : []}
            rowKey={(r) => `${r.tenant_buildium_id}-${r.lease_buildium_id}`}
            onRowClick={(r) =>
              setFocusedTenant({
                id: r.tenant_buildium_id,
                label: personName(r.first_name, r.last_name),
              })
            }
            defaultSort="balance"
            emptyText={
              tenants instanceof NotSyncedRows
                ? notSynced(tenants.pmsName, 'which tenant is on which lease')
                : 'No active leases in the mirror yet.'
            }
          />
        )}
        {!loading && tab === 'vendors' && (
          <OpsTable
            testId="ops-vendors"
            columns={vendorCols}
            rows={vendors ?? []}
            rowKey={(r) => r.buildium_id}
            defaultSort="spend"
            emptyText="No vendors in the mirror yet."
          />
        )}
        {!loading && tab === 'team' && (
          <OpsTable
            testId="ops-team"
            columns={teamCols}
            rows={Array.isArray(team) ? team : []}
            rowKey={(r) => r.buildium_user_id}
            defaultSort="open_wos"
            emptyText={
              team instanceof NotSyncedRows
                ? notSynced(team.pmsName, 'your staff list')
                : 'No Buildium staff synced yet — use Assignment rules → Re-sync.'
            }
          />
        )}

        {!loading && tab === 'properties' && properties?.some((r) => r.delinquent_total == null) && (
          <p className="text-[12px] mt-2" style={{ color: 'var(--ink-faint)' }}>
            {notSynced(properties[0]?.pms_name, 'rent balances')} Check each tenant's ledger there.
          </p>
        )}
        {!loading && tab === 'vendors' && vendors?.some((r) => r.spend_12m == null || r.total_work_orders == null) && (
          <p className="text-[12px] mt-2" style={{ color: 'var(--ink-faint)' }}>
            {notSynced(vendors[0]?.pms_name, 'bills or which vendor did each work order')}
          </p>
        )}
        {!loading && tab === 'vendors' && isBuildium && (
          <p className="text-[12px] mt-2" style={{ color: 'var(--ink-faint)' }}>
            Avg days to complete measures from when Occupella first saw the work order —
            Buildium doesn't expose a created date.
          </p>
        )}
      </div>
    </div>
  );
}
