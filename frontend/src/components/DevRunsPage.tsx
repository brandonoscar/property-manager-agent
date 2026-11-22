/**
 * Dev /runs inspection page (PR #141, Trust series #1).
 *
 * Brutalist on purpose — this is a debugging tool, not a product
 * surface. Renders the last 100 orchestrator invocations with their
 * tools / handoffs / errors / final text, gated server-side by
 * ``settings.dev.show_runs_page``. The page itself just consumes the
 * shape; the 404-when-disabled branch is rendered as an EmptyState.
 *
 * Reachable only via the command palette ("Dev: Orchestrator runs")
 * or a direct ``setView('dev-runs')`` call. Hidden from the sidebar.
 */

import { useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';

import { Badge, type BadgeTone } from './Badge';
import { Button } from './Button';
import { EmptyState } from './EmptyState';
import { Skeleton } from './Skeleton';
import {
  DevRunsDisabledError,
  listRuns,
  type OrchestratorRun,
} from '../services/devRuns';

type ViewState =
  | { kind: 'loading' }
  | { kind: 'disabled' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; runs: OrchestratorRun[] };

const STATUS_TONE: Record<OrchestratorRun['status'], BadgeTone> = {
  running: 'info',
  succeeded: 'positive',
  failed: 'danger',
  cancelled: 'caution',
};

function formatLatency(ms: number | null): string {
  if (ms === null || ms === undefined) return '—';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function formatRelative(iso: string): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return iso;
  const delta = Date.now() - t;
  if (delta < 60_000) return `${Math.max(1, Math.floor(delta / 1000))}s ago`;
  if (delta < 3_600_000) return `${Math.floor(delta / 60_000)}m ago`;
  if (delta < 86_400_000) return `${Math.floor(delta / 3_600_000)}h ago`;
  return `${Math.floor(delta / 86_400_000)}d ago`;
}

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max - 1)}…`;
}

export function DevRunsPage() {
  const [state, setState] = useState<ViewState>({ kind: 'loading' });
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const load = async () => {
    setState({ kind: 'loading' });
    try {
      const runs = await listRuns(100);
      setState({ kind: 'ready', runs });
    } catch (err) {
      if (err instanceof DevRunsDisabledError) {
        setState({ kind: 'disabled' });
        return;
      }
      const message = err instanceof Error ? err.message : 'Failed to load runs.';
      setState({ kind: 'error', message });
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const toggleExpand = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <div className="min-h-full px-6 py-6" data-testid="dev-runs-page">
      <div className="flex items-baseline justify-between mb-4">
        <div>
          <h1
            className="font-semibold"
            style={{ color: 'var(--ink)', fontSize: 'var(--text-display)' }}
          >
            Runs
          </h1>
          <div
            className="mt-1"
            style={{ color: 'var(--ink-muted)', fontSize: 'var(--text-body)' }}
          >
            Last 100 orchestrator invocations.{' '}
            {state.kind === 'ready' ? `${state.runs.length} loaded.` : null}
          </div>
        </div>
        <Button variant="secondary" size="sm" onClick={() => void load()}>
          Refresh
        </Button>
      </div>

      {state.kind === 'loading' && (
        <div className="flex flex-col gap-2" data-testid="dev-runs-loading">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} variant="rect" />
          ))}
        </div>
      )}

      {state.kind === 'disabled' && (
        <EmptyState
          title="Dev runs page is disabled"
          body="Set HELIXIS_DEV__SHOW_RUNS_PAGE=true on the backend to enable this view. In production this stays off until explicitly toggled."
          data-testid="dev-runs-disabled"
        />
      )}

      {state.kind === 'error' && (
        <EmptyState
          title="Failed to load runs"
          body={state.message}
          ctaLabel="Try again"
          onCta={() => void load()}
          data-testid="dev-runs-error"
        />
      )}

      {state.kind === 'ready' && state.runs.length === 0 && (
        <EmptyState
          title="No runs yet"
          body="Once the orchestrator processes its first invocation, it'll show up here."
          data-testid="dev-runs-empty"
        />
      )}

      {state.kind === 'ready' && state.runs.length > 0 && (
        <RunsTable
          runs={state.runs}
          expanded={expanded}
          onToggle={toggleExpand}
        />
      )}

      <SentryTestPanel />
    </div>
  );
}

/**
 * Sentry verification panel — throws an Error from a click handler so
 * we can confirm frontend Sentry init is reaching the dashboard. Lives
 * on the dev page (gated server-side by ``settings.dev.show_runs_page``)
 * because only operators care about exercising the pipeline. The error
 * happens inside a deferred timeout so it escapes React's synthetic-
 * event handler and lands in the browser's global error path, which is
 * what Sentry's global listener captures.
 *
 * ``import.meta.env.DEV`` is a build-time check from Vite — when the
 * production bundle is built, ``DEV`` is statically ``false`` and the
 * panel body is tree-shaken out of the output. Belt-and-suspenders on
 * top of the server-side ``show_runs_page`` gate: even if the page's
 * gating logic ever changes, the test button literally cannot exist
 * in the production bundle.
 */
function SentryTestPanel() {
  if (!import.meta.env.DEV) return null;
  return (
    <div
      className="mt-12 border-t pt-6"
      style={{ borderColor: 'var(--ink-faint)' }}
      data-testid="dev-runs-sentry-panel"
    >
      <h2
        className="font-semibold mb-2"
        style={{ color: 'var(--ink)', fontSize: 'var(--text-body-lg)' }}
      >
        Sentry test
      </h2>
      <p
        className="mb-3"
        style={{ color: 'var(--ink-muted)', fontSize: 'var(--text-body)' }}
      >
        Throws an Error from a click handler. If Sentry is configured
        (VITE_SENTRY_DSN set), the event lands in the Sentry dashboard
        within a few seconds. Safe to click — the error escapes React's
        event handling via setTimeout and does not unmount the app.
      </p>
      <Button
        variant="secondary"
        size="sm"
        onClick={() => {
          setTimeout(() => {
            throw new Error(
              'Sentry test exception (intentional, from /dev page button)',
            );
          }, 0);
        }}
      >
        Throw test error
      </Button>
    </div>
  );
}

interface RunsTableProps {
  runs: OrchestratorRun[];
  expanded: Set<string>;
  onToggle: (id: string) => void;
}

function RunsTable({ runs, expanded, onToggle }: RunsTableProps) {
  return (
    <div
      className="rounded border overflow-hidden"
      style={{ borderColor: 'var(--line)' }}
      data-testid="dev-runs-table"
    >
      <table className="w-full" style={{ borderCollapse: 'collapse' }}>
        <thead>
          <tr
            className="text-left"
            style={{
              background: 'var(--canvas-elevated)',
              borderBottom: '1px solid var(--line)',
              color: 'var(--ink-subtle)',
              fontSize: 12,
              letterSpacing: '0.06em',
              textTransform: 'uppercase',
            }}
          >
            <th className="py-2 px-3 w-[110px]">Status</th>
            <th className="py-2 px-3 w-[110px]">When</th>
            <th className="py-2 px-3 w-[110px]">Source</th>
            <th className="py-2 px-3">Query</th>
            <th className="py-2 px-3 w-[80px] text-right">Tools</th>
            <th className="py-2 px-3 w-[80px] text-right">Latency</th>
            <th className="py-2 px-3 w-[120px]">Error</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <RunRow
              key={run.id}
              run={run}
              isExpanded={expanded.has(run.id)}
              onToggle={() => onToggle(run.id)}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

interface RunRowProps {
  run: OrchestratorRun;
  isExpanded: boolean;
  onToggle: () => void;
}

function RunRow({ run, isExpanded, onToggle }: RunRowProps) {
  const tone = STATUS_TONE[run.status] ?? 'neutral';
  const toolCount = run.tool_calls.length;
  return (
    <>
      <tr
        onClick={onToggle}
        // Same reason as OpsTable: without these the row expands on click and
        // is unreachable by keyboard entirely.
        tabIndex={0}
        role="button"
        aria-expanded={isExpanded}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            onToggle();
          }
        }}
        className="cursor-pointer"
        style={{
          borderBottom: '1px solid var(--line)',
          background: isExpanded ? 'var(--canvas-elevated)' : 'transparent',
        }}
        data-testid={`dev-run-row-${run.id}`}
      >
        <td className="py-2 px-3">
          <Badge tone={tone}>{run.status}</Badge>
        </td>
        <td
          className="py-2 px-3"
          style={{ color: 'var(--ink-muted)', fontSize: 12 }}
          title={run.started_at}
        >
          {formatRelative(run.started_at)}
        </td>
        <td className="py-2 px-3">
          <Badge tone="neutral">{run.source}</Badge>
        </td>
        <td
          className="py-2 px-3"
          style={{ color: 'var(--ink)', fontSize: 13 }}
        >
          {truncate(run.user_query, 80)}
        </td>
        <td
          className="py-2 px-3 text-right tabular-nums"
          style={{ color: 'var(--ink-muted)', fontSize: 12 }}
        >
          {toolCount}
        </td>
        <td
          className="py-2 px-3 text-right tabular-nums"
          style={{ color: 'var(--ink-muted)', fontSize: 12 }}
        >
          {formatLatency(run.latency_ms)}
        </td>
        <td className="py-2 px-3">
          {run.error_type ? (
            <Badge tone="danger" title={run.error_message ?? undefined}>
              {run.error_type}
            </Badge>
          ) : (
            <span style={{ color: 'var(--ink-subtle)' }}>—</span>
          )}
        </td>
      </tr>
      {isExpanded && <RunDetail run={run} />}
    </>
  );
}

function RunDetail({ run }: { run: OrchestratorRun }) {
  const copyAsJson = () => {
    const text = JSON.stringify(run, null, 2);
    void navigator.clipboard?.writeText(text);
  };

  const jsonString = useMemo(() => JSON.stringify(run, null, 2), [run]);

  return (
    <tr style={{ borderBottom: '1px solid var(--line)' }}>
      <td colSpan={7} className="px-3 py-4" style={{ background: 'var(--canvas)' }}>
        <div className="flex items-center justify-between mb-3">
          <div style={{ fontSize: 12, color: 'var(--ink-subtle)' }}>
            run_id <code>{run.id}</code> · session_id{' '}
            <code>{run.session_id}</code>
          </div>
          <Button variant="secondary" size="sm" onClick={copyAsJson}>
            Copy as JSON
          </Button>
        </div>

        {run.handoffs.length > 0 && (
          <div className="mb-3">
            <SectionHeader>Handoffs</SectionHeader>
            <ul className="text-sm space-y-1">
              {run.handoffs.map((h, i) => (
                <li key={i} style={{ color: 'var(--ink-muted)' }}>
                  <code>{h.from_agent}</code> →{' '}
                  <code>{h.to_agent}</code>{' '}
                  <span style={{ color: 'var(--ink-subtle)' }}>
                    {formatRelative(h.at)}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="mb-3">
          <SectionHeader>Tool calls ({run.tool_calls.length})</SectionHeader>
          {run.tool_calls.length === 0 ? (
            <div style={{ color: 'var(--ink-subtle)', fontSize: 12 }}>
              No tool calls.
            </div>
          ) : (
            <ol className="space-y-3">
              {run.tool_calls.map((tc, i) => (
                <li
                  key={i}
                  className="rounded p-3"
                  style={{
                    background: 'var(--canvas-elevated)',
                    border: '1px solid var(--line)',
                  }}
                >
                  <div className="flex items-baseline gap-2 mb-2">
                    <code style={{ fontSize: 12, color: 'var(--ink)' }}>
                      {i + 1}. {tc.name}
                    </code>
                    <span
                      style={{ color: 'var(--ink-subtle)', fontSize: 12 }}
                    >
                      {tc.duration_ms !== undefined
                        ? `${tc.duration_ms}ms`
                        : 'no duration'}
                    </span>
                    {tc.error ? (
                      <Badge tone="danger" title={tc.error}>
                        error
                      </Badge>
                    ) : null}
                    {tc.orphaned ? (
                      <Badge tone="caution" title="No matching tool_called event">
                        orphaned
                      </Badge>
                    ) : null}
                  </div>
                  <Block label="args" value={tc.args} />
                  <Block label="output" value={tc.output} />
                </li>
              ))}
            </ol>
          )}
        </div>

        {run.final_text && (
          <div className="mb-3">
            <SectionHeader>Final text</SectionHeader>
            <pre
              className="rounded p-3 overflow-x-auto whitespace-pre-wrap"
              style={{
                background: 'var(--canvas-elevated)',
                border: '1px solid var(--line)',
                color: 'var(--ink)',
                fontSize: 12,
                fontFamily: 'ui-monospace, monospace',
              }}
            >
              {run.final_text}
            </pre>
          </div>
        )}

        {run.error_message && (
          <div className="mb-3">
            <SectionHeader>Error</SectionHeader>
            <pre
              className="rounded p-3 overflow-x-auto whitespace-pre-wrap"
              style={{
                background: 'var(--danger-bg)',
                border: '1px solid var(--danger)',
                color: 'var(--danger)',
                fontSize: 12,
                fontFamily: 'ui-monospace, monospace',
              }}
            >
              {run.error_message}
            </pre>
          </div>
        )}

        <details>
          <summary
            style={{
              cursor: 'pointer',
              fontSize: 12,
              letterSpacing: '0.06em',
              textTransform: 'uppercase',
              color: 'var(--ink-subtle)',
            }}
          >
            Raw row
          </summary>
          <pre
            className="rounded p-3 mt-2 overflow-x-auto"
            style={{
              background: 'var(--canvas-elevated)',
              border: '1px solid var(--line)',
              color: 'var(--ink-muted)',
              fontSize: 12,
              fontFamily: 'ui-monospace, monospace',
            }}
          >
            {jsonString}
          </pre>
        </details>
      </td>
    </tr>
  );
}

function SectionHeader({ children }: { children: ReactNode }) {
  return (
    <div
      className="mb-1"
      style={{
        fontSize: 12,
        letterSpacing: '0.06em',
        textTransform: 'uppercase',
        color: 'var(--ink-subtle)',
      }}
    >
      {children}
    </div>
  );
}

function Block({ label, value }: { label: string; value: unknown }) {
  if (value === undefined || value === null) return null;
  const text =
    typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  return (
    <div className="mb-1">
      <div
        style={{
          fontSize: 12,
          letterSpacing: '0.05em',
          color: 'var(--ink-subtle)',
        }}
      >
        {label}
      </div>
      <pre
        className="overflow-x-auto whitespace-pre-wrap"
        style={{
          color: 'var(--ink-muted)',
          fontSize: 12,
          fontFamily: 'ui-monospace, monospace',
        }}
      >
        {text}
      </pre>
    </div>
  );
}
