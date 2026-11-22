/**
 * Tool activity card — renders an agent tool call.
 *
 * Phase 5 of the premium-OS redesign: dual-mode.
 *   • mode='chat'  — inline card in the conversation transcript,
 *     expandable to reveal the full result blob.
 *   • mode='rail'  — a single compact row intended for the Activity
 *     rail (sub-list under a running task). No expand, just status +
 *     duration.
 *
 * In both modes we use the new token palette and Lucide icons — no
 * emoji.
 */

import { useState } from 'react';
import { motion } from 'motion/react';
import {
  Home,
  Wrench,
  DollarSign,
  Globe,
  Mail,
  Calendar,
  Brain,
  AlertCircle,
  Cog,
  Check,
  X as XIcon,
} from 'lucide-react';
import type { HelixisEvent } from '../services/agent';

type ToolStatus = 'running' | 'done' | 'error';

interface ToolCardProps {
  startEvent: HelixisEvent;
  resultEvent?: HelixisEvent;
  mode?: 'chat' | 'rail';
  /**
   * Visual variant. ``'subagent'`` adds a small iris dot + "Sub-agent"
   * label above the card title so the user can tell agent-driven
   * sub-flows apart from raw tool calls. Behaviourally identical.
   */
  variant?: 'tool' | 'subagent';
}

export function ToolCard({
  startEvent,
  resultEvent,
  mode = 'chat',
  variant = 'tool',
}: ToolCardProps) {
  const toolName = (startEvent.data.tool as string) || 'Tool';
  const description = (startEvent.data.description as string) || '';
  const result = resultEvent?.data.result as string | undefined;
  const durationMs = resultEvent?.data.duration_ms as number | undefined;
  const error = resultEvent?.data.error as string | undefined;

  const status: ToolStatus = !resultEvent
    ? 'running'
    : error
    ? 'error'
    : 'done';

  if (mode === 'rail') {
    return (
      <RailRow
        toolName={toolName}
        status={status}
        durationMs={durationMs}
        description={description}
      />
    );
  }

  return (
    <ChatCard
      toolName={toolName}
      status={status}
      description={description}
      result={result}
      error={error}
      durationMs={durationMs}
      variant={variant}
    />
  );
}

// ── Rail row ──────────────────────────────────────────────────────

function RailRow({
  toolName,
  status,
  durationMs,
  description,
}: {
  toolName: string;
  status: ToolStatus;
  durationMs: number | undefined;
  description: string;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, x: 6 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: 0.18, ease: [0.2, 0.8, 0.2, 1] }}
      className="flex items-start gap-2.5 py-2"
    >
      <StatusGlyph status={status} />
      <div className="flex-1 min-w-0">
        <div className="flex items-baseline gap-2">
          <span
            className="text-[13px] truncate"
            style={{ color: 'var(--ink)' }}
          >
            {formatToolName(toolName)}
          </span>
          {durationMs !== undefined && (
            <span
              className="font-mono tabular-nums shrink-0"
              style={{
                fontSize: 12,
                color: 'var(--ink-faint)',
              }}
            >
              {(durationMs / 1000).toFixed(1)}s
            </span>
          )}
        </div>
        {description && status === 'running' && (
          <div
            className="text-[12px] mt-0.5 leading-snug truncate"
            style={{ color: 'var(--ink-subtle)' }}
          >
            {description}
          </div>
        )}
      </div>
    </motion.div>
  );
}

// ── Chat card ─────────────────────────────────────────────────────

