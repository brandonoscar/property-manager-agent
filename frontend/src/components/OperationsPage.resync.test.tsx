/**
 * Refresh on Operations pulls from BUILDIUM, not just from our own copy of it.
 *
 * ⚠ **The old button cleared this tab's cached rows and refetched the mirror.**
 * Every number came back identical, and the "properties last synced 191h ago"
 * caption sitting directly above it did not move — so the control read as
 * broken while working exactly as written. A refresh under a staleness warning
 * has to be able to clear that warning (founder call, 2026-09-08).
 *
 * ⚠ These are behavioural, not source greps, because the failure they guard
 * is a call that does not happen. A static check that `runBuildiumBackfill` is
 * IMPORTED would pass against a handler that never calls it (gotcha 62's
 * shape) — so each test drives the click and reads the spy.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const fetchCockpitOverview = vi.fn();
const runBuildiumBackfill = vi.fn();
const fetchMirrorFreshness = vi.fn();

vi.mock('../services/properties', () => ({
  fetchCockpitOverview: (...a: unknown[]) => fetchCockpitOverview(...a),
}));
vi.mock('../services/operations', () => ({
  fetchTenantOps: vi.fn().mockResolvedValue([]),
  fetchVendorOps: vi.fn().mockResolvedValue([]),
  fetchTeamOps: vi.fn().mockResolvedValue([]),
}));
vi.mock('../services/buildium', () => ({
  runBuildiumBackfill: (...a: unknown[]) => runBuildiumBackfill(...a),
}));
const runRentvineSync = vi.fn();
vi.mock('../services/rentvine', () => ({
  runRentvineSync: (...a: unknown[]) => runRentvineSync(...a),
}));
const runRentManagerSync = vi.fn();
vi.mock('../services/rentmanager', () => ({
  runRentManagerSync: (...a: unknown[]) => runRentManagerSync(...a),
}));
const runDoorLoopSync = vi.fn();
vi.mock('../services/doorloop', () => ({
  runDoorLoopSync: (...a: unknown[]) => runDoorLoopSync(...a),
}));
const runPropertywareSync = vi.fn();
vi.mock('../services/propertyware', () => ({
  runPropertywareSync: (...a: unknown[]) => runPropertywareSync(...a),
}));
vi.mock('../services/mirrorFreshness', () => ({
  fetchMirrorFreshness: (...a: unknown[]) => fetchMirrorFreshness(...a),
  freshnessLabel: () => 'Synced 7 days ago',
  stalenessNote: () => null,
}));
vi.mock('./PropertyCockpit', () => ({ PropertyCockpit: () => <div /> }));
vi.mock('./PropertiesPage', () => ({ PropertiesPage: () => <div /> }));

import { ApiError } from '../services/api';
import { PMS, type PmsId } from '../lib/pms';
import { useStore } from '../stores/store';
import { OperationsPage } from './OperationsPage';

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

const OK = {
  ok: true,
  properties: 1,
  units: 8,
  tenants: 6,
  leases: 6,
  balances: 1,
  transactions: 3,
  errors: [],
};

async function renderAndSettle(): Promise<void> {
  render(<OperationsPage />);
  await screen.findByText('Maple Court');
}

const refresh = () => screen.getByTestId('operations-refresh');

beforeEach(() => {
  vi.clearAllMocks();
  fetchCockpitOverview.mockResolvedValue([PROPERTY]);
  runBuildiumBackfill.mockResolvedValue(OK);
  fetchMirrorFreshness.mockResolvedValue({ asOf: '2026-09-01T00:00:00Z', stages: [] });
});

describe('the Refresh button', () => {
  it('asks Buildium for fresh data, then redraws the tab from the mirror', async () => {
    await renderAndSettle();
    expect(fetchCockpitOverview).toHaveBeenCalledTimes(1);

    fireEvent.click(refresh());

    await waitFor(() => expect(runBuildiumBackfill).toHaveBeenCalledTimes(1));
    // ⚠ BOTH halves. A resync with no redraw leaves the user looking at the
    // pre-sync rows, which is the same "did that do anything" gap in a new
    // place.
    await waitFor(() => expect(fetchCockpitOverview).toHaveBeenCalledTimes(2));
  });

  it('re-reads the staleness caption it was clicked to clear', async () => {
    await renderAndSettle();
    expect(fetchMirrorFreshness).toHaveBeenCalledTimes(1);

    fireEvent.click(refresh());

    // The caption fetches once on mount. Without a reload key the number the
    // user was watching stays frozen at the value that made them click.
    await waitFor(() => expect(fetchMirrorFreshness).toHaveBeenCalledTimes(2));
  });

  it('still redraws when Buildium refuses, and does not claim a sync it did not get', async () => {
    fetchMirrorFreshness.mockClear();
    runBuildiumBackfill.mockRejectedValue(new ApiError(403, 'Forbidden'));
    await renderAndSettle();
    const freshnessCallsBefore = fetchMirrorFreshness.mock.calls.length;

    fireEvent.click(refresh());

    // The rows the user asked for still arrive — the failure costs the
    // Buildium half, not the whole button.
    await waitFor(() => expect(fetchCockpitOverview).toHaveBeenCalledTimes(2));
    // ⚠ And the caption is NOT re-read: re-fetching it would redraw the same
    // stale stamp and read as "we tried and it is still 191h", when what
    // actually happened is that we never asked.
    expect(fetchMirrorFreshness).toHaveBeenCalledTimes(freshnessCallsBefore);
  });

  it('locks and spins for the whole Buildium pull, not just the mirror read', async () => {
    // ⚠ This replaced a "three clicks fire one sync" test that PASSED against
    // a mutant with the in-flight guard deleted — `disabled` had already
    // absorbed clicks 2 and 3, so the test could not see the guard it named
    // (gotcha 58). The disabled state is the property that is real, visible,
    // and load-bearing: a full account backfill takes seconds, and a button
    // that looks idle throughout is one people click again.
    let release: (v: unknown) => void = () => {};
    runBuildiumBackfill.mockReturnValue(new Promise((r) => (release = r)));
    await renderAndSettle();

    expect(refresh().hasAttribute('disabled')).toBe(false);
    fireEvent.click(refresh());
    await waitFor(() => expect(refresh().hasAttribute('disabled')).toBe(true));

    release(OK);
    await waitFor(() => expect(fetchCockpitOverview).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(refresh().hasAttribute('disabled')).toBe(false));
  });
});

// ─── a Rentvine workspace ──────────────────────────────────────────────
// The button used to call the Buildium backfill for every workspace, which
// answered a working Rentvine connection with "No saved credentials."

describe('the Refresh button on a Rentvine workspace', () => {
  const RENTVINE = { asOf: '2026-09-01T00:00:00Z', pms: 'rentvine', pmsName: 'Rentvine' };

  it('runs the Rentvine sync, not the Buildium backfill', async () => {
    fetchMirrorFreshness.mockResolvedValue(RENTVINE);
    runRentvineSync.mockResolvedValue({ ok: true, rows: { units: 37 }, stale: [] });
    await renderAndSettle();
    await waitFor(() => expect(refresh()).toHaveAttribute('aria-label', 'Refresh from Rentvine'));

    fireEvent.click(refresh());

    await waitFor(() => expect(runRentvineSync).toHaveBeenCalledTimes(1));
    expect(runBuildiumBackfill).not.toHaveBeenCalled();
    await waitFor(() => expect(fetchCockpitOverview).toHaveBeenCalledTimes(2));
  });

  it('names Rentvine when a manager is refused', async () => {
    fetchMirrorFreshness.mockResolvedValue(RENTVINE);
    runRentvineSync.mockRejectedValue(new ApiError(403, 'Forbidden'));
    const { toast } = await import('sonner');
    const message = vi.spyOn(toast, 'message');
    await renderAndSettle();
    await waitFor(() => expect(refresh()).toHaveAttribute('aria-label', 'Refresh from Rentvine'));

    fireEvent.click(refresh());

    await waitFor(() =>
      expect(message).toHaveBeenCalledWith(
        'Refreshed this page. Pulling fresh data from Rentvine needs an admin.',
      ),
    );
  });

  it('offers no Buildium assignment rules and no Buildium empty-state', async () => {
    fetchMirrorFreshness.mockResolvedValue(RENTVINE);
    fetchCockpitOverview.mockResolvedValue([]);
    render(<OperationsPage />);
    await screen.findByText('No properties synced from Rentvine yet. Use Refresh to pull them now.');
    fireEvent.click(screen.getByTestId('operations-tab-team'));
    await waitFor(() => expect(screen.queryByText('Loading…')).toBeNull());
    expect(screen.queryByTestId('operations-assignment-rules')).toBeNull();
  });

  it('keeps the Buildium behaviour when the system is unknown', async () => {
    fetchMirrorFreshness.mockResolvedValue({ asOf: '2026-09-01T00:00:00Z', pms: null, pmsName: null });
    await renderAndSettle();
    fireEvent.click(refresh());
    await waitFor(() => expect(runBuildiumBackfill).toHaveBeenCalledTimes(1));
    expect(runRentvineSync).not.toHaveBeenCalled();
  });
});

// ─── a Rent Manager or Propertyware workspace ──────────────────────────
// Before their connectors shipped, these workspaces got no pull at all, and
// before lib/pms.ts anything that was not Rentvine read as Buildium: the
// button called the Buildium backfill and the page offered Buildium staff
// routing.

describe('the Refresh button on a Rent Manager workspace', () => {
  const RENT_MANAGER = { asOf: null, pms: 'rentmanager', pmsName: 'Rent Manager' };

  it("runs the Rent Manager sync, not Buildium's or Rentvine's, and still redraws the tab", async () => {
    fetchMirrorFreshness.mockResolvedValue(RENT_MANAGER);
    runRentManagerSync.mockResolvedValue({ ok: true, rows: { units: 37 }, stale: [] });
    await renderAndSettle();
    await waitFor(() =>
      expect(refresh()).toHaveAttribute('aria-label', 'Refresh from Rent Manager'),
    );

    fireEvent.click(refresh());

    await waitFor(() => expect(runRentManagerSync).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(fetchCockpitOverview).toHaveBeenCalledTimes(2));
    expect(runBuildiumBackfill).not.toHaveBeenCalled();
    expect(runRentvineSync).not.toHaveBeenCalled();
    expect(runPropertywareSync).not.toHaveBeenCalled();
  });

  it('names Rent Manager in the empty state and offers Refresh, but no Buildium routing', async () => {
    fetchMirrorFreshness.mockResolvedValue(RENT_MANAGER);
    fetchCockpitOverview.mockResolvedValue([]);
    render(<OperationsPage />);
    await screen.findByText(
      'No properties synced from Rent Manager yet. Use Refresh to pull them now.',
    );
    fireEvent.click(screen.getByTestId('operations-tab-team'));
    await waitFor(() => expect(screen.queryByText('Loading…')).toBeNull());
    expect(screen.queryByTestId('operations-assignment-rules')).toBeNull();
  });

  // ⚠ The connector's sentences are written for Settings → Connectors ("Run
  // Test connection", "Sync now"), and neither button is on this page. A toast
  // naming a button the user cannot see breaks CLAUDE.md rule 16, so the toast
  // carries the way there. Driven through the real store, not a spy on it.
  it('shows the sync\'s own sentence with a way to the Connectors page', async () => {
    const sentence = 'Nothing came through from Rent Manager this time. Run Test connection.';
    fetchMirrorFreshness.mockResolvedValue(RENT_MANAGER);
    runRentManagerSync.mockResolvedValue({ ok: false, rows: {}, stale: ['units'], error: sentence });
    const { toast } = await import('sonner');
    const error = vi.spyOn(toast, 'error');
    await renderAndSettle();
    await waitFor(() =>
      expect(refresh()).toHaveAttribute('aria-label', 'Refresh from Rent Manager'),
    );
    useStore.setState({ view: 'properties', settingsSection: 'account' });
    const freshnessCallsBefore = fetchMirrorFreshness.mock.calls.length;

    fireEvent.click(refresh());

    await waitFor(() => expect(error).toHaveBeenCalledWith(sentence, expect.anything()));
    // The rows still redraw, and the caption is not re-read for a sync that
    // did not land (the Buildium refusal's rule above).
    await waitFor(() => expect(fetchCockpitOverview).toHaveBeenCalledTimes(2));
    expect(fetchMirrorFreshness).toHaveBeenCalledTimes(freshnessCallsBefore);
    const options = error.mock.calls.find((call) => call[0] === sentence)?.[1] as {
      action: { label: string; onClick: () => void };
    };
    expect(options.action.label).toBe('Open Connectors');
    options.action.onClick();
    expect(useStore.getState().view).toBe('settings');
    expect(useStore.getState().settingsSection).toBe('connectors');
  });

  it('names Rent Manager when a manager is refused', async () => {
    fetchMirrorFreshness.mockResolvedValue(RENT_MANAGER);
    runRentManagerSync.mockRejectedValue(new ApiError(403, 'Forbidden'));
    const { toast } = await import('sonner');
    const message = vi.spyOn(toast, 'message');
    await renderAndSettle();
    await waitFor(() =>
      expect(refresh()).toHaveAttribute('aria-label', 'Refresh from Rent Manager'),
    );

    fireEvent.click(refresh());

    await waitFor(() =>
      expect(message).toHaveBeenCalledWith(
        'Refreshed this page. Pulling fresh data from Rent Manager needs an admin.',
      ),
    );
  });

  it('reads an id this build does not know as Buildium, as before', async () => {
    fetchMirrorFreshness.mockResolvedValue({ asOf: null, pms: 'appfolio', pmsName: 'AppFolio' });
    await renderAndSettle();
    fireEvent.click(refresh());
    await waitFor(() => expect(runBuildiumBackfill).toHaveBeenCalledTimes(1));
  });
});

// ─── every system pulls from its own sync ──────────────────────────────
// Rule 15: lib/pms.ts names the systems, OperationsPage's PULL maps them. One
// row per system lib/pms.ts knows, so a fifth system, or two pulls swapped,
// fails here rather than syncing the wrong account.

describe('the Refresh button, for every system lib/pms.ts knows', () => {
  const SPIES: Record<PmsId, ReturnType<typeof vi.fn>> = {
    buildium: runBuildiumBackfill,
    rentvine: runRentvineSync,
    rentmanager: runRentManagerSync,
    propertyware: runPropertywareSync,
    doorloop: runDoorLoopSync,
  };

  it.each(Object.values(PMS))('$name pulls from its own sync and no other', async (system) => {
    fetchMirrorFreshness.mockResolvedValue({ asOf: null, pms: system.id, pmsName: system.name });
    for (const spy of Object.values(SPIES)) spy.mockResolvedValue({ ok: true, rows: {}, stale: [] });
    await renderAndSettle();
    await waitFor(() =>
      expect(refresh()).toHaveAttribute('aria-label', `Refresh from ${system.name}`),
    );

    fireEvent.click(refresh());

    await waitFor(() => expect(SPIES[system.id]).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(fetchCockpitOverview).toHaveBeenCalledTimes(2));
    for (const [id, spy] of Object.entries(SPIES)) {
      if (id !== system.id) expect(spy).not.toHaveBeenCalled();
    }
  });
});
