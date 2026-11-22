/**
 * What the page shows when the backend sends `null` for a figure or a list.
 *
 * ⚠ `null` from the mirror endpoints means "this company's property
 * management system doesn't send it", never zero or empty. Rentvine sends no
 * rent balances, payments, bills, tasks, staff or lease-tenant links, so a
 * plain `?? 0` would tell a Rentvine customer "$0 delinquent" or "Everyone
 * current" about tenants we know nothing about. The backend decides
 * (`src/helixis/db/pms_coverage.py`); these helpers only word it, and
 * `pms_name` says which system to check.
 */

/**
 * A whole list the backend declined: this company's PMS doesn't send it
 * (Rentvine sends no lease-tenant links and no staff list). ⚠ Never collapse
 * it to `[]`: an empty Tenants tab reads as "you have no tenants".
 *
 * Lives here, not in `services/`, so a test that mocks the service module
 * still gets the real class for `instanceof`.
 */
export class NotSyncedRows {
  constructor(readonly pmsName: string | null) {}
}

/** "Occupella doesn't sync rent balances from Rentvine yet." */
export function notSynced(pmsName: string | null | undefined, what: string): string {
  if (pmsName) return `Occupella doesn't sync ${what} from ${pmsName} yet.`;
  return `We can't read ${what} right now.`;
}

/** A list the backend may decline: its rows, and the line to show if there are none. */
export function listOrNotSynced<T>(
  list: T[] | null | undefined,
  empty: string,
  pmsName: string | null | undefined,
  what: string,
): { items: T[]; empty: string } {
  if (list == null) return { items: [], empty: notSynced(pmsName, what) };
  return { items: list, empty };
}

/** The Delinquent tile. Unknown is never "$0" or "Everyone current". */
export function delinquencyTile(
  leases: number | null | undefined,
  total: number | null | undefined,
  pmsName: string | null | undefined,
  money: (n: number) => string,
): { value: string; sub: string } {
  if (leases == null || total == null) {
    return { value: '—', sub: notSynced(pmsName, 'rent balances') };
  }
  if (!leases) return { value: '$0', sub: 'Everyone current' };
  return { value: money(total), sub: `${leases} lease${leases > 1 ? 's' : ''} behind` };
}

/** Sum a column the backend may send as null. `null` when ANY row is unknown. */
export function sumKnown<T>(rows: T[], pick: (row: T) => number | null | undefined): number | null {
  let total = 0;
  for (const row of rows) {
    const v = pick(row);
    if (v == null) return null;
    total += v;
  }
  return total;
}
