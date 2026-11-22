import { describe, it, expect } from 'vitest';

import {
  demoCrmBoard,
  demoCrmOverview,
  demoLeadDraft,
  demoLeadMessages,
  demoStageLeads,
} from './demoData';
import {
  CRM_MOVE_RULES,
  CRM_STAGES,
  normalizeLead,
  tourChipLabel,
  type CrmStage,
} from './crm';

const STAGE_KEYS: CrmStage[] = CRM_STAGES.map((s) => s.key);

describe('CRM stage metadata', () => {
  it('is the five-stage pipeline in order, with Scheduled (not Toured)', () => {
    expect(STAGE_KEYS).toEqual(['new', 'contacted', 'scheduled', 'applied', 'leased']);
  });

  it('every move rule targets a real stage', () => {
    for (const rule of CRM_MOVE_RULES) {
      expect(STAGE_KEYS).toContain(rule.to);
    }
  });
});

describe('demoCrmBoard', () => {
  const board = demoCrmBoard();

  it('is marked not-yet-live (Coming soon)', () => {
    expect(board.enabled).toBe(false);
  });

  it('has leads spread across the pipeline with valid stages', () => {
    expect(board.leads.length).toBeGreaterThanOrEqual(5);
    for (const lead of board.leads) {
      expect(STAGE_KEYS).toContain(lead.stage);
      expect(['renter', 'owner']).toContain(lead.kind);
      expect(lead.name).toBeTruthy();
      expect(lead.reason.text).toBeTruthy();
    }
  });

  it('covers every stage at least once (the board is not lopsided)', () => {
    const present = new Set(board.leads.map((l) => l.stage));
    for (const key of STAGE_KEYS) {
      expect(present.has(key)).toBe(true);
    }
  });

  it('exactly one lead needs the user (the drafted New reply)', () => {
    const needy = board.leads.filter((l) => l.needsYou);
    expect(needy.length).toBe(1);
    expect(needy[0].stage).toBe('new');
  });

  it('the Scheduled lead carries a calendar chip', () => {
    const scheduled = board.leads.find((l) => l.stage === 'scheduled');
    expect(scheduled?.calendarLabel).toBeTruthy();
    expect(scheduled?.reason.kind).toBe('calendar');
  });

  it('produces unique lead + activity ids (React keys never collide)', () => {
    const leadIds = board.leads.map((l) => l.id);
    expect(new Set(leadIds).size).toBe(leadIds.length);
    const actIds = board.activity.map((a) => a.id);
    expect(new Set(actIds).size).toBe(actIds.length);
  });
});

describe('normalizeLead (real backend shape → UI shape)', () => {
  // The live /crm/board returns a lean lead with NO reason/initials/activity.
  // The board reads lead.reason.kind, so a lean lead used to crash it
  // ("Cannot read properties of undefined (reading 'kind')"). Normalize fills
  // the presentational fields so that can't happen.
  const lean = {
    id: 'lead-1',
    name: '',
    kind: 'renter',
    stage: 'new',
    source: 'sms',
    unit: null,
    note: 'Is 4B available?',
    phone: '+17045550142',
  };

  it('fills reason so the board never reads an undefined field', () => {
    const l = normalizeLead(lean as never);
    expect(l.reason).toBeTruthy();
    expect(typeof l.reason.kind).toBe('string');
  });

  it('derives initials from the phone when there is no name', () => {
    const l = normalizeLead(lean as never);
    expect(l.initials).toBe('42');
  });

  it('defaults the booleans/optionals safely', () => {
    const l = normalizeLead(lean as never);
    expect(l.needsYou).toBe(false);
    expect(l.movedAt).toBeNull();
    expect(l.calendarLabel).toBeNull();
  });

  it('is idempotent — a full demo lead passes through unchanged', () => {
    const full = demoCrmBoard().leads[0];
    const l = normalizeLead(full);
    expect(l.reason).toEqual(full.reason);
    expect(l.initials).toBe(full.initials);
    expect(l.needsYou).toBe(full.needsYou);
  });

  it('derives a stage-appropriate reason kind', () => {
    expect(normalizeLead({ ...lean, stage: 'scheduled' } as never).reason.kind).toBe('calendar');
    expect(normalizeLead({ ...lean, stage: 'leased' } as never).reason.kind).toBe('buildium');
  });

  it('maps the backend snake_case tour_at → tourAt and derives a calendar chip', () => {
    const l = normalizeLead({ ...lean, stage: 'scheduled', tour_at: '2026-07-11T14:00:00' } as never);
    expect(l.tourAt).toBe('2026-07-11T14:00:00');
    expect(l.calendarLabel).toContain('Tour · ');
  });
});

