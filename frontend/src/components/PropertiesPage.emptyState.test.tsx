/**
 * The properties grid is empty — WHY?
 *
 * THE DEFECT: three states render an identical empty table and need opposite
 * instructions, and this screen showed the same sentence to all three. A
 * customer who had connected Buildium AND synced it, whose account genuinely
 * holds no rental properties, was told to "Connect your Buildium account, then
 * sync" — an instruction they had already followed twice, that cannot change
 * anything, on a screen where the product is working exactly as designed.
 * Following it changes nothing, which is what makes it read as broken.
 *
 * The same three-way split is drawn server-side by `mirror_tools._not_synced`
 * for the agent, and the "has it ever synced" half is the same Buildium
 * `lastSyncAt` stamp the getting-started checklist ticks off — one fact, three
 * consumers, so they cannot tell a customer different things.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

// Demo mode defaults ON in the vitest env and short-circuits every fetch to
// in-memory fixtures — which would never reach the empty branch at all.
vi.mock('../services/auth', async () => {
  const actual = await vi.importActual<typeof import('../services/auth')>('../services/auth');
  return { ...actual, demoMode: false };
});

import { PropertiesPage } from './PropertiesPage';
import * as propertiesService from '../services/properties';
import * as connectorsService from '../services/connectors';

const EMPTY_GRID: propertiesService.PropertiesGridResponse = {
  default_row: {
    assignments: {
      maintenance: null,
      leasing: null,
      accounting: null,
      tenant_relations: null,
      other: null,
    },
  },
  properties: [],
  page: 1,
  page_size: 50,
  total: 0,
};

/** A Rentvine connector row, same contract as the Buildium one. */
function rentvineRow(over: Partial<connectorsService.Connector>): connectorsService.Connector {
  return {
    id: 'rentvine',
    name: 'Rentvine',
    category: 'property_management',
    description: '',
    capabilities: ['api'],
    authMethod: 'api_key',
    ...over,
  };
}

/** A Buildium connector row. `lastSyncAt` present ⇒ a sync has completed. */
function buildiumRow(over: Partial<connectorsService.Connector>): connectorsService.Connector {
  return {
    id: 'buildium',
    name: 'Buildium',
    category: 'property_management',
    description: '',
    capabilities: ['api'],
    authMethod: 'api_key',
    ...over,
  };
}

async function renderEmptyGrid(): Promise<HTMLElement> {
  render(<PropertiesPage />);
  return waitFor(() => screen.getByTestId('properties-empty'));
}

