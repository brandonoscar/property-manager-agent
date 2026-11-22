/**
 * Inline confirmation gate. Pauses the thread for explicit confirm
 * before any write fires. Renders a 1-sentence summary, two CTAs
 * (primary iris + ghost cancel), and a single ``Show raw`` toggle
 * that expands the payload preview. Never opens a modal — the gate
 * is part of the thread surface, not a layer over it.
 */

import { useState } from 'react';
import { motion } from 'motion/react';

import type { ConfirmGateData } from './types';

export interface ConfirmGateProps {
  data: ConfirmGateData;
  onConfirm: () => void;
  onCancel: () => void;
  /** Disabled while the execute round-trip is in flight. */
  busy?: boolean;
}

export function ConfirmGate({ data, onConfirm, onCancel, busy }: ConfirmGateProps) {
  const [showRaw, setShowRaw] = useState(false);
  return (
    <motion.div
      data-testid="confirm-gate"
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2, ease: [0.2, 0.8, 0.2, 1] }}
      className="mb-6 rounded-[var(--r-md)] border p-4"
      style={{ borderColor: 'var(--line)', background: 'var(--canvas-elevated)' }}
    >
      <div
        className="text-[14px] leading-relaxed mb-3"
        style={{ color: 'var(--ink)' }}
      >
        {data.summary}
      </div>

      <div className="flex items-center gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={onConfirm}
          data-testid="confirm-gate-confirm"
          className="inline-flex items-center h-9 px-4 rounded-[var(--r-sm)] text-[14px] font-medium transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
          style={{ background: 'var(--iris)', color: 'var(--canvas)' }}
          onMouseEnter={(e) => {
            if (busy) return;
            (e.currentTarget as HTMLElement).style.background = 'var(--iris-hover)';
          }}
          onMouseLeave={(e) => {
            (e.currentTarget as HTMLElement).style.background = 'var(--iris)';
          }}
        >
          {busy ? 'Working…' : data.confirm_label}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={onCancel}
          data-testid="confirm-gate-cancel"
          className="inline-flex items-center h-9 px-3 rounded-[var(--r-sm)] text-[13px] transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
          style={{ background: 'transparent', color: 'var(--ink-muted)' }}
          onMouseEnter={(e) => {
            (e.currentTarget as HTMLElement).style.color = 'var(--ink)';
          }}
          onMouseLeave={(e) => {
            (e.currentTarget as HTMLElement).style.color = 'var(--ink-muted)';
          }}
        >
          {data.cancel_label}
        </button>
        <span className="flex-1" />
        <button
          type="button"
          onClick={() => setShowRaw((v) => !v)}
          data-testid="confirm-gate-show-raw"
          className="text-[12px] underline-offset-2"
          style={{ color: 'var(--ink-subtle)' }}
          onMouseEnter={(e) => {
            (e.currentTarget as HTMLElement).style.textDecoration = 'underline';
          }}
          onMouseLeave={(e) => {
            (e.currentTarget as HTMLElement).style.textDecoration = 'none';
          }}
        >
          {showRaw ? 'Hide raw' : 'Show raw'}
        </button>
      </div>

      {showRaw && (
        <pre
          data-testid="confirm-gate-raw"
          className="mt-3 text-[12px] font-mono whitespace-pre-wrap break-words rounded-[var(--r-sm)] p-3 max-h-[280px] overflow-auto"
          style={{
            background: 'var(--canvas)',
            color: 'var(--ink-subtle)',
            border: '1px solid var(--line)',
          }}
        >
          {JSON.stringify(data.payload_preview, null, 2)}
        </pre>
      )}
    </motion.div>
  );
}
