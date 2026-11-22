/**
 * CrmPage — fixture-data honesty (2026-08-15 audit, trust liability).
 *
 * When the pipeline shown is the fixture preview (demo build, or the
 * backend CRM flag is dark), the page must SAY so with the example-data
 * banner; when the live API answers, no banner. The distinction comes
 * from the service (`loadCrmOverview().isDemo`), never inferred here.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

vi.mock('../services/crm', async (orig) => {
  const actual = await orig<typeof import('../services/crm')>();
  return {
    ...actual,
    loadCrmOverview: vi.fn(),
  };
});

import {
  CRM_STAGES,
  loadCrmOverview,
  type CrmLead,
  type CrmOverview,
} from '../services/crm';
import { CrmPage } from './CrmPage';

const overview: CrmOverview = {
  stages: CRM_STAGES.map((s) => ({ key: s.key, label: s.label, count: 0, recent: [] })),
  metrics: {
    new_this_month: 0,
    new_prev_month: 0,
    replies_this_month: 0,
    active: 0,
    leased: 0,
    total: 0,
  },
  cold_count: 0,
};

const loadMock = vi.mocked(loadCrmOverview);

beforeEach(() => {
  loadMock.mockReset();
});

describe('CrmPage example-data banner', () => {
  it('labels the fixture preview so sample leads cannot pass as real', async () => {
    loadMock.mockResolvedValue({ overview, isDemo: true });
    render(<CrmPage />);
    const banner = await screen.findByTestId('crm-demo-banner');
    expect(banner).toHaveTextContent(/example data/i);
    expect(banner).toHaveTextContent(/isn't enabled for your workspace yet/i);
    expect(banner).toHaveTextContent(/samples, not your pipeline/i);
  });

  it('renders no banner when the live API answered', async () => {
    loadMock.mockResolvedValue({ overview, isDemo: false });
    render(<CrmPage />);
    // Wait for the overview to land (the analytics band renders).
    await waitFor(() => {
      expect(screen.getByText('This month')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('crm-demo-banner')).toBeNull();
  });

  it('shows an error — never unlabeled fixtures — on a live fetch failure', async () => {
    loadMock.mockRejectedValue(new Error('backend unreachable'));
    render(<CrmPage />);
    await waitFor(() => {
      expect(screen.getByText(/couldn't load your pipeline/i)).toBeInTheDocument();
    });
    expect(screen.queryByTestId('crm-demo-banner')).toBeNull();
  });
});

/**
 * The board card renders what the lead actually carries.
 *
 * ⚠ It used to render three fields — avatar, unit, source — while `CrmLead`
 * carried nine, so the board could not say what a lead WANTED (`note`) or
 * which ones were waiting on the user (`needsYou`). Nothing was missing behind
 * it: the fixtures, the API and `crm_lead_messages` all carried this already,
 * and only the card did not read it. That is why the pipeline looked like a
 * mockup rather than a tool.
 *
 * A single assertion on the whole card would pass while any one field
 * regressed, so each is pinned on its own — and each is given a value the
 * OTHERS do not share, since a card that echoed one field into every slot
 * would satisfy a laxer test.
 */
describe('CrmPage lead card', () => {
  // ⚠ ANNOTATED, not inferred. vitest does not typecheck (gotcha 72), so an
  // unannotated fixture that `tsc` rejects still reports a green suite — which
  // is exactly what the first cut of this file did: 5 passed, and `reason` was
  // a bare string where `CrmMoveReason` is an object.
  const lead: CrmLead = {
    id: 'l1',
    name: 'Jordan Reyes',
    initials: 'JR',
    kind: 'renter',
    source: 'zillow',
    stage: 'new',
    status: 'active',
    unit: 'Maple Court 4B',
    note: 'Asked if 4B is still available.',
    reason: { kind: 'inquiry', text: 'The lead just arrived.', at: '8:02a' },
    needsYou: true,
    calendarLabel: 'Tour · Sat 2:00p',
    movedAt: '8:02a',
  };

  it('shows the ask, the needs-you flag, the tour and the auto-move', async () => {
    loadMock.mockResolvedValue({
      overview: {
        ...overview,
        stages: CRM_STAGES.map((s) => ({
          key: s.key,
          label: s.label,
          count: s.key === 'new' ? 1 : 0,
          recent: s.key === 'new' ? [lead] : [],
        })),
      },
      isDemo: true,
    });
    render(<CrmPage />);

    // The ask. Without it the board cannot say why any lead is on it.
    expect(await screen.findByTestId('crm-lead-note')).toHaveTextContent(
      'Asked if 4B is still available.',
    );
    // The product's whole promise: which of these is waiting on me.
    expect(screen.getByTestId('crm-lead-needs-you')).toBeTruthy();
    expect(screen.getByTestId('crm-lead-tour')).toHaveTextContent('Tour · Sat 2:00p');
    // ⚠ Occupella moved this card by itself. Saying so is the difference
    // between a board that reorganised overnight and one the user assumes
    // they misremembered.
    expect(screen.getByTestId('crm-lead-moved')).toHaveTextContent('8:02a');
    // And the name is still whole — the flag used to sit beside it and cost
    // the last few characters on a five-column board.
    expect(screen.getByText('Jordan Reyes')).toBeTruthy();
  });

  it('omits each marker when the lead does not carry it', async () => {
    loadMock.mockResolvedValue({
      overview: {
        ...overview,
        stages: CRM_STAGES.map((s) => ({
          key: s.key,
          label: s.label,
          count: s.key === 'new' ? 1 : 0,
          recent:
            s.key === 'new'
              ? [{ ...lead, note: '', needsYou: false, calendarLabel: null, movedAt: null }]
              : [],
        })),
      },
      isDemo: true,
    });
    render(<CrmPage />);

    // The other direction. A card that always shows "Needs you" is exactly as
    // useless as one that never does — it stops meaning anything.
    await screen.findByText('Jordan Reyes');
    expect(screen.queryByTestId('crm-lead-needs-you')).toBeNull();
    expect(screen.queryByTestId('crm-lead-note')).toBeNull();
    expect(screen.queryByTestId('crm-lead-tour')).toBeNull();
    expect(screen.queryByTestId('crm-lead-moved')).toBeNull();
  });
});
