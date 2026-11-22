/**
 * Horizontal row of soft pill suggestions. One per SUGGESTION_CHIP
 * event the backend emits in this slot of the thread. On click, the
 * chip "collapses" — the parent removes it from the live event log
 * and inserts a "→ <label>" assistant turn so the conversation reads
 * naturally on scrollback.
 *
 * Destructive chips (e.g. "Delete task") render with a muted danger
 * tint and require an extra hover press before the click fires —
 * Occupella house rule: writes are always one click away from confirm.
 */

import { useEffect, useState } from 'react';
import { motion } from 'motion/react';

import type { SuggestionChipData } from './types';

export interface SuggestionRowProps {
  chips: SuggestionChipData[];
  onPick: (chip: SuggestionChipData) => void;
  /** Disabled while a pick is in flight (debounce double-click). */
  disabled?: boolean;
}

export function SuggestionRow({ chips, onPick, disabled }: SuggestionRowProps) {
  if (chips.length === 0) return null;
  return (
    <motion.div
      data-testid="suggestion-row"
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18, ease: [0.2, 0.8, 0.2, 1] }}
      className="flex flex-wrap gap-2 mb-6"
    >
      {chips.map((chip, i) => (
        <SuggestionChip
          key={`${chip.action_index}-${i}`}
          chip={chip}
          onPick={() => onPick(chip)}
          disabled={disabled}
        />
      ))}
    </motion.div>
  );
}

function SuggestionChip({
  chip,
  onPick,
  disabled,
}: {
  chip: SuggestionChipData;
  onPick: () => void;
  disabled?: boolean;
}) {
  // Destructive chips take TWO presses (approval-pipeline PR): the first arms
  // the chip ("Click again to confirm", auto-disarms after 3s), the second
  // fires. This implements the guard the event docstring always promised —
  // before this, is_destructive changed only the tint and a single click
  // executed immediately.
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 3000);
    return () => clearTimeout(t);
  }, [armed]);
  const handleClick = () => {
    if (chip.is_destructive && !armed) {
      setArmed(true);
      return;
    }
    setArmed(false);
    onPick();
  };
  const tone = chip.is_destructive ? 'var(--danger)' : 'var(--ink)';
  const border = chip.is_destructive ? 'var(--danger)' : 'var(--line)';
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={handleClick}
      data-armed={armed || undefined}
      data-testid="suggestion-chip"
      className="inline-flex items-center h-8 px-3 rounded-[var(--r-sm)] border text-[13px] font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
      style={{
        background: 'transparent',
        borderColor: border,
        color: tone,
      }}
      onMouseEnter={(e) => {
        if (disabled) return;
        const el = e.currentTarget;
        el.style.background = chip.is_destructive ? 'rgba(255,90,90,0.06)' : 'var(--iris-soft)';
        el.style.borderColor = chip.is_destructive ? 'var(--danger)' : 'var(--iris)';
      }}
      onMouseLeave={(e) => {
        if (disabled) return;
        const el = e.currentTarget;
        el.style.background = 'transparent';
        el.style.borderColor = border;
      }}
      onMouseDown={(e) => {
        if (disabled) return;
        const el = e.currentTarget;
        el.style.transform = 'scale(0.97)';
      }}
      onMouseUp={(e) => {
        if (disabled) return;
        const el = e.currentTarget;
        el.style.transform = 'scale(1)';
      }}
    >
      {armed ? 'Click again to confirm' : chip.label}
    </button>
  );
}
