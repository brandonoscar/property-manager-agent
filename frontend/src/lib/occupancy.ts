/**
 * Occupancy without guessing. A unit with no Active lease whose occupancy the
 * PMS never stated (Rentvine units completed from the units export), or that
 * the PMS flags occupied while our lease data disagrees, is neither occupied
 * nor vacant (`occupancy_unknown_count`, computed by the backend). Before this, both screens computed
 * `unit_count - occupied_count` and called those units vacant: a 120-unit
 * property with 100 unknown read as fully vacant (sandbox, 2026-10-02;
 * CLAUDE.md rule 14).
 *
 * The same arithmetic as the backend's `PropertySummary.vacant_count`
 * (`src/helixis/db/repositories/cockpit.py`); `tests/unit/test_occupancy_unknown.py`
 * reads both sides.
 */
import type { CockpitSummary } from '../services/properties';

type Counts = Pick<CockpitSummary, 'unit_count' | 'occupied_count' | 'occupancy_unknown_count'>;

/** Units KNOWN to be vacant. Never the unknown ones. */
export function vacantCount(s: Counts): number {
  return Math.max(0, s.unit_count - s.occupied_count - (s.occupancy_unknown_count ?? 0));
}

/** The line under "X/Y": vacant and unknown named separately. "Fully
 * occupied" only when nothing is vacant AND nothing is unknown. */
export function occupancySub(s: Counts): string {
  const vacant = vacantCount(s);
  const unknown = s.occupancy_unknown_count ?? 0;
  const parts: string[] = [];
  if (vacant > 0) parts.push(`${vacant} vacant`);
  if (unknown > 0) parts.push(`${unknown} occupancy unknown`);
  return parts.length ? parts.join(' · ') : 'Fully occupied';
}

/** Occupied over the units whose occupancy is KNOWN, for sorting; `null`
 * when no unit's occupancy is known (an unknown never sorts as vacant). */
export function knownOccupancyRatio(s: Counts): number | null {
  const known = s.unit_count - (s.occupancy_unknown_count ?? 0);
  return known > 0 ? s.occupied_count / known : null;
}
