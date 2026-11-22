/**
 * Small UI atoms shared across connector detail panels.
 * Kept narrow on purpose — these don't belong in the global UI kit
 * because they encode connector-page-specific layout choices.
 */

import { useState, type ReactNode } from 'react';
import { Check, Copy } from 'lucide-react';
import { toast } from 'sonner';

export function Field({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label
        className="text-[12px] font-medium uppercase tracking-wider"
        style={{ color: 'var(--ink-muted)' }}
      >
        {label}
      </label>
      {children}
    </div>
  );
}

export function Card({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div
      className="rounded-[var(--r-md)] p-4 border"
      style={{
        background: 'var(--canvas)',
        borderColor: 'var(--card-edge)',
      }}
    >
      <div
        className="text-[12px] font-medium uppercase tracking-wider mb-2"
        style={{ color: 'var(--ink-muted)' }}
      >
        {title}
      </div>
      {children}
    </div>
  );
}

export function CopyRow({ value, mono = false }: { value: string; mono?: boolean }) {
  const [copied, setCopied] = useState(false);

  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      toast.error('Clipboard not available — copy manually.');
    }
  };

  return (
    <div
      className="flex items-center gap-2 px-3 py-2 rounded-[var(--r-sm)] border overflow-hidden"
      style={{ background: 'var(--canvas)', borderColor: 'var(--line)' }}
    >
      <code
        className={`flex-1 truncate text-[13px] ${mono ? 'font-mono' : ''}`}
        style={{ color: 'var(--ink)' }}
      >
        {value}
      </code>
      <button
        type="button"
        onClick={onCopy}
        className="p-1.5 rounded-[var(--r-sm)] hover:bg-[var(--canvas-elevated)] flex items-center gap-1 text-[12px]"
        style={{ color: 'var(--ink-muted)' }}
      >
        {copied ? <Check size={12} /> : <Copy size={12} />}
        {copied ? 'Copied' : 'Copy'}
      </button>
    </div>
  );
}

export function StatusBadge({ status }: { status: 'pending' | 'running' | 'done' | 'failed' }) {
  const styles: Record<typeof status, { bg: string; fg: string }> = {
    pending: { bg: 'var(--canvas-raised)', fg: 'var(--ink-muted)' },
    running: { bg: 'var(--canvas-raised)', fg: 'var(--ink)' },
    done: { bg: 'var(--positive-bg)', fg: 'var(--positive)' },
    failed: { bg: 'var(--negative-bg)', fg: 'var(--negative)' },
  };
  const s = styles[status];
  return (
    <span
      className=" uppercase px-1.5 py-0.5 rounded-[var(--r-xs)]"
      style={{
        fontSize: 12,
        letterSpacing: '0.14em',
        background: s.bg,
        color: s.fg,
      }}
    >
      {status}
    </span>
  );
}

/**
 * A relative timestamp that can still tell you exactly when.
 *
 * ⚠ "3h ago" is the right default and the wrong only answer. Somebody
 * correlating a webhook against a Buildium record, or working out whether a
 * sync ran before or after a change they made, needs the actual time — and
 * relative text alone means opening a database to get it. The exact stamp goes
 * on `title`, which costs nothing and is what every other relative timestamp in
 * this app already does.
 *
 * Rendering it here rather than at the call site is deliberate: `timeAgo` had
 * exactly one caller and no title, so the next caller would have inherited the
 * same omission. A component carries the pair.
 */
export function RelativeTime({ iso }: { iso: string }) {
  const exact = new Date(iso);
  return (
    <span title={Number.isNaN(exact.getTime()) ? undefined : exact.toLocaleString()}>
      {timeAgo(iso)}
    </span>
  );
}

export function timeAgo(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 60_000) return `${Math.floor(ms / 1000)}s ago`;
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m ago`;
  if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)}h ago`;
  return `${Math.floor(ms / 86_400_000)}d ago`;
}
