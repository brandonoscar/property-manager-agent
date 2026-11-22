/**
 * The unit and tenant panes for a system that sends no balances, tasks or
 * lease-tenant links (Rentvine). A missing balance row used to read "No
 * outstanding balance on this lease." whatever the reason it was missing.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

const fetchUnitDetail = vi.fn();
const fetchTenantDetail = vi.fn();
vi.mock('../../services/properties', () => ({
  fetchUnitDetail: (...a: unknown[]) => fetchUnitDetail(...a),
  fetchTenantDetail: (...a: unknown[]) => fetchTenantDetail(...a),
}));

import { TenantDetailPane } from './TenantDetailPane';
import { UnitDetailPane } from './UnitDetailPane';

const UNIT = {
  unit: {
    buildium_id: 40,
    unit_number: '1A',
    market_rent: 1500,
    is_occupied: true,
    property_id: 7,
    property_name: 'Garden Row',
  },
  current_lease: { buildium_id: 31, status: 'Active', lease_from_date: null, lease_to_date: null, rent: 1500 },
  open_work_orders: [],
  recent_work_orders: [],
};

describe('UnitDetailPane', () => {
  it('does not say "no outstanding balance" when balances are not synced', async () => {
    fetchUnitDetail.mockResolvedValue({
      ...UNIT,
      tenants: null,
      balance: null,
      balances_sent: false,
      open_tasks: null,
      pms_name: 'Rentvine',
    });
    render(<UnitDetailPane unitId={40} label="Unit 1A" onBack={() => {}} />);
    await waitFor(() =>
      expect(
        screen.getByText("Occupella doesn't sync rent balances from Rentvine yet."),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText('No outstanding balance on this lease.')).toBeNull();
    expect(screen.getByText("Occupella doesn't sync tasks from Rentvine yet.")).toBeInTheDocument();
    expect(
      screen.getByText("Occupella doesn't sync which tenant is on which lease from Rentvine yet."),
    ).toBeInTheDocument();
  });

  it('keeps "no outstanding balance" when the system sends balances', async () => {
    fetchUnitDetail.mockResolvedValue({
      ...UNIT,
      tenants: [],
      balance: null,
      balances_sent: true,
      open_tasks: [],
      pms_name: 'Buildium',
    });
    render(<UnitDetailPane unitId={40} label="Unit 1A" onBack={() => {}} />);
    await waitFor(() =>
      expect(screen.getByText('No outstanding balance on this lease.')).toBeInTheDocument(),
    );
  });
});

describe('UnitDetailPane occupancy', () => {
  it('says occupancy is unknown, not vacant, when the backend sends null', async () => {
    fetchUnitDetail.mockResolvedValue({
      ...UNIT,
      unit: { ...UNIT.unit, is_occupied: null },
      tenants: null,
      balance: null,
      balances_sent: false,
      open_tasks: null,
      pms_name: 'Rentvine',
    });
    render(<UnitDetailPane unitId={40} label="Unit 1A" onBack={() => {}} />);
    await waitFor(() => expect(screen.getByText('Occupancy unknown')).toBeInTheDocument());
    expect(screen.queryByText('Vacant')).toBeNull();
  });

  it('still says vacant when the system says so', async () => {
    fetchUnitDetail.mockResolvedValue({
      ...UNIT,
      unit: { ...UNIT.unit, is_occupied: false },
      tenants: [],
      balance: null,
      balances_sent: true,
      open_tasks: [],
      pms_name: 'Buildium',
    });
    render(<UnitDetailPane unitId={40} label="Unit 1A" onBack={() => {}} />);
    await waitFor(() => expect(screen.getByText('Vacant')).toBeInTheDocument());
  });
});

describe('TenantDetailPane', () => {
  it('says the lists are not synced instead of empty', async () => {
    fetchTenantDetail.mockResolvedValue({
      tenant: { buildium_id: 9, first_name: 'Ana', last_name: 'Diaz', email: null, phone: null },
      leases: null,
      recent_transactions: null,
      open_work_orders: null,
      balances_sent: false,
      pms_name: 'Rentvine',
    });
    render(<TenantDetailPane tenantId={9} label="Ana Diaz" onBack={() => {}} />);
    await waitFor(() =>
      expect(
        screen.getAllByText(
          "Occupella doesn't sync which tenant is on which lease from Rentvine yet.",
        ),
      ).toHaveLength(3),
    );
    expect(screen.queryByText('No leases mirrored for this tenant.')).toBeNull();
  });
});

// The not-yet-synced 404's sentence is the backend's (`not_synced_yet`): each
// pane used to replace it with "Run a Buildium backfill", whatever the system.
describe('a unit or tenant not synced yet', () => {
  const sentence = (record: string) =>
    `This ${record} isn't in Occupella's copy of your Rent Manager data yet. An admin can ` +
    'run Sync now on the Rent Manager card in Settings → Connectors; then reload.';

  it("UnitDetailPane shows the backend's sentence, naming no other system", async () => {
    fetchUnitDetail.mockRejectedValue(new Error(sentence('unit')));
    render(<UnitDetailPane unitId={40} label="Unit 1A" onBack={() => {}} />);
    await waitFor(() => expect(screen.getByText(sentence('unit'))).toBeInTheDocument());
    expect(screen.queryByText(/Buildium/)).toBeNull();
  });

  it("TenantDetailPane shows the backend's sentence, naming no other system", async () => {
    fetchTenantDetail.mockRejectedValue(new Error(sentence('tenant')));
    render(<TenantDetailPane tenantId={9} label="Ana Diaz" onBack={() => {}} />);
    await waitFor(() => expect(screen.getByText(sentence('tenant'))).toBeInTheDocument());
    expect(screen.queryByText(/Buildium/)).toBeNull();
  });
});