describe('tourChipLabel', () => {
  it('formats an ISO datetime as a "Tour · <day> <time>" chip', () => {
    // Use a local-time string so the label is timezone-stable in CI.
    const label = tourChipLabel('2026-07-11T14:05:00');
    expect(label.startsWith('Tour · ')).toBe(true);
    expect(label).toContain(':05');
  });

  it('degrades to "Tour" on an unparseable value', () => {
    expect(tourChipLabel('not-a-date')).toBe('Tour');
  });
});

describe('demoLeadMessages', () => {
  it('returns a threaded conversation for a lead with history', () => {
    const msgs = demoLeadMessages('demo-lead-priya');
    expect(msgs.length).toBeGreaterThan(0);
    expect(msgs.some((m) => m.direction === 'inbound')).toBe(true);
    expect(msgs.some((m) => m.direction === 'outbound')).toBe(true);
    for (const m of msgs) {
      expect(['inbound', 'outbound']).toContain(m.direction);
      expect(m.body).toBeTruthy();
    }
  });

  it('returns an empty thread for a first-touch / unknown lead', () => {
    expect(demoLeadMessages('demo-lead-renee')).toEqual([]);
    expect(demoLeadMessages('nope')).toEqual([]);
  });
});

describe('demoCrmOverview + demoStageLeads (mirror backend shape)', () => {
  const ov = demoCrmOverview();

  it('has all five stages in order with counts', () => {
    expect(ov.stages.map((s) => s.key)).toEqual(['new', 'contacted', 'scheduled', 'applied', 'leased']);
    for (const s of ov.stages) {
      expect(typeof s.count).toBe('number');
      expect(s.recent.length).toBeLessThanOrEqual(3);
      expect(s.recent.length).toBeLessThanOrEqual(s.count);
    }
  });

  it('metrics add up (active + leased = total)', () => {
    const { active, leased, total } = ov.metrics;
    expect(active + leased).toBe(total);
    expect(total).toBeGreaterThan(6); // the pool is bigger than the 6 board leads
  });

  it('stage list returns only that stage, and search filters it', () => {
    const newLeads = demoStageLeads('new');
    expect(newLeads.leads.length).toBeGreaterThan(0);
    for (const l of newLeads.leads) expect(l.stage).toBe('new');

    const filtered = demoStageLeads('new', 'jordan');
    expect(filtered.leads.every((l) => l.name.toLowerCase().includes('jordan'))).toBe(true);
    expect(demoStageLeads('new', 'zzzznomatch').leads).toEqual([]);
  });

  it('a stage count matches its full list length', () => {
    for (const s of ov.stages) {
      expect(demoStageLeads(s.key).leads.length).toBe(s.count);
    }
  });
});

describe('demoLeadDraft (AI assist — mirrors backend /draft shape)', () => {
  it('returns signals + a drafted reply for a lead with an assist', () => {
    const d = demoLeadDraft('demo-lead-jordan');
    expect(Array.isArray(d.signals)).toBe(true);
    expect(d.signals.length).toBeGreaterThanOrEqual(2);
    expect(typeof d.draft_reply).toBe('string');
    expect(d.draft_reply).toBeTruthy();
  });

  it('degrades to empty signals + null draft for an unknown lead', () => {
    expect(demoLeadDraft('nope')).toEqual({ signals: [], draft_reply: null });
  });
});
