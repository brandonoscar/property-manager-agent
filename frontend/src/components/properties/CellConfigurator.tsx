/**
 * PR #81 — extensible cell renderer for the Properties × Roles grid.
 *
 * Picks a sub-component based on ``action_type``. Today (v1) the only
 * sub-component is ``<AssignToUserCell>``; future PRs add
 * ``<AutomateCell>``, ``<EscalateCell>``, etc. without touching the
 * grid layout or the row component.
 *
 * The grid never assumes "this cell is a user picker." It hands the
 * action_type + config to this component and lets the cell render
 * itself. That's the architectural seam from PR #80's brief.
 */

import type { CellAssignment, StaffOption } from '../../services/properties';
import { AssignToUserCell } from './AssignToUserCell';

export interface CellConfiguratorProps {
  assignment: CellAssignment | null;
  staff: StaffOption[];
  disabled?: boolean;
  onChange: (buildiumUserId: number | null) => void;
}

export function CellConfigurator({
  assignment,
  staff,
  disabled,
  onChange,
}: CellConfiguratorProps) {
  // v1 only renders ``assign_to_user``. An unset cell is also rendered
  // by ``AssignToUserCell`` so the dropdown is always visible — the
  // property manager picks a user to "configure" the cell from empty.
  const action_type = assignment?.action_type ?? 'assign_to_user';

  switch (action_type) {
    case 'assign_to_user':
      return (
        <AssignToUserCell
          buildiumUserId={assignment?.buildium_user_id ?? null}
          staff={staff}
          disabled={disabled}
          onChange={onChange}
        />
      );
    default:
      // Forward-compat: a future action_type the frontend doesn't know
      // yet renders as a non-interactive label so we don't crash on a
      // newer-than-client row. Backend admin-fallback covers routing.
      return (
        <div
          className="text-[12px] italic"
          style={{ color: 'var(--ink-subtle)' }}
        >
          {action_type}
        </div>
      );
  }
}
