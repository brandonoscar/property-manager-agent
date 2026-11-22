/**
 * Demo-mode isolation + fixture tests.
 *
 * The two invariants that protect production:
 *   1. Demo fixtures are populated (so a viewer sees a living app).
 *   2. In demo mode, every MUTATING service call short-circuits and
 *      NEVER calls ``apiFetch`` — so a demo click can't write to a real
 *      table or hit the backend.
 *
 * We mock ``demoMode`` true and make ``apiFetch`` throw if it is ever
 * reached, so any demo write that leaked to the network fails the test.
 */

import { describe, expect, it, vi } from 'vitest';

vi.mock('./auth', async () => {
  const actual = await vi.importActual<typeof import('./auth')>('./auth');
  return { ...actual, demoMode: true };
});

vi.mock('./api', async () => {
  const actual = await vi.importActual<typeof import('./api')>('./api');
  return {
    ...actual,
    apiFetch: vi.fn(() => {
      throw new Error('apiFetch must never be called in demo mode');
    }),
  };
});

import {
  demoActivityPage,
  demoInboxRows,
  demoPropertiesGrid,
  demoStaffOptions,
} from './demoData';
import { executeInboxAction, submitInboxFreeform, replanInboxEvent } from './inbox';
import { setPropertyAssignment, syncBuildiumStaff } from './properties';

describe('demo fixtures are populated', () => {
  it('inbox rows carry action surfaces', () => {
    const rows = demoInboxRows();
    expect(rows.length).toBeGreaterThan(0);
    expect(rows[0].action_surface?.actions.length).toBeGreaterThan(0);
  });

  it('activity, properties, and staff fixtures are non-empty', () => {
    expect(demoActivityPage().rows.length).toBeGreaterThan(0);
    expect(demoPropertiesGrid().properties.length).toBeGreaterThan(0);
    expect(demoStaffOptions().length).toBeGreaterThan(0);
  });
});

describe('demo isolation — mutations never hit the network', () => {
  it('executeInboxAction returns a synthetic success without calling apiFetch', async () => {
    const resp = await executeInboxAction('demo-evt-1', 0, null);
    expect(resp.status).toBe('executed');
    expect(resp.result.summary).toMatch(/demo/i);
  });

  it('submitInboxFreeform and replanInboxEvent resolve as no-ops', async () => {
    await expect(submitInboxFreeform('demo-evt-1', 'note')).resolves.toBeUndefined();
    await expect(replanInboxEvent('demo-evt-1')).resolves.toBeUndefined();
  });

  it('setPropertyAssignment reflects locally without a PUT', async () => {
    const resp = await setPropertyAssignment(31, 'maintenance', 7);
    expect(resp.ok).toBe(true);
    expect(resp.assignment?.buildium_user_id).toBe(7);
  });

  it('syncBuildiumStaff is a no-op success', async () => {
    const resp = await syncBuildiumStaff();
    expect(resp.ok).toBe(true);
  });
});
