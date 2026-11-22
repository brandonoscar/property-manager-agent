/**
 * The CRM drill-downs must not render a failure as an empty pipeline.
 *
 * Both views under the CRM overview — the per-stage list and the going-cold
 * list — swallowed every fetch error into `setLeads([])`, so a 500, a timeout
 * or a dropped connection rendered as "No leads in this stage yet" and, on the
 * cold view, as a congratulation:
 *
 *     Nothing going cold — every lead's had recent activity. 🎉
 *
 * That sentence is wrong twice over. It is an assertion about the WHOLE
 * pipeline made by a query that read only the quiet end of it, so a brand-new
 * customer with zero leads was congratulated for an achievement they had not
 * made; and on a failed request it was a claim about data nobody had.
 *
 * The overview ABOVE these two views has always done this correctly —
 * `ErrorState` with a retry, and `loadCrmOverview` labelling demo data rather
 * than passing it off as live. The drill-downs were simply never told, which is
 * gotcha 25/67's fan-out: a rule documented and enforced at its first caller,
 * invisible to the second.
 *
 * ⚠ THE EMPTY-STATE ASSERTIONS ARE PINNED IN BOTH DIRECTIONS (gotchas 38/51).
 * A wording fix that only removes the congratulation would pass a one-sided
 * check while a view that renders nothing at all — or renders an error as
 * empty — would pass it too. So each case asserts what IS shown as well as what
 * is not.
 */

import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { CrmDisabledError } from '../services/crm';

const loadCrmOverview = vi.fn();
const loadStageLeads = vi.fn();
const loadColdLeads = vi.fn();

vi.mock('../services/crm', async () => {
  // Only the NETWORK calls are stubbed. `CrmDisabledError` and the pure
  // normalisers stay real, so a test cannot pass by agreeing with a stub about
  // what the error type is (gotcha 88's rule on partial mocks).
  const actual = await vi.importActual<typeof import('../services/crm')>('../services/crm');
  return {
    ...actual,
    loadCrmOverview: (...a: unknown[]) => loadCrmOverview(...a),
    loadStageLeads: (...a: unknown[]) => loadStageLeads(...a),
    loadColdLeads: (...a: unknown[]) => loadColdLeads(...a),
    listLeadMessages: vi.fn().mockResolvedValue([]),
    getLeadDraft: vi.fn().mockResolvedValue({ signals: [], draft_reply: null }),
  };
});

vi.mock('../services/sms', () => ({
  getSmsStatus: vi.fn().mockResolvedValue({ block: null, enabled: true, configured: true }),
}));

// The two views are module-private, so they are reached the way a user reaches
// them: render the page, then drive it. Importing after the mocks is required —
// `vi.mock` is hoisted but the module graph is not (gotcha 44).
const { CrmPage } = await import('./CrmPage');

// ⚠ EVERY FIELD IS STATED, and `metrics` is a real object rather than null.
// `MetricsBand` is rendered unconditionally and types `metrics` non-null, so a
// null there is a state no deployment is in — a fixture that does not say what
// it means is how a gap stays green (gotchas 67/72/73/96/107). The first cut
// used null and crashed the page on every case.
const OVERVIEW = {
  overview: {
    stages: [
      { key: 'new' as const, label: 'New', count: 3, recent: [] },
      { key: 'contacted' as const, label: 'Contacted', count: 0, recent: [] },
    ],
    metrics: {
      new_this_month: 3,
      new_prev_month: 2,
      replies_this_month: 1,
      active: 3,
      leased: 0,
      total: 3,
    },
    cold_count: 2,
  },
  isDemo: false,
};

/**
 * Open the "New" stage the way a user does.
 *
 * ⚠ `findByText('New')` is ambiguous — the label appears on the stage card AND
 * in a second `onOpenStage` affordance — and an ambiguous selector fails with
 * "found multiple elements", which reads like a broken test rather than a
 * broken query. The stage card is a BUTTON, so ask for the role.
 */
async function openStage(name = /New/) {
  const buttons = await screen.findAllByRole('button', { name });
  buttons[0].click();
}

beforeEach(() => {
  vi.clearAllMocks();
  loadCrmOverview.mockResolvedValue(OVERVIEW);
  loadStageLeads.mockResolvedValue({ leads: [], has_more: false, isDemo: false });
  loadColdLeads.mockResolvedValue({ leads: [], count: 0, threshold_days: 3, isDemo: false });
});

