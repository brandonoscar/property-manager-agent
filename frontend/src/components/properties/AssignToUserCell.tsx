/**
 * PR #81 — one cell of the Properties × Roles grid for the
 * ``assign_to_user`` action type. A dropdown listing active
 * Buildium staff plus a permanent "(Unassigned — falls back to
 * admin)" sentinel that clears the cell.
 *
 * Hard rule from the brief: NO typing of names, NO "add member."
 * Options come from the synced ``buildium_staff`` cache only.
 */

import { useId, useMemo } from 'react';
import type { StaffOption } from '../../services/properties';

export interface AssignToUserCellProps {
  buildiumUserId: number | null;
  staff: StaffOption[];
  disabled?: boolean;
  onChange: (buildiumUserId: number | null) => void;
}

const UNASSIGNED_VALUE = '__unassigned__';

export function AssignToUserCell({
  buildiumUserId,
  staff,
  disabled,
  onChange,
}: AssignToUserCellProps) {
  const selectId = useId();
  const value = useMemo(
    () => (buildiumUserId == null ? UNASSIGNED_VALUE : String(buildiumUserId)),
    [buildiumUserId],
  );

  // PR #118 Phase E — full display string for the currently-selected
  // option, used as the select's ``title`` so the browser shows a
  // tooltip with the unclipped name on hover. The pre-#118 cell
  // truncated mid-word ("Vinny Dan · Adm[in]" with the trailing "in"
  // cut off) because the native <select>'s visible text width
  // doesn't fit the longest entries; a tooltip + ellipsis CSS makes
  // truncation legible without changing the layout.
  const selectedFullText = useMemo(() => {
    if (buildiumUserId == null) return 'Unassigned';
    const found = staff.find((s) => s.buildium_user_id === buildiumUserId);
    if (!found) return '';
    return `${found.display_name}${found.role ? ` · ${found.role}` : ''}`;
  }, [buildiumUserId, staff]);

  return (
    <select
      id={selectId}
      data-testid="assign-to-user-cell"
      aria-label="Assigned staff"
      title={selectedFullText}
      value={value}
      disabled={disabled || staff.length === 0}
      onChange={(e) => {
        const next = e.target.value;
        if (next === UNASSIGNED_VALUE) {
          onChange(null);
          return;
        }
        const id = Number(next);
        if (Number.isFinite(id)) onChange(id);
      }}
      className="w-full text-[13px] rounded-[var(--r-sm)] px-2 py-1 border bg-transparent transition-colors truncate cursor-pointer disabled:cursor-not-allowed"
      style={{
        borderColor: 'var(--line)',
        color: 'var(--ink)',
        textOverflow: 'ellipsis',
      }}
    >
      <option value={UNASSIGNED_VALUE} title="Falls back to the admin">Unassigned</option>
      {staff.map((s) => {
        const fullText = `${s.display_name}${s.role ? ` · ${s.role}` : ''}`;
        return (
          <option key={s.buildium_user_id} value={s.buildium_user_id} title={fullText}>
            {fullText}
          </option>
        );
      })}
    </select>
  );
}