function ChatCard({
  toolName,
  status,
  description,
  result,
  error,
  durationMs,
  variant,
}: {
  toolName: string;
  status: ToolStatus;
  description: string;
  result: string | undefined;
  error: string | undefined;
  durationMs: number | undefined;
  variant: 'tool' | 'subagent';
}) {
  const [expanded, setExpanded] = useState(false);
  const canExpand = status !== 'running' && (result || error);

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18, ease: [0.2, 0.8, 0.2, 1] }}
      className="rounded-[var(--r-md)] border overflow-hidden"
      style={{
        borderColor: 'var(--line)',
        background: 'var(--canvas-elevated)',
      }}
    >
      {variant === 'subagent' && (
        <div
          className="flex items-center gap-1.5 px-3.5 pt-2 pb-0"
          style={{ color: 'var(--ink-subtle)' }}
        >
          <span
            className="inline-block w-1.5 h-1.5 rounded-full"
            style={{ background: 'var(--iris)' }}
          />
          <span
            className=" uppercase"
            style={{ fontSize: 12, letterSpacing: '0.14em' }}
          >
            Sub-agent
          </span>
        </div>
      )}
      <button
        type="button"
        onClick={() => canExpand && setExpanded((v) => !v)}
        disabled={!canExpand}
        className="flex items-center gap-2.5 w-full px-3.5 py-2.5 text-left transition-colors duration-150"
        style={{
          cursor: canExpand ? 'pointer' : 'default',
          background: 'transparent',
        }}
        onMouseEnter={(e) => {
          if (!canExpand) return;
          (e.currentTarget as HTMLElement).style.background = 'var(--canvas-hover)';
        }}
        onMouseLeave={(e) => {
          (e.currentTarget as HTMLElement).style.background = 'transparent';
        }}
      >
        <span
          className="flex items-center justify-center w-6 h-6 rounded-[var(--r-xs)] shrink-0"
          style={{
            background: 'var(--iris-soft)',
            color: 'var(--iris-hover)',
          }}
        >
          {getToolIcon(toolName)}
        </span>
        <span
          className="text-[13px] font-medium flex-1 truncate"
          style={{ color: 'var(--ink)' }}
        >
          {formatToolName(toolName)}
        </span>
        {durationMs !== undefined && (
          <span
            className="font-mono tabular-nums"
            style={{ fontSize: 12, color: 'var(--ink-faint)' }}
          >
            {(durationMs / 1000).toFixed(1)}s
          </span>
        )}
        <StatusGlyph status={status} />
      </button>

      {status === 'running' && description && (
        <div
          className="px-3.5 pb-3 text-[13px]"
          style={{ color: 'var(--ink-muted)' }}
        >
          {description}
        </div>
      )}

      {status !== 'running' && !expanded && (result || error) && (
        <div
          className="px-3.5 pb-3 text-[13px] truncate"
          style={{ color: error ? 'var(--danger)' : 'var(--ink-muted)' }}
        >
          {(error || result || '').slice(0, 140)}
          {(error || result || '').length > 140 ? '…' : ''}
        </div>
      )}

      {status !== 'running' && expanded && (result || error) && (
        <div
          className="px-3.5 pb-3 pt-2.5 text-[13px] whitespace-pre-wrap border-t"
          style={{
            color: error ? 'var(--danger)' : 'var(--ink-muted)',
            borderColor: 'var(--line)',
          }}
        >
          {error || result}
        </div>
      )}
    </motion.div>
  );
}

// ── Status glyph ──────────────────────────────────────────────────

function StatusGlyph({ status }: { status: ToolStatus }) {
  if (status === 'running') {
    return (
      <span
        className="w-1.5 h-1.5 rounded-full pulse-dot shrink-0"
        style={{ background: 'var(--iris)' }}
        aria-label="Running"
      />
    );
  }
  if (status === 'error') {
    return (
      <XIcon
        size={14}
        strokeWidth={1.75}
        style={{ color: 'var(--danger)' }}
        aria-label="Error"
      />
    );
  }
  return (
    <Check
      size={14}
      strokeWidth={1.75}
      style={{ color: 'var(--positive)' }}
      aria-label="Done"
    />
  );
}

// ── Icon / name helpers ───────────────────────────────────────────

function getToolIcon(toolName: string): React.ReactNode {
  const name = toolName.toLowerCase();
  const props = { size: 13, strokeWidth: 1.5 };
  if (name.includes('work_order') || name.includes('maintenance'))
    return <Wrench {...props} />;
  if (name.includes('gl') || name.includes('ledger') || name.includes('financial'))
    return <DollarSign {...props} />;
  if (
    name.includes('lease') ||
    name.includes('tenant') ||
    name.includes('rental') ||
    name.includes('buildium')
  )
    return <Home {...props} />;
  if (name.includes('browse') || name.includes('browser') || name.includes('web'))
    return <Globe {...props} />;
  if (name.includes('email') || name.includes('gmail')) return <Mail {...props} />;
  if (name.includes('calendar')) return <Calendar {...props} />;
  if (name.includes('memory')) return <Brain {...props} />;
  if (name.includes('confirm')) return <AlertCircle {...props} />;
  return <Cog {...props} />;
}

function formatToolName(toolName: string): string {
  return toolName
    .replace(/^buildium_/, 'Buildium: ')
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}
