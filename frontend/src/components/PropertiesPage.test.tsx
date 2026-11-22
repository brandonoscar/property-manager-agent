/**
 * PR #81 — Properties × Roles grid page tests.
 *
 * Covers the user-facing contract:
 *   - Header + Re-sync button render.
 *   - Default row + property rows render with 5 dropdowns each.
 *   - Dropdown options come from /properties/staff.
 *   - Changing a cell calls PUT with the right body.
 *   - Re-sync button calls POST /properties/sync.
 *   - "(Unassigned …)" option clears the cell (sends null).
 *
 * Mocks the services module so jsdom doesn't try to hit a real
 * backend; assertions verify the calls AND the rendered state.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

// These tests exercise the PRODUCTION data path (real fetch + PUT
// against the spied service). Demo mode defaults ON in the vitest env,
// which would short-circuit those calls to in-memory fixtures — so pin
// demoMode false here.
vi.mock('../services/auth', async () => {
  const actual = await vi.importActual<typeof import('../services/auth')>('../services/auth');
  return { ...actual, demoMode: false };
});

import { PropertiesPage } from './PropertiesPage';
import * as propertiesService from '../services/properties';
import * as buildiumService from '../services/buildium';

const grid: propertiesService.PropertiesGridResponse = {
  default_row: {
    assignments: {
      maintenance: { action_type: 'assign_to_user', buildium_user_id: 1 },
      leasing: null,
      accounting: null,
      tenant_relations: null,
      other: null,
    },
  },
  properties: [
    {
      buildium_property_id: 100,
      name: '150 Main Ave',
      address: 'Cleveland, OH',
      assignments: {
        maintenance: { action_type: 'assign_to_user', buildium_user_id: 2 },
        leasing: null,
        accounting: null,
        tenant_relations: null,
        other: null,
      },
    },
  ],
  page: 1,
  page_size: 50,
  total: 1,
};

const staff: propertiesService.StaffListResponse = {
  options: [
    {
      buildium_user_id: 1,
      display_name: 'Robert Friedman',
      email: 'r@example.com',
      role: 'Property Manager',
    },
    {
      buildium_user_id: 2,
      display_name: 'Mei Tan',
      email: 'm@example.com',
      role: 'Property Manager',
    },
  ],
};

describe('PropertiesPage', () => {
  beforeEach(() => {
    vi.spyOn(propertiesService, 'fetchPropertiesGrid').mockResolvedValue(grid);
    vi.spyOn(propertiesService, 'fetchStaffOptions').mockResolvedValue(staff);
    vi.spyOn(propertiesService, 'setPropertyAssignment').mockResolvedValue({
      ok: true,
      cleared: false,
      assignment: { action_type: 'assign_to_user', buildium_user_id: 1 },
    });
    vi.spyOn(propertiesService, 'setDefaultAssignment').mockResolvedValue({
      ok: true,
      cleared: false,
      assignment: { action_type: 'assign_to_user', buildium_user_id: 1 },
    });
    vi.spyOn(propertiesService, 'syncBuildiumStaff').mockResolvedValue({
      ok: true,
      upserted: 2,
      deactivated: 0,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the header + Re-sync button', async () => {
    render(<PropertiesPage />);
    await waitFor(() =>
      expect(propertiesService.fetchPropertiesGrid).toHaveBeenCalled(),
    );
    expect(screen.getByText('Properties × Roles')).toBeInTheDocument();
    expect(screen.getByTestId('resync-button')).toBeInTheDocument();
  });

  it('renders the (no property) default row + property row', async () => {
    render(<PropertiesPage />);
    await waitFor(() => screen.getByTestId('default-row'));
    expect(screen.getByTestId('default-row')).toBeInTheDocument();
    expect(screen.getByTestId('property-row-100')).toBeInTheDocument();
    expect(screen.getByText('150 Main Ave')).toBeInTheDocument();
  });

  it('renders 5 dropdowns per row (5 default + 5 per property)', async () => {
    render(<PropertiesPage />);
    await waitFor(() => screen.getByTestId('default-row'));
    const dropdowns = screen.getAllByTestId('assign-to-user-cell');
    // 5 categories × (1 default row + 1 property row) = 10 dropdowns.
    expect(dropdowns).toHaveLength(10);
  });

  it('populates dropdown options from /properties/staff', async () => {
    render(<PropertiesPage />);
    await waitFor(() => screen.getByTestId('default-row'));
    const firstDropdown = screen.getAllByTestId('assign-to-user-cell')[0] as HTMLSelectElement;
    // The "(Unassigned …)" sentinel is always present plus the two
    // staff options from the mock.
    const optionLabels = Array.from(firstDropdown.options).map((o) => o.textContent);
    expect(optionLabels.some((l) => l?.startsWith('Unassigned'))).toBe(true);
    expect(optionLabels.some((l) => l?.includes('Robert Friedman'))).toBe(true);
    expect(optionLabels.some((l) => l?.includes('Mei Tan'))).toBe(true);
  });

  it('PUTs a new assignment when the user picks someone', async () => {
    render(<PropertiesPage />);
    await waitFor(() => screen.getByTestId('property-row-100'));

    const propertyRow = screen.getByTestId('property-row-100');
    const dropdowns = propertyRow.querySelectorAll('[data-testid="assign-to-user-cell"]');
    // First dropdown in the property row = "maintenance" category.
    const maintenanceDropdown = dropdowns[0] as HTMLSelectElement;
    fireEvent.change(maintenanceDropdown, { target: { value: '1' } });

    await waitFor(() => {
      expect(propertiesService.setPropertyAssignment).toHaveBeenCalled();
    });
    const call = (propertiesService.setPropertyAssignment as ReturnType<typeof vi.fn>).mock
      .calls[0];
    expect(call[0]).toBe(100); // property id
    expect(call[1]).toBe('maintenance'); // category
    expect(call[2]).toBe(1); // buildium_user_id
  });

  it('clears the cell (sends null) when the user picks (Unassigned …)', async () => {
    render(<PropertiesPage />);
    await waitFor(() => screen.getByTestId('property-row-100'));

    const propertyRow = screen.getByTestId('property-row-100');
    const dropdowns = propertyRow.querySelectorAll('[data-testid="assign-to-user-cell"]');
    const maintenanceDropdown = dropdowns[0] as HTMLSelectElement;
    fireEvent.change(maintenanceDropdown, { target: { value: '__unassigned__' } });

    await waitFor(() => {
      expect(propertiesService.setPropertyAssignment).toHaveBeenCalled();
    });
    const call = (propertiesService.setPropertyAssignment as ReturnType<typeof vi.fn>).mock
      .calls[0];
    expect(call[2]).toBeNull();
  });

  it('default-row PUT goes through setDefaultAssignment', async () => {
    render(<PropertiesPage />);
    await waitFor(() => screen.getByTestId('default-row'));

    const defaultRow = screen.getByTestId('default-row');
    const dropdowns = defaultRow.querySelectorAll('[data-testid="assign-to-user-cell"]');
    const leasingDropdown = dropdowns[1] as HTMLSelectElement;
    fireEvent.change(leasingDropdown, { target: { value: '2' } });

    await waitFor(() => {
      expect(propertiesService.setDefaultAssignment).toHaveBeenCalled();
    });
    const call = (propertiesService.setDefaultAssignment as ReturnType<typeof vi.fn>).mock
      .calls[0];
    expect(call[0]).toBe('leasing');
    expect(call[1]).toBe(2);
  });

  it('clicking Re-sync runs staff sync AND the full mirror backfill', async () => {
    // The button label promises Buildium data — staff-only left the
    // properties/units/leases mirror stale (2026-07-25 dogfood).
    const backfill = vi.spyOn(buildiumService, 'runBuildiumBackfill').mockResolvedValueOnce({
      ok: true,
      properties: 5,
      units: 13,
      tenants: 11,
      leases: 19,
      balances: 2,
      transactions: 30,
      errors: [],
    });
    render(<PropertiesPage />);
    await waitFor(() => screen.getByTestId('resync-button'));
    fireEvent.click(screen.getByTestId('resync-button'));
    await waitFor(() => {
      expect(propertiesService.syncBuildiumStaff).toHaveBeenCalled();
    });
    expect(backfill).toHaveBeenCalled();
    await waitFor(() => {
      expect(screen.getByTestId('properties-toast')).toBeInTheDocument();
    });
    const toast = screen.getByTestId('properties-toast').textContent ?? '';
    expect(toast).toContain('2 staff');
    expect(toast).toContain('19 leases');
  });

  it('shows the permission-error message from the sync response', async () => {
    vi.spyOn(propertiesService, 'syncBuildiumStaff').mockResolvedValueOnce({
      ok: false,
      error:
        "Buildium denied access to the staff list. Make sure the Buildium API key has the 'Administration > Users — View' permission enabled.",
    });
    render(<PropertiesPage />);
    await waitFor(() => screen.getByTestId('resync-button'));
    fireEvent.click(screen.getByTestId('resync-button'));
    await waitFor(() => {
      expect(screen.getByTestId('properties-toast')).toBeInTheDocument();
    });
    expect(screen.getByTestId('properties-toast').textContent).toContain(
      'Administration > Users — View',
    );
  });

  it('prompts the user to sync when the staff cache is empty', async () => {
    vi.spyOn(propertiesService, 'fetchStaffOptions').mockResolvedValueOnce({
      options: [],
    });
    render(<PropertiesPage />);
    await waitFor(() => {
      expect(screen.getByText(/No Buildium staff/)).toBeInTheDocument();
    });
  });
});
