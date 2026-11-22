/**
 * UI-fuzz data generator (keyless, client-only).
 *
 * The problem it solves: demo mode always renders the SAME fixtures, so a
 * screenshot check of demo mode tells you nothing new run-to-run. This module
 * varies the input — it takes the real demo rows and mutates them with an
 * edge-case matrix (empty / huge / weird-chars / many / missing / unknown), so
 * each fuzz seed exercises different data shapes and the Playwright sweep
 * (`frontend/fuzz/run.mjs`) can surface render bugs a single happy-path run
 * never would. This is the render-layer half of the harness planned in
 * `docs/plans/ui-fuzz-harness.md` — it needs no keys and no backend.
 *
 * It directly enforces the build principle "never assume users fill in optional
 * Buildium fields; degrade gracefully" — the mutators deliberately blank,
 * overflow, and garble the optional fields that real Buildium data often leaves
 * empty.
 *
 * SAFETY: like demoData, this is pure in-memory transformation. No network, no
 * DB, no writes. It is only reachable inside demo mode (`VITE_DEMO_MODE`) AND
 * only when a `?fuzz=<seed>` query param is present — so it can never touch a
 * real render path or real data.
 */

import type { InboxRow } from './inbox';

// ── Seeded PRNG (mulberry32) — deterministic so a seed reproduces the
//    exact dataset; a failing seed replays the exact break. ────────────
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Parse `?fuzz=<seed>` from a location search string. Returns the seed (a
 *  non-negative integer) or null when absent/invalid. Pure + exported so the
 *  demo seam and the tests share one definition. */
export function fuzzSeedFromSearch(search: string): number | null {
  try {
    const raw = new URLSearchParams(search).get('fuzz');
    if (raw === null) return null;
    const n = Number.parseInt(raw, 10);
    return Number.isFinite(n) && n >= 0 ? n : null;
  } catch {
    return null;
  }
}

// ── Nasty content the mutators draw from ──────────────────────────────
const HUGE =
  'Overflowingtenantnamewithnospacesatalltoforcehorizontaloverflowandclippingchecks'.repeat(3);
const WEIRD = '🏚️🔧 "Ünît" <script>alert(1)</script> ‮reversed‬ émoji—dash 你好';
const LONG_SENTENCE =
  'This is a deliberately very long summary paragraph that keeps going well past ' +
  'any reasonable card height to make sure the layout wraps and clamps instead of ' +
  'blowing out the container or pushing the action buttons below the fold, again ' +
  'and again and again until we are confident nothing overflows or clips badly.';

function clone<T>(v: T): T {
  const sc = (globalThis as { structuredClone?: <U>(x: U) => U }).structuredClone;
  return sc ? sc(v) : (JSON.parse(JSON.stringify(v)) as T);
}

// Each mutator takes a fresh clone of a base row + a rng and returns an
// edge-case variant. Every mutation is guarded (optional chaining) so a
// mutator can never throw on a differently-shaped base row.
type Mutator = { name: string; apply: (row: InboxRow, rng: () => number) => InboxRow };

