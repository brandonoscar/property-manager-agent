/**
 * Turnovers — the property cockpit's make-ready tracker. Rows open
 * automatically when Buildium files a move-out; each shows three tracked
 * steps (inspection → make-ready work orders → list the unit) worked as
 * checkboxes, with the mirror's open-WO count as a hint on step two.
 * DESIGN.md: token palette, 1px var(--line) borders, no gradients.
 */

import { useCallback, useEffect, useState } from 'react';
import { DoorOpen, X } from 'lucide-react';
import { formatCalendarDate } from '../../lib/calendarDate';
import {
  fetchTurnovers,
  setTurnoverStep,
  dismissTurnover,
  type Turnover,
  type TurnoverStep,
} from '../../services/turnovers';

const STEPS: { key: TurnoverStep; label: string }[] = [
  { key: 'inspection', label: 'Move-out inspection' },
  { key: 'work_orders', label: 'Make-ready work orders' },
  { key: 'listed', label: 'Unit listed' },
];

const stepDone = (t: Turnover, step: TurnoverStep): boolean =>
  step === 'inspection'
    ? t.inspection_done
    : step === 'work_orders'
      ? t.work_orders_done
      : t.listed_done;

export function TurnoversSection({ propertyId }: { propertyId: number }) {
  const [turnovers, setTurnovers] = useState<Turnover[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const reload = useCallback(() => {
    fetchTurnovers(propertyId)
      .then(setTurnovers)
      .catch((e: unknown) =>
        setError(e instanceof Error ? e.message : 'Failed to load turnovers'),
      );
  }, [propertyId]);

  useEffect(() => {
    setTurnovers(null);
    setError(null);
    reload();
  }, [reload]);

  const toggle = async (t: Turnover, step: TurnoverStep) => {
    if (busyId) return;
    setBusyId(t.id);
    try {
      const updated = await setTurnoverStep(propertyId, t.id, step, !stepDone(t, step));
      setTurnovers((rows) => rows?.map((r) => (r.id === updated.id ? updated : r)) ?? rows);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to update the step');
    } finally {
      setBusyId(null);
    }
  };

  const dismiss = async (t: Turnover) => {
    if (busyId) return;
    setBusyId(t.id);
    try {
      await dismissTurnover(propertyId, t.id);
      setTurnovers((rows) => rows?.filter((r) => r.id !== t.id) ?? rows);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to dismiss the turnover');
    } finally {
      setBusyId(null);
    }
  };

  // No turnovers at all → stay quiet. The section only earns space on the
  // page when a move-out actually opened one.
  if (turnovers !== null && turnovers.length === 0 && !error) return null;

  return (
    <div
      className="rounded-[var(--r-md)] border"
      style={{ borderColor: 'var(--line)', background: 'var(--canvas-raised)' }}
      data-testid="turnovers-section"
    >
      <div
        className="flex items-center gap-2 px-4 py-2.5 border-b text-[13px] font-medium"
        style={{ borderColor: 'var(--line)', color: 'var(--ink)' }}
      >
        <DoorOpen size={13} strokeWidth={1.7} style={{ color: 'var(--ink-muted)' }} />
        Turnovers — move-outs to make rent-ready
      </div>

      {error ? (
        <div className="px-4 py-3 text-[13px]" style={{ color: 'var(--ink-secondary)' }}>
          {error}
        </div>
      ) : turnovers === null ? (
        <div className="px-4 py-3 text-[13px]" style={{ color: 'var(--ink-muted)' }}>
          Loading…
        </div>
      ) : (
        <div className="px-4 py-2 flex flex-col gap-1">
          {turnovers.map((t) => (
            <div key={t.id} className="py-1.5">
              <div className="flex items-center justify-between gap-3 text-[13px]">
                <span className="min-w-0 truncate" style={{ color: 'var(--ink)' }}>
                  {t.unit_number ? `Unit ${t.unit_number}` : `Lease #${t.lease_buildium_id}`}
                  {t.move_out_date
                    ? ` · move-out ${formatCalendarDate(t.move_out_date)}`
                    : ''}
                </span>
                <span className="flex items-center gap-2 shrink-0">
                  {t.status === 'done' ? (
                    <span
                      className="rounded-full border px-2 py-0.5 text-[12px]"
                      style={{
                        borderColor: 'var(--line)',
                        color: 'var(--ok, var(--ink-secondary))',
                      }}
                    >
                      Rent-ready
                    </span>
                  ) : null}
                  <button
                    type="button"
                    aria-label="Stop tracking this turnover"
                    onClick={() => dismiss(t)}
                    disabled={busyId === t.id}
                    className="disabled:opacity-40"
                    style={{ color: 'var(--ink-muted)' }}
                  >
                    <X size={13} strokeWidth={1.7} />
                  </button>
                </span>
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1">
                {STEPS.map((s) => (
                  <label
                    key={s.key}
                    className="flex items-center gap-1.5 text-[12px] cursor-pointer select-none"
                    style={{
                      color: stepDone(t, s.key) ? 'var(--ink-muted)' : 'var(--ink-secondary)',
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={stepDone(t, s.key)}
                      onChange={() => toggle(t, s.key)}
                      disabled={busyId === t.id}
                      className="accent-[var(--iris)]"
                    />
                    <span
                      style={
                        stepDone(t, s.key) ? { textDecoration: 'line-through' } : undefined
                      }
                    >
                      {s.label}
                    </span>
                    {s.key === 'work_orders' && t.open_work_orders > 0 ? (
                      <span className="text-[12px]" style={{ color: 'var(--ink-muted)' }}>
                        ({t.open_work_orders} open on this unit)
                      </span>
                    ) : null}
                  </label>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
