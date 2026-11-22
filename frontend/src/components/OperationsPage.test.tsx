/**
 * OperationsPage — the four-tab hub that replaced Properties.
 * Pins: lazy per-tab fetch, tab switching, the Assignment-rules door to
 * the old routing grid (Team tab only), and property-row → cockpit.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const fetchCockpitOverview = vi.fn();
const fetchTenantOps = vi.fn();
const fetchVendorOps = vi.fn();
const fetchTeamOps = vi.fn();

vi.mock('../services/properties', () => ({
  fetchCockpitOverview: (...a: unknown[]) => fetchCockpitOverview(...a),
}));
vi.mock('../services/operations', () => ({
  fetchTenantOps: (...a: unknown[]) => fetchTenantOps(...a),
  fetchVendorOps: (...a: unknown[]) => fetchVendorOps(...a),
  fetchTeamOps: (...a: unknown[]) => fetchTeamOps(...a),
}));
vi.mock('./PropertyCockpit', () => ({
  PropertyCockpit: ({ propertyName }: { propertyName: string }) => (
    <div data-testid="mock-cockpit">{propertyName}</div>
  ),
}));
vi.mock('./PropertiesPage', () => ({
  PropertiesPage: () => <div data-testid="mock-routing-grid" />,
}));

import { OperationsPage } from './OperationsPage';
import { NotSyncedRows } from '../lib/notSynced';

const PROPERTY = {
  buildium_id: 700,
  name: 'Maple Court',
  address_line1: '12 Maple St',
  city: 'Boston',
  state: 'MA',
  unit_count: 8,
  occupied_count: 6,
  open_work_orders: 2,
  expiring_leases_90d: 1,
  delinquent_leases: 1,
  delinquent_total: 350,
};

const VENDOR = {
  buildium_id: 41,
  name: 'Rapid Plumbing Co',
  category_name: 'Plumbing',
  is_active: true,
  phone: null,
  email: null,
  open_work_orders: 2,
  total_work_orders: 19,
  jobs_90d: 6,
  avg_days_to_complete: 2.4,
  spend_12m: 18240,
  bill_count_12m: 17,
  last_bill_date: '2026-08-02',
};

const MEMBER = {
  buildium_user_id: 77,
  first_name: 'Dana',
  last_name: 'Whitfield',
  email: 'dana@x.co',
  user_role_name: 'Property Manager',
  is_active: true,
  open_work_orders: 4,
  open_tasks: 7,
  completed_tasks_7d: 12,
};

beforeEach(() => {
  vi.clearAllMocks();
  fetchCockpitOverview.mockResolvedValue([PROPERTY]);
  fetchVendorOps.mockResolvedValue([VENDOR]);
  fetchTeamOps.mockResolvedValue([MEMBER]);
  fetchTenantOps.mockResolvedValue([]);
});

describe('OperationsPage', () => {
  it('opens on the Properties tab and only fetches that tab', async () => {
    render(<OperationsPage />);
    await waitFor(() => expect(screen.getByText('Maple Court')).toBeInTheDocument());
    expect(fetchCockpitOverview).toHaveBeenCalledTimes(1);
    expect(fetchVendorOps).not.toHaveBeenCalled();
    expect(fetchTeamOps).not.toHaveBeenCalled();
  });

  it('switches to Vendors and renders the analytics columns', async () => {
    render(<OperationsPage />);
    fireEvent.click(screen.getByTestId('operations-tab-vendors'));
    await waitFor(() => expect(screen.getByText('Rapid Plumbing Co')).toBeInTheDocument());
    expect(screen.getByText('2.4')).toBeInTheDocument(); // avg days to complete
    expect(screen.getByText('$18,240')).toBeInTheDocument(); // 12mo spend
  });

  it('shows Assignment rules only on the Team tab, opening the old grid', async () => {
    render(<OperationsPage />);
    expect(screen.queryByTestId('operations-assignment-rules')).toBeNull();
    fireEvent.click(screen.getByTestId('operations-tab-team'));
    await waitFor(() => expect(screen.getByText('Dana Whitfield')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('operations-assignment-rules'));
    expect(screen.getByTestId('mock-routing-grid')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('operations-routing-back'));
    expect(screen.getByTestId('operations-page')).toBeInTheDocument();
  });

  // ─── Totals row ────────────────────────────────────────────────────────
  // The page's own thesis is that the table IS the analytics; without a
  // footer the only way to get "how much is owed across the portfolio" was
  // to add sixty rows by eye. These pin the two halves that can go wrong:
  // the sums that must be right, and the columns that must stay BLANK.

  it('totals the properties table — and pools occupancy instead of adding ratios', async () => {
    fetchCockpitOverview.mockResolvedValue([
      PROPERTY,
      { ...PROPERTY, buildium_id: 701, name: 'Oak Row', unit_count: 2, occupied_count: 1, open_work_orders: 3, expiring_leases_90d: 4, delinquent_total: 150 },
    ]);
    render(<OperationsPage />);
    await waitFor(() => expect(screen.getByText('Oak Row')).toBeInTheDocument());

    const totals = screen.getByTestId('ops-properties-totals');
    expect(totals).toHaveTextContent('All 2');
    expect(totals).toHaveTextContent('5'); // open WOs: 2 + 3
    expect(totals).toHaveTextContent('$500'); // owed: 350 + 150
    // ⚠ 6/8 and 1/2 pooled is 7/10 — NOT 0.75 + 0.5. Summing the ratios
    // would print "1.25", a number with no meaning that still looks like an
    // answer, which is the whole reason `total` is a function per column.
    expect(totals).toHaveTextContent('7/10');
  });

  it('shows unknown occupancy beside the pooled count, never as vacant', async () => {
    fetchCockpitOverview.mockResolvedValue([
      { ...PROPERTY, occupancy_unknown_count: 0 },
      {
        ...PROPERTY,
        buildium_id: 702,
        name: 'Seed Court',
        unit_count: 120,
        occupied_count: 0,
        occupancy_unknown_count: 100,
      },
    ]);
    render(<OperationsPage />);
    await waitFor(() => expect(screen.getByText('Seed Court')).toBeInTheDocument());
    expect(screen.getByText('0/120 · 100 unknown')).toBeInTheDocument();
    expect(screen.getByText('6/8')).toBeInTheDocument(); // a fully known row reads as before
    const totals = screen.getByTestId('ops-properties-totals');
    expect(totals).toHaveTextContent('6/128 · 100 unknown');
  });

  it('does NOT total average days to complete — there is no weight to pool it by', async () => {
    // A correct portfolio average needs each vendor's completed-job count,
    // and the row carries jobs STARTED in a window. Averaging the averages
    // would over-weight a vendor who finished one job, so the cell stays
    // empty. A plausible wrong number is worse than none.
    fetchVendorOps.mockResolvedValue([
      VENDOR,
      { ...VENDOR, buildium_id: 42, name: 'Slow Plumbing', avg_days_to_complete: 9.6, open_work_orders: 1, jobs_90d: 2, spend_12m: 1000, bill_count_12m: 3 },
    ]);
    render(<OperationsPage />);
    fireEvent.click(screen.getByTestId('operations-tab-vendors'));
    await waitFor(() => expect(screen.getByText('Slow Plumbing')).toBeInTheDocument());

    const cells = [...screen.getByTestId('ops-vendors-totals').querySelectorAll('td')];
    const speed = cells[3]; // name · open · jobs90 · speed
    expect(speed.textContent).toBe('');
    // The columns either side DO total, so an empty cell here is a decision
    // rather than a footer that never renders anything.
    expect(cells[2].textContent).toBe('8'); // jobs 90d: 6 + 2
    expect(cells[4].textContent).toBe('$19,240'); // billed: 18,240 + 1,000
  });

  it('counts only what is OWED — a credit balance must not cancel arrears', async () => {
    // Someone who paid ahead reduces nothing. The cells hide credits too
    // ("—"), so a total that netted them off could not be reconciled against
    // the rows above it, and it would understate the debt.
    fetchTenantOps.mockResolvedValue([
      { tenant_buildium_id: 1, first_name: 'Ann', last_name: 'Diaz', email: null, phone: null, property_name: 'Maple Court', unit_number: '1', balance: 400, oldest_bucket: '31-60', rent: 1200, lease_to_date: '2027-01-01' },
      { tenant_buildium_id: 2, first_name: 'Ben', last_name: 'Cole', email: null, phone: null, property_name: 'Maple Court', unit_number: '2', balance: -250, oldest_bucket: null, rent: 900, lease_to_date: '2027-02-01' },
    ]);
    render(<OperationsPage />);
    fireEvent.click(screen.getByTestId('operations-tab-tenants'));
    await waitFor(() => expect(screen.getByText('Ben Cole')).toBeInTheDocument());

    const totals = screen.getByTestId('ops-tenants-totals');
    expect(totals).toHaveTextContent('$400'); // not $150
    expect(totals).toHaveTextContent('$2,100'); // rent roll: 1,200 + 900
  });

  it('totals the team workload', async () => {
    fetchTeamOps.mockResolvedValue([
      MEMBER,
      { ...MEMBER, buildium_user_id: 78, first_name: 'Sam', last_name: 'Reyes', open_work_orders: 1, open_tasks: 3, completed_tasks_7d: 5 },
    ]);
    render(<OperationsPage />);
    fireEvent.click(screen.getByTestId('operations-tab-team'));
    await waitFor(() => expect(screen.getByText('Sam Reyes')).toBeInTheDocument());

    const cells = [...screen.getByTestId('ops-team-totals').querySelectorAll('td')];
    expect(cells.slice(1).map((c) => c.textContent)).toEqual(['5', '10', '17']);
  });

  it('opens the cockpit when a property row is clicked', async () => {
    render(<OperationsPage />);
    await waitFor(() => expect(screen.getByText('Maple Court')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('ops-properties-row-700'));
    expect(screen.getByTestId('mock-cockpit')).toHaveTextContent('Maple Court');
  });
});

// ─── A system that doesn't send the data (Rentvine) ─────────────────────
// The backend sends null, never 0, for what the company's PMS doesn't sync.
// Each of these rendered as a fact before: "—" owed, an empty Tenants tab,
// "0" bills, "No Buildium staff synced yet".

describe('OperationsPage for a Rentvine company', () => {
  it('says balances are not synced instead of totalling them as nothing', async () => {
    // Two rows: the totals row only renders when there is something to add.
    const blind = { ...PROPERTY, delinquent_leases: null, delinquent_total: null, pms_name: 'Rentvine' };
    fetchCockpitOverview.mockResolvedValue([blind, { ...blind, buildium_id: 701, name: 'Oak Row' }]);
    render(<OperationsPage />);
    await waitFor(() => expect(screen.getByText('Maple Court')).toBeInTheDocument());
    expect(screen.getAllByText('not synced')).toHaveLength(2);
    expect(
      screen.getByText(/Occupella doesn't sync rent balances from Rentvine yet/),
    ).toBeInTheDocument();
    expect(screen.getByTestId('ops-properties-totals')).not.toHaveTextContent('$');
  });

  it('shows why the Tenants tab is empty', async () => {
    fetchTenantOps.mockResolvedValue(new NotSyncedRows('Rentvine'));
    render(<OperationsPage />);
    fireEvent.click(screen.getByTestId('operations-tab-tenants'));
    await waitFor(() =>
      expect(
        screen.getByText(
          "Occupella doesn't sync which tenant is on which lease from Rentvine yet.",
        ),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText('No active leases in the mirror yet.')).toBeNull();
  });

  it('shows why the Team tab is empty', async () => {
    fetchTeamOps.mockResolvedValue(new NotSyncedRows('Rentvine'));
    render(<OperationsPage />);
    fireEvent.click(screen.getByTestId('operations-tab-team'));
    await waitFor(() =>
      expect(
        screen.getByText("Occupella doesn't sync your staff list from Rentvine yet."),
      ).toBeInTheDocument(),
    );
  });

  it('leaves unknown vendor billing and work orders blank, not zero', async () => {
    const blind = {
      ...VENDOR,
      open_work_orders: null,
      total_work_orders: null,
      jobs_90d: null,
      avg_days_to_complete: null,
      spend_12m: null,
      bill_count_12m: null,
      pms_name: 'Rentvine',
    };
    fetchVendorOps.mockResolvedValue([blind, { ...blind, buildium_id: 42, name: 'Volt & Vine' }]);
    render(<OperationsPage />);
    fireEvent.click(screen.getByTestId('operations-tab-vendors'));
    await waitFor(() => expect(screen.getByText('Rapid Plumbing Co')).toBeInTheDocument());
    const cells = [...screen.getByTestId('ops-vendors-totals').querySelectorAll('td')];
    expect(cells.map((c) => c.textContent)).not.toContain('0');
    expect(
      screen.getByText(/doesn't sync bills or which vendor did each work order from Rentvine/),
    ).toBeInTheDocument();
  });
});