const MUTATORS: Mutator[] = [
  {
    name: 'empty-optionals',
    apply: (row) => {
      // The "user left every optional field blank" case.
      if (row.enrichment?.task) {
        row.enrichment.task.title = '';
        row.enrichment.task.assignee_name = null;
        row.enrichment.task.requested_by_first_name = null;
        row.enrichment.task.requested_by_last_name = null;
      }
      if (row.enrichment?.unit) {
        row.enrichment.unit.address_line1 = null;
        row.enrichment.unit.unit_number = null;
      }
      if (row.summary) row.summary.text = '';
      if (row.action_surface) {
        row.action_surface.summary = '';
        for (const a of row.action_surface.actions) a.intent = '';
      }
      return row;
    },
  },
  {
    name: 'huge-strings',
    apply: (row) => {
      if (row.enrichment?.task) row.enrichment.task.title = HUGE;
      const t = row.enrichment?.active_lease?.tenants?.[0];
      if (t) t.first_name = HUGE;
      if (row.summary) row.summary.text = LONG_SENTENCE + ' ' + LONG_SENTENCE;
      return row;
    },
  },
  {
    name: 'weird-chars',
    apply: (row) => {
      if (row.enrichment?.task) row.enrichment.task.title = WEIRD;
      if (row.summary) row.summary.text = WEIRD + ' ' + WEIRD;
      if (row.action_surface) {
        for (const a of row.action_surface.actions) a.label = WEIRD;
      }
      return row;
    },
  },
  {
    name: 'many-actions',
    apply: (row, rng) => {
      // Blow the flip-through card up to 6-8 tabs.
      const base = row.action_surface?.actions ?? [];
      if (row.action_surface && base.length > 0) {
        const target = 6 + Math.floor(rng() * 3);
        const grown = [];
        for (let i = 0; i < target; i++) {
          const src = clone(base[i % base.length]);
          src.label = `${src.label} #${i + 1}`;
          grown.push(src);
        }
        row.action_surface.actions = grown;
      }
      return row;
    },
  },
  {
    name: 'no-actions',
    apply: (row) => {
      if (row.action_surface) row.action_surface.actions = [];
      return row;
    },
  },
  {
    name: 'unknown-category',
    apply: (row) => {
      if (row.enrichment?.task) row.enrichment.task.category = 'ZZ_Unrecognized_Category_42';
      return row;
    },
  },
  {
    name: 'null-enrichment',
    apply: (row) => {
      // The pre-enrichment / enrichment-failed shape — the floor must render.
      row.enrichment = null;
      return row;
    },
  },
  {
    name: 'long-highlights',
    apply: (row) => {
      if (row.action_surface) {
        row.action_surface.summary_highlights = [
          'a',
          LONG_SENTENCE.slice(0, 60),
          WEIRD,
          'unit 4B',
          'has not been cooling',
        ];
      }
      if (row.summary) row.summary.text = LONG_SENTENCE;
      return row;
    },
  },
  {
    name: 'name-mismatch',
    apply: (row) => {
      // Trips the red "Name mismatch" attention chip.
      if (row.enrichment?.task) {
        row.enrichment.task.requested_by_first_name = 'Nobody';
        row.enrichment.task.requested_by_last_name = 'Unlisted';
      }
      return row;
    },
  },
  {
    name: 'recurrence',
    apply: (row) => {
      // Trips the amber recurrence chip via history_unit length.
      if (row.enrichment) {
        row.enrichment.history_unit = [
          { title: 'prior', event_time: row.event_time } as never,
          { title: 'prior', event_time: row.event_time } as never,
        ];
      }
      return row;
    },
  },
];

export const FUZZ_MUTATOR_NAMES = MUTATORS.map((m) => m.name);

/**
 * Produce a fuzzed inbox-row set from the base demo rows + a seed.
 *
 * Deterministic: the same (base, seed) always yields the same rows. Each
 * output row is a base row with one edge-case mutator applied; the seed picks
 * the starting mutator + rotation so a run covers the whole matrix and a given
 * seed is reproducible. Never throws — a mutator failure falls back to the
 * un-mutated clone so the sweep still renders something.
 */
export function fuzzInboxRows(base: InboxRow[], seed: number): InboxRow[] {
  if (base.length === 0) return base;
  const rng = mulberry32(seed);
  const start = Math.floor(rng() * MUTATORS.length);
  const out: InboxRow[] = [];
  // One row per mutator so every edge case is on screen each run.
  for (let i = 0; i < MUTATORS.length; i++) {
    const mutator = MUTATORS[(start + i) % MUTATORS.length];
    const baseRow = clone(base[i % base.length]);
    // Unique id so React keys + the pager reset behave.
    baseRow.id = `fuzz-${seed}-${i}-${mutator.name}`;
    let mutated: InboxRow;
    try {
      mutated = mutator.apply(baseRow, rng);
    } catch {
      mutated = baseRow; // never let a mutator sink the whole set
    }
    out.push(mutated);
  }
  return out;
}
