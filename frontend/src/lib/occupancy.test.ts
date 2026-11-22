/**
 * Unknown occupancy is never vacant (rule 14). Sandbox, 2026-10-02: a
 * 120-unit property with 100 units of unknown occupancy read "Fully vacant".
 */
import { describe, expect, it } from 'vitest';
import { knownOccupancyRatio, occupancySub, vacantCount } from './occupancy';

const counts = (unit_count: number, occupied_count: number, occupancy_unknown_count: number) => ({
  unit_count,
  occupied_count,
  occupancy_unknown_count,
});

describe('occupancy', () => {
  it('never counts unknown units as vacant', () => {
    expect(vacantCount(counts(120, 0, 100))).toBe(20);
    expect(occupancySub(counts(120, 0, 100))).toBe('20 vacant · 100 occupancy unknown');
  });

  it('says "Fully occupied" only when nothing is vacant AND nothing is unknown', () => {
    expect(occupancySub(counts(6, 6, 0))).toBe('Fully occupied');
    expect(occupancySub(counts(5, 0, 5))).toBe('5 occupancy unknown');
    expect(occupancySub(counts(5, 0, 5))).not.toContain('vacant');
  });

  it('reads exactly as before when every unit is known (Buildium)', () => {
    expect(vacantCount(counts(8, 6, 0))).toBe(2);
    expect(occupancySub(counts(8, 6, 0))).toBe('2 vacant');
    expect(knownOccupancyRatio(counts(8, 6, 0))).toBe(6 / 8);
  });

  it('sorts on the units whose occupancy is known, and not at all when none is', () => {
    expect(knownOccupancyRatio(counts(120, 0, 100))).toBe(0);
    expect(knownOccupancyRatio(counts(10, 5, 5))).toBe(1); // 5 occupied of 5 known
    expect(knownOccupancyRatio(counts(5, 0, 5))).toBeNull();
  });

  it('never goes below zero vacant', () => {
    expect(vacantCount(counts(2, 3, 0))).toBe(0);
  });
});
