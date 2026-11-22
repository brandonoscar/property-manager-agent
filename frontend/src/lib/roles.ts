/**
 * The team-role ordering (ADR 0005), in one place.
 *
 * This lived as a private function inside ``SettingsPage``. The Leasing
 * compliance gate needs the same comparison, and a second copy of a
 * privilege ordering is how two screens come to disagree about who counts as
 * a manager — the failure mode is silent and points the permissive way.
 *
 * ⚠ Fails CLOSED on ``null``. The role arrives from an async probe, so
 * ``null`` means "not known yet", and a gate that treats unknown as
 * permitted is open for the whole first paint.
 */

import type { TeamRole } from '../services/company';

const ROLE_RANK: Record<TeamRole, number> = { admin: 0, manager: 1, member: 2 };

export function roleAtLeast(role: TeamRole | null, minimum: TeamRole): boolean {
  if (role === null) return false;
  return ROLE_RANK[role] <= ROLE_RANK[minimum];
}
