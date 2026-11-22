import { describe, it, expect } from 'vitest';

import { demoInboxRows } from './demoData';
import { FUZZ_MUTATOR_NAMES, fuzzInboxRows, fuzzSeedFromSearch } from './fuzzData';
import type { InboxRow } from './inbox';

// The base demo rows are the fuzzer's raw material. demoInboxRows() with no
// ?fuzz param returns them unchanged (no window in jsdom → null seed).
const base: InboxRow[] = demoInboxRows();

describe('fuzzSeedFromSearch', () => {
  it('parses a valid seed', () => {
    expect(fuzzSeedFromSearch('?fuzz=42')).toBe(42);
    expect(fuzzSeedFromSearch('?foo=1&fuzz=7')).toBe(7);
  });
  it('returns null when absent or invalid', () => {
    expect(fuzzSeedFromSearch('')).toBeNull();
    expect(fuzzSeedFromSearch('?x=1')).toBeNull();
    expect(fuzzSeedFromSearch('?fuzz=abc')).toBeNull();
    expect(fuzzSeedFromSearch('?fuzz=-3')).toBeNull();
  });
});

describe('fuzzInboxRows', () => {
  it('is deterministic — same seed yields identical rows', () => {
    expect(JSON.stringify(fuzzInboxRows(base, 123))).toBe(
      JSON.stringify(fuzzInboxRows(base, 123)),
    );
  });

  it('different seeds generally differ', () => {
    // The seed rotates which mutator lands on which row, so the id tags differ.
    const a = fuzzInboxRows(base, 1).map((r) => r.id);
    const b = fuzzInboxRows(base, 2).map((r) => r.id);
    expect(a).not.toEqual(b);
  });

  it('covers the whole edge-case matrix each run (one row per mutator)', () => {
    const rows = fuzzInboxRows(base, 5);
    expect(rows.length).toBe(FUZZ_MUTATOR_NAMES.length);
    for (const name of FUZZ_MUTATOR_NAMES) {
      expect(rows.some((r) => r.id.includes(name))).toBe(true);
    }
  });

  it('produces unique ids (React keys never collide)', () => {
    const ids = fuzzInboxRows(base, 9).map((r) => r.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('never throws across a spread of seeds and returns valid-shaped rows', () => {
    for (let seed = 0; seed < 25; seed++) {
      const rows = fuzzInboxRows(base, seed);
      for (const row of rows) {
        // Minimal shape the inbox render relies on.
        expect(typeof row.id).toBe('string');
        expect(typeof row.event_name).toBe('string');
        expect(Array.isArray(row.action_surface?.actions ?? [])).toBe(true);
      }
    }
  });

  it('does not mutate the base rows it was given', () => {
    const snapshot = JSON.stringify(base);
    fuzzInboxRows(base, 3);
    expect(JSON.stringify(base)).toBe(snapshot);
  });

  it('empty base in → empty out (no crash)', () => {
    expect(fuzzInboxRows([], 1)).toEqual([]);
  });
});