describe('the going-cold empty state', () => {
  it('states only what the response says, and never congratulates', async () => {
    // The distinguishing input is the one a brand-new customer produces: the
    // cold query legitimately returns nothing, and the pipeline behind it is
    // empty too. The old copy claimed every lead had recent activity, which is
    // a statement about leads this query never read.
    const { rerender } = render(<CrmPage />);
    await waitFor(() => expect(loadCrmOverview).toHaveBeenCalled());
    rerender(<CrmPage />);

    // Drive to the cold view via the overview's own affordance.
    const coldEntry = await screen.findByText(/going cold/i);
    coldEntry.click();

    const empty = await screen.findByText(/no leads have gone quiet/i);
    expect(empty).toBeTruthy();
    expect(screen.queryByText(/recent activity/i)).toBeNull();
    expect(screen.queryByText(/🎉/)).toBeNull();
  });

  it("names the server's own window rather than a number typed twice", async () => {
    // `threshold_days` rode on every response and was read by nothing, so both
    // sentences hardcoded "3". A backend change to the window would have left
    // the screen quietly wrong.
    loadColdLeads.mockResolvedValue({ leads: [], count: 0, threshold_days: 7, isDemo: false });

    render(<CrmPage />);
    const coldEntry = await screen.findByText(/going cold/i);
    coldEntry.click();

    const empty = await screen.findByText(/no leads have gone quiet/i);
    expect(empty.textContent).toContain('7');
    expect(empty.textContent).not.toContain('3+');
  });
});

describe('a failed drill-down fetch', () => {
  it('shows an error with a retry, not an empty list', async () => {
    loadColdLeads.mockRejectedValue(new Error('Could not load going-cold leads.'));

    render(<CrmPage />);
    const coldEntry = await screen.findByText(/going cold/i);
    coldEntry.click();

    expect(await screen.findByTestId('crm-cold-error')).toBeTruthy();
    // The other direction: the empty state must NOT be what a failure renders.
    expect(screen.queryByText(/no leads have gone quiet/i)).toBeNull();
  });

  it('retries on demand rather than leaving the customer stuck', async () => {
    loadColdLeads.mockRejectedValueOnce(new Error('boom'));
    loadColdLeads.mockResolvedValue({ leads: [], count: 0, threshold_days: 3, isDemo: false });

    render(<CrmPage />);
    (await screen.findByText(/going cold/i)).click();

    const retry = await screen.findByTestId('crm-cold-error-retry');
    retry.click();

    await waitFor(() => expect(screen.queryByTestId('crm-cold-error')).toBeNull());
    expect(await screen.findByText(/no leads have gone quiet/i)).toBeTruthy();
  });

  it('shows an error on the stage list too, not "no leads in this stage"', async () => {
    // Both views had the identical defect. A guard covering one of them reads
    // as covering the surface, which is how the second survived (gotcha 136).
    loadStageLeads.mockRejectedValue(new Error('Could not load leads.'));

    render(<CrmPage />);
    await openStage();

    expect(await screen.findByTestId('crm-stage-error')).toBeTruthy();
    expect(screen.queryByText(/no leads in this stage/i)).toBeNull();
  });
});

describe('a dark CRM flag', () => {
  it('shows the SAME fixture the overview already labelled, not an empty stage', async () => {
    // With the flag dark, `loadCrmOverview` returns the demo fixture and the
    // page banners it. The drill-downs hit the real endpoint, took a 404, and
    // rendered "No leads in this stage yet" — the page contradicting the counts
    // it had just displayed.
    loadCrmOverview.mockResolvedValue({ ...OVERVIEW, isDemo: true });
    loadStageLeads.mockResolvedValue({
      leads: [
        {
          id: 'demo-1',
          name: 'Sample Lead',
          phone: null,
          email: null,
          stage: 'new',
          status: 'active',
          source: 'sms',
          unit: null,
          note: null,
          initials: 'SL',
          kind: 'lead',
          last_activity_at: null,
          created_at: null,
        },
      ],
      has_more: false,
      isDemo: true,
    });

    render(<CrmPage />);
    await openStage();

    expect(await screen.findByText('Sample Lead')).toBeTruthy();
    expect(screen.queryByText(/no leads in this stage/i)).toBeNull();
    // The banner is what keeps this honest — fixture rows on screen with no
    // label is the trust liability `loadCrmOverview`'s docstring names.
    expect(await screen.findByText(/samples, not your pipeline/i)).toBeTruthy();
  });

  it('labels the COLD view fixture too, not only the stage list', async () => {
    // ⚠ A mutation found this hole: blanking `onSourceChange` on ColdListView
    // alone left every other assertion green, because the dark-flag case above
    // drives the STAGE view. Two views, two wirings — a guard covering one
    // reads as covering the surface (gotcha 136, and gotcha 122's
    // per-component rule).
    loadCrmOverview.mockResolvedValue({ ...OVERVIEW, isDemo: false });
    loadColdLeads.mockResolvedValue({
      leads: [],
      count: 0,
      threshold_days: 3,
      isDemo: true,
    });

    render(<CrmPage />);
    (await screen.findByText(/going cold/i)).click();

    expect(await screen.findByText(/samples, not your pipeline/i)).toBeTruthy();
  });

  it('is a real error type, not a string the test and the code agreed on', () => {
    // `CrmDisabledError` is imported unmocked above, so the loaders' branch and
    // this assertion read the same class (gotcha 28's both-sides rule).
    expect(new CrmDisabledError()).toBeInstanceOf(Error);
    expect(new CrmDisabledError().name).toBe('CrmDisabledError');
  });
});
