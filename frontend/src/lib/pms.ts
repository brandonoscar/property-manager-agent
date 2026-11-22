/**
 * Every property-management system Occupella knows, in one place.
 *
 * The web app's copy of `src/helixis/pms/registry.py`.
 * `tests/unit/test_pms_registry.py` reads both files and fails if they
 * disagree (CLAUDE.md rule 15), so a system the server can name is never one
 * this file calls Buildium.
 *
 * ⚠ Knowing a system is not offering it. Settings → Connectors shows the cards
 * the server lists (`services/connectors.ts` `PMS_CONNECTOR_IDS`). Rent
 * Manager, Propertyware and DoorLoop are here so that a workspace on one is
 * named correctly and is never sent Buildium's sync.
 */

export type PmsId = 'buildium' | 'rentvine' | 'rentmanager' | 'propertyware' | 'doorloop';

export interface PmsInfo {
  id: PmsId;
  /** How the system is named to a customer. */
  name: string;
  /**
   * True for Buildium only. Every other system is read from Occupella's synced
   * copy, and Buildium-only features (staff routing, the created-date note)
   * do not apply to it.
   */
  liveTools: boolean;
}

export const PMS: Readonly<Record<PmsId, PmsInfo>> = {
  buildium: { id: 'buildium', name: 'Buildium', liveTools: true },
  rentvine: { id: 'rentvine', name: 'Rentvine', liveTools: false },
  rentmanager: { id: 'rentmanager', name: 'Rent Manager', liveTools: false },
  propertyware: { id: 'propertyware', name: 'Propertyware', liveTools: false },
  doorloop: { id: 'doorloop', name: 'DoorLoop', liveTools: false },
};

/**
 * The system for an id the server sent, or `null` for no id or one this build
 * does not know. Callers keep their Buildium behaviour on `null`, as they did
 * before a second system existed.
 */
export function pmsInfo(id: string | null | undefined): PmsInfo | null {
  if (!id || !Object.prototype.hasOwnProperty.call(PMS, id)) return null;
  return PMS[id as PmsId];
}