describe('PropertiesPage — the empty grid says WHY', () => {
  beforeEach(() => {
    vi.spyOn(propertiesService, 'fetchPropertiesGrid').mockResolvedValue(EMPTY_GRID);
    vi.spyOn(propertiesService, 'fetchStaffOptions').mockResolvedValue({ options: [] });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('tells a synced account with no properties that nothing is wrong', async () => {
    vi.spyOn(connectorsService, 'listConnectors').mockResolvedValue([
      buildiumRow({ apiConnected: true, lastSyncAt: 1_700_000_000_000 }),
    ]);

    const empty = await renderEmptyGrid();
    await waitFor(() => expect(empty.textContent).toMatch(/synced fine/i));
    // The two claims that were false for this customer.
    expect(empty.textContent).not.toMatch(/Connect your Buildium account/i);
    expect(empty.textContent).not.toMatch(/Run Sync data/i);
    // ⚠ And NO call to action: every button here would be a thing to click
    // that cannot help, which is the same defect wearing a nicer sentence.
    expect(screen.queryByText('Set up Buildium')).not.toBeInTheDocument();
    expect(screen.queryByText('Go to Buildium settings')).not.toBeInTheDocument();
  });

  it('tells a connected-but-never-synced account to run the sync', async () => {
    vi.spyOn(connectorsService, 'listConnectors').mockResolvedValue([
      buildiumRow({ apiConnected: true }),
    ]);

    const empty = await renderEmptyGrid();
    await waitFor(() => expect(empty.textContent).toMatch(/Run Sync data/i));
    expect(empty.textContent).not.toMatch(/Connect your Buildium account/i);
    expect(screen.getByText('Go to Buildium settings')).toBeInTheDocument();
  });

  it('still tells a disconnected account to connect', async () => {
    vi.spyOn(connectorsService, 'listConnectors').mockResolvedValue([
      buildiumRow({ apiConnected: false }),
    ]);

    const empty = await renderEmptyGrid();
    await waitFor(() => expect(empty.textContent).toMatch(/Connect your Buildium account/i));
    expect(screen.getByText('Set up Buildium')).toBeInTheDocument();
  });

  it('keeps the old wording when it cannot tell — a failed probe is not evidence', async () => {
    // ⚠ The direction that matters. A connectors call that 503s says nothing
    // about whether this customer owns properties, and "your account has none"
    // would be the fixed defect pointed the other way (gotcha 55).
    vi.spyOn(connectorsService, 'listConnectors').mockRejectedValue(new Error('503'));

    const empty = await renderEmptyGrid();
    await waitFor(() => expect(empty.textContent).toMatch(/Connect your Buildium account/i));
    expect(empty.textContent).not.toMatch(/synced fine/i);
  });

  // ── a Rentvine workspace ────────────────────────────────────────────────
  // It used to be told to "Connect your Buildium account", which one PMS per
  // workspace (migration 0100) forbids it to do.

  it('names Rentvine for a Rentvine workspace that synced and has no properties', async () => {
    vi.spyOn(connectorsService, 'listConnectors').mockResolvedValue([
      rentvineRow({ apiConnected: true, lastSyncAt: 1_700_000_000_000 }),
    ]);
    const empty = await renderEmptyGrid();
    await waitFor(() => expect(empty.textContent).toMatch(/Rentvine synced fine/));
    expect(empty.textContent).not.toMatch(/Buildium/);
  });

  it('points a connected, never-synced Rentvine workspace at its own sync', async () => {
    vi.spyOn(connectorsService, 'listConnectors').mockResolvedValue([
      rentvineRow({ apiConnected: true }),
    ]);
    const empty = await renderEmptyGrid();
    await waitFor(() => expect(empty.textContent).toMatch(/Sync now in the Rentvine settings/));
    expect(empty.textContent).not.toMatch(/Buildium|Sync data/);
    expect(screen.getByText('Go to Rentvine settings')).toBeInTheDocument();
  });

  it('asks a Rentvine workspace whose key stopped working to reconnect Rentvine', async () => {
    vi.spyOn(connectorsService, 'listConnectors').mockResolvedValue([
      rentvineRow({ apiConnected: false, needsReauth: true, lastSyncAt: 1_700_000_000_000 }),
    ]);
    const empty = await renderEmptyGrid();
    await waitFor(() => expect(empty.textContent).toMatch(/Connect your Rentvine account/));
    expect(screen.getByText('Set up Rentvine')).toBeInTheDocument();
  });

  it('names both systems for a workspace that has connected neither', async () => {
    vi.spyOn(connectorsService, 'listConnectors').mockResolvedValue([
      buildiumRow({ apiConnected: false }),
      rentvineRow({ apiConnected: false }),
    ]);
    const empty = await renderEmptyGrid();
    await waitFor(() =>
      expect(empty.textContent).toMatch(/Connect your Buildium or Rentvine account/),
    );
  });

  // ── a Rent Manager workspace ────────────────────────────────────────────
  // Its panel's button is Sync now; "Run Sync data" is Buildium's (rule 16).

  it('points a connected, never-synced Rent Manager workspace at its own Sync now', async () => {
    vi.spyOn(connectorsService, 'listConnectors').mockResolvedValue([
      rentvineRow({ id: 'rentmanager', name: 'Rent Manager', apiConnected: true }),
    ]);
    const empty = await renderEmptyGrid();
    await waitFor(() => expect(empty.textContent).toMatch(/Sync now in the Rent Manager settings/));
    expect(empty.textContent).not.toMatch(/Buildium|Rentvine|Sync data/);
    expect(screen.getByText('Go to Rent Manager settings')).toBeInTheDocument();
  });

  it('lists three systems a workspace can still choose from as a list', async () => {
    vi.spyOn(connectorsService, 'listConnectors').mockResolvedValue([
      buildiumRow({ apiConnected: false }),
      rentvineRow({ apiConnected: false }),
      rentvineRow({ id: 'rentmanager', name: 'Rent Manager', apiConnected: false }),
    ]);
    const empty = await renderEmptyGrid();
    await waitFor(() =>
      expect(empty.textContent).toMatch(/Connect your Buildium, Rentvine or Rent Manager account/),
    );
  });

  it('does not ask about connectors at all when the grid has rows', async () => {
    // The classify call is on the EMPTY path only, so the common load costs
    // no extra request.
    vi.spyOn(propertiesService, 'fetchPropertiesGrid').mockResolvedValue({
      ...EMPTY_GRID,
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
      total: 1,
    });
    const spy = vi.spyOn(connectorsService, 'listConnectors').mockResolvedValue([]);

    render(<PropertiesPage />);
    await waitFor(() => screen.getByTestId('property-row-100'));
    expect(spy).not.toHaveBeenCalled();
  });
});
