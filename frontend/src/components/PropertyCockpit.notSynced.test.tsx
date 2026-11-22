/**
 * The property page for a system that sends no balances, payments or
 * lease-tenant links (Rentvine). The backend sends null for each; before
 * this, the Delinquent tile read "$0 · Everyone current" and the lists read
 * "No outstanding balances." / "No active tenants mirrored." — claims about
 * tenants the mirror knows nothing about.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

const fetchCockpitDetail = vi.fn();
vi.mock('../services/properties', () => ({
  fetchCockpitDetail: (...a: unknown[]) => fetchCockpitDetail(...a),
}));
vi.mock('./properties/RenewalsSection', () => ({ RenewalsSection: () => null }));
vi.mock('./properties/TurnoversSection', () => ({ TurnoversSection: () => null }));
vi.mock('./properties/UnitDetailPane', () => ({ UnitDetailPane: () => null }));
vi.mock('./properties/TenantDetailPane', () => ({ TenantDetailPane: () => null }));

import { PropertyCockpit } from './PropertyCockpit';

const SUMMARY = {
  buildium_id: 7,
  name: 'Garden Row',
  address_line1: '1 Garden Way',
  city: 'Boston',
  state: 'MA',
  unit_count: 4,
  occupied_count: 3,
  open_work_orders: 1,
  expiring_leases_90d: 0,
};

function show(detail: Record<string, unknown>) {
  fetchCockpitDetail.mockResolvedValue({
    rent_scheduled: 4000,
    open_work_order_list: [],
    expiring_lease_list: [],
    ...detail,
  });
  render(<PropertyCockpit propertyId={7} propertyName="Garden Row" onBack={() => {}} />);
}

describe('PropertyCockpit', () => {
  it('says what Rentvine does not send instead of "$0" and "Everyone current"', async () => {
    show({
      summary: { ...SUMMARY, delinquent_leases: null, delinquent_total: null },
      rent_collected_mtd: null,
      tenants: null,
      delinquency_list: null,
      pms_name: 'Rentvine',
    });
    await waitFor(() =>
      expect(
        screen.getAllByText("Occupella doesn't sync rent balances from Rentvine yet."),
      ).toHaveLength(2), // the tile and the Outstanding balances list
    );
    expect(screen.queryByText('Everyone current')).toBeNull();
    expect(screen.queryByText('$0')).toBeNull();
    expect(screen.queryByText('No outstanding balances.')).toBeNull();
    expect(
      screen.getByText("Occupella doesn't sync payments from Rentvine yet."),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Occupella doesn't sync which tenant is on which lease from Rentvine yet.",
      ),
    ).toBeInTheDocument();
  });

  it('shows unknown occupancy on its own, never as vacant', async () => {
    // 2026-10-02 sandbox: 100 of 120 units had unknown occupancy, and the
    // tile said "120 vacant" — the subtraction counted them as vacant.
    show({
      summary: {
        ...SUMMARY,
        unit_count: 120,
        occupied_count: 0,
        occupancy_unknown_count: 100,
        delinquent_leases: null,
        delinquent_total: null,
      },
      rent_collected_mtd: null,
      tenants: null,
      delinquency_list: null,
      pms_name: 'Rentvine',
    });
    await waitFor(() =>
      expect(screen.getByText('20 vacant · 100 occupancy unknown')).toBeInTheDocument(),
    );
    expect(screen.queryByText('120 vacant')).toBeNull();
    expect(screen.queryByText('Fully occupied')).toBeNull();
  });

  it('shows an unknown rent roll as unknown, never "$0"', async () => {
    // 2026-10-01: the rent roll was SUM(rent) coalesced to 0, so a property
    // whose leases carried no rent showed "Rent roll $0" (rule 14).
    show({
      summary: { ...SUMMARY, delinquent_leases: null, delinquent_total: null },
      rent_scheduled: null,
      rent_unknown_leases: 2,
      rent_collected_mtd: null,
      tenants: null,
      delinquency_list: null,
      pms_name: 'Rentvine',
    });
    await waitFor(() =>
      expect(screen.getByText('Rent not known for 2 active leases')).toBeInTheDocument(),
    );
    expect(screen.queryByText('$0')).toBeNull();
  });

  it('says how many leases a partly known rent roll leaves out', async () => {
    show({
      summary: { ...SUMMARY, delinquent_leases: null, delinquent_total: null },
      rent_scheduled: 1500,
      rent_unknown_leases: 1,
      rent_collected_mtd: null,
      tenants: null,
      delinquency_list: null,
      pms_name: 'Rentvine',
    });
    await waitFor(() => expect(screen.getByText('$1,500')).toBeInTheDocument());
    expect(screen.getByText('Rent not known for 1 active lease')).toBeInTheDocument();
  });

  it('still says everyone is current when Buildium says so', async () => {
    show({
      summary: { ...SUMMARY, delinquent_leases: 0, delinquent_total: 0 },
      rent_collected_mtd: 1200,
      tenants: [],
      delinquency_list: [],
      pms_name: 'Buildium',
    });
    await waitFor(() => expect(screen.getByText('Everyone current')).toBeInTheDocument());
    expect(screen.getByText('No outstanding balances.')).toBeInTheDocument();
    expect(screen.getByText('$1,200 collected this month')).toBeInTheDocument();
  });
});

// The not-yet-synced 404's sentence is the backend's, worded for this
// workspace's system (`not_synced_yet`). The page used to swap any message
// holding "backfill" for "Run a Buildium backfill from Settings → Connectors",
// which a Rentvine company read too.
describe('a property not synced yet', () => {
  it("shows the backend's sentence as it is, naming no other system", async () => {
    const sentence =
      "This property isn't in Occupella's copy of your Rentvine data yet. An admin can " +
      'run Sync now on the Rentvine card in Settings → Connectors; then reload.';
    fetchCockpitDetail.mockRejectedValue(new Error(sentence));
    render(<PropertyCockpit propertyId={7} propertyName="Garden Row" onBack={() => {}} />);
    await waitFor(() => expect(screen.getByText(sentence)).toBeInTheDocument());
    expect(screen.queryByText(/Buildium/)).toBeNull();
  });
});
