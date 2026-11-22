/**
 * PR #82 — visible save-state feedback on the Properties × Roles
 * dropdowns.
 *
 * Brandon's post-PR-#81 quote: "the properties dropdown assign
 * boxes work because there's no like saving then some kind of
 * loading that it's actually changing something." This file pins
 * the four states the cell can be in:
 *
 *  - idle (no pill)
 *  - Saving… (spinner while the PUT is in flight; dropdown disabled)
 *  - ✓ Saved (iris purple, auto-fades after 2 seconds)
 *  - Failed (with a separate error toast + rollback)
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';

// Production-path tests (real PUT against the spied service). Demo mode
// defaults ON in the vitest env and would short-circuit the PUT to a
// local fixture — pin demoMode false.
vi.mock('../services/auth', async () => {
  const actual = await vi.importActual<typeof import('../services/auth')>('../services/auth');
  return { ...actual, demoMode: false };
});

import { PropertiesPage } from './PropertiesPage';
import * as propertiesService from '../services/properties';

const baseGrid: propertiesService.PropertiesGridResponse = {
  default_row: {
    assignments: {
      maintenance: null,
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
        maintenance: null,
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

const baseStaff: propertiesService.StaffListResponse = {
  options: [
    {
      buildium_user_id: 1,
      display_name: 'Robert Friedman',
      email: 'r@example.com',
      role: 'Property Manager',
    },
  ],
};

describe('PropertiesPage — PR #82 saving state', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.spyOn(propertiesService, 'fetchPropertiesGrid').mockResolvedValue(baseGrid);
    vi.spyOn(propertiesService, 'fetchStaffOptions').mockResolvedValue(baseStaff);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('dropdown change shows Saving… while the PUT is in flight, then ✓ Saved', async () => {
    // Pending promise we resolve manually so the test can observe
    // the Saving state.
    let resolvePut: (value: propertiesService.AssignmentMutationResponse) => void = () => {};
    const putPending = new Promise<propertiesService.AssignmentMutationResponse>((resolve) => {
      resolvePut = resolve;
    });
    vi.spyOn(propertiesService, 'setPropertyAssignment').mockReturnValue(putPending);

    render(<PropertiesPage />);
    await waitFor(() => screen.getByTestId('property-row-100'));

    const row = screen.getByTestId('property-row-100');
    const dropdown = row.querySelectorAll(
      '[data-testid="assign-to-user-cell"]',
    )[0] as HTMLSelectElement;
    fireEvent.change(dropdown, { target: { value: '1' } });

    // Saving pill renders synchronously alongside the optimistic
    // update — the row carries exactly one pill in state=saving.
    const savingPill = await screen.findByTestId('saving-pill');
    expect(savingPill.getAttribute('data-state')).toBe('saving');
    expect(savingPill.textContent).toContain('Saving');

    // Resolve the PUT — pill flips to ``saved``.
    await act(async () => {
      resolvePut({
        ok: true,
        cleared: false,
        assignment: { action_type: 'assign_to_user', buildium_user_id: 1 },
      });
    });
    await waitFor(() => {
      expect(screen.getByTestId('saving-pill').getAttribute('data-state')).toBe(
        'saved',
      );
    });
    expect(screen.getByTestId('saving-pill').textContent).toContain('Saved');
  });

  it('dropdown is disabled while saving', async () => {
    // Same setup — long-running PUT.
    let resolvePut: (value: propertiesService.AssignmentMutationResponse) => void = () => {};
    vi.spyOn(propertiesService, 'setPropertyAssignment').mockReturnValue(
      new Promise((resolve) => {
        resolvePut = resolve;
      }),
    );

    render(<PropertiesPage />);
    await waitFor(() => screen.getByTestId('property-row-100'));

    const row = screen.getByTestId('property-row-100');
    const dropdown = row.querySelectorAll(
      '[data-testid="assign-to-user-cell"]',
    )[0] as HTMLSelectElement;
    fireEvent.change(dropdown, { target: { value: '1' } });

    // While in flight the underlying <select> is disabled.
    await waitFor(() => screen.getByTestId('saving-pill'));
    expect(dropdown.disabled).toBe(true);

    // Resolve to clean up the timer dependency.
    await act(async () => {
      resolvePut({
        ok: true,
        cleared: false,
        assignment: { action_type: 'assign_to_user', buildium_user_id: 1 },
      });
    });
  });

  it('failed PUT shows the Failed pill, rolls back, and surfaces the toast', async () => {
    vi.spyOn(propertiesService, 'setPropertyAssignment').mockRejectedValue(
      new Error('500 internal'),
    );

    render(<PropertiesPage />);
    await waitFor(() => screen.getByTestId('property-row-100'));

    const row = screen.getByTestId('property-row-100');
    const dropdown = row.querySelectorAll(
      '[data-testid="assign-to-user-cell"]',
    )[0] as HTMLSelectElement;
    fireEvent.change(dropdown, { target: { value: '1' } });

    // Pill ends in ``failed`` state.
    await waitFor(() => {
      const pill = screen.queryByTestId('saving-pill');
      expect(pill?.getAttribute('data-state')).toBe('failed');
    });
    expect(screen.getByTestId('saving-pill').textContent).toContain('Failed');

    // The dropdown value rolled back to the (Unassigned …) sentinel.
    expect(dropdown.value).toBe('__unassigned__');

    // Existing error toast still fires.
    await waitFor(() => screen.getByTestId('properties-toast'));
    expect(screen.getByTestId('properties-toast').textContent).toContain('500 internal');
  });

  it('Saved pill fades after 2 seconds', async () => {
    // FLAKE FIX — this fixture runs fake timers with
    // ``shouldAdvanceTime: true``, so the 2s fade timer ALSO advances
    // with real time. With an auto-resolving PUT mock, ``waitFor(state
    // === 'saved')`` raced the fade under full-suite load: the pill
    // could fade before the poll ever observed 'saved', failing with
    // "unable to find saving-pill" (never reproducible in isolation).
    //
    // Deterministic shape instead: hold the PUT open (same manual-
    // resolve pattern as the tests above). While it's pending the pill
    // is pinned at 'saving' and NO fade timer exists, so nothing can
    // race. Resolving then immediately advancing past 2s pins exactly
    // one contract here — the fade. (The saving→saved transition is
    // already pinned by the first test in this file.)
    let resolvePut: (value: propertiesService.AssignmentMutationResponse) => void = () => {};
    vi.spyOn(propertiesService, 'setPropertyAssignment').mockReturnValue(
      new Promise((resolve) => {
        resolvePut = resolve;
      }),
    );

    render(<PropertiesPage />);
    await waitFor(() => screen.getByTestId('property-row-100'));

    const row = screen.getByTestId('property-row-100');
    const dropdown = row.querySelectorAll(
      '[data-testid="assign-to-user-cell"]',
    )[0] as HTMLSelectElement;
    fireEvent.change(dropdown, { target: { value: '1' } });

    // Pill appears and stays 'saving' for as long as we hold the PUT.
    const pill = await screen.findByTestId('saving-pill');
    expect(pill.getAttribute('data-state')).toBe('saving');

    // Release the PUT → 'saved' + the 2s fade timer arms.
    await act(async () => {
      resolvePut({
        ok: true,
        cleared: false,
        assignment: { action_type: 'assign_to_user', buildium_user_id: 1 },
      });
    });

    // Advance past the fade — the pill must be gone.
    await act(async () => {
      vi.advanceTimersByTime(2500);
    });
    await waitFor(() => {
      expect(screen.queryByTestId('saving-pill')).toBeNull();
    });
  });
});
