/**
 * Client for ``/api/v1/dev/runs`` — orchestrator-run telemetry
 * inspection page (PR #141, Trust series #1).
 *
 * The backend returns 404 when ``settings.dev.show_runs_page`` is
 * off, so a quiet "not enabled" branch in the page UI mirrors the
 * same shape. No retry, no caching at this layer — the dev page is
 * a manual-refresh debugging tool.
 */

import { apiFetch } from './api';

export interface OrchestratorToolCall {
  name: string;
  args?: unknown;
  output?: unknown;
  started_at?: string;
  duration_ms?: number;
  error?: string | null;
  orphaned?: boolean;
}

export interface OrchestratorHandoff {
  from_agent: string;
  to_agent: string;
  at: string;
}

export interface OrchestratorRun {
  id: string;
  company_id: string;
  user_id: string | null;
  session_id: string;
  source: string;
  user_query: string;
  started_at: string;
  completed_at: string | null;
  latency_ms: number | null;
  status: 'running' | 'succeeded' | 'failed' | 'cancelled';
  error_message: string | null;
  error_type: string | null;
  tool_calls: OrchestratorToolCall[];
  final_text: string | null;
  handoffs: OrchestratorHandoff[];
  raw: Record<string, unknown> | null;
}

interface ListRunsResponse {
  runs: OrchestratorRun[];
  count: number;
}

export class DevRunsDisabledError extends Error {
  constructor() {
    super('Dev runs page is disabled on this deployment.');
    this.name = 'DevRunsDisabledError';
  }
}

/**
 * Fetch the most recent ``limit`` orchestrator runs for the
 * authenticated user's company. Throws ``DevRunsDisabledError`` when
 * the route 404s — that's the backend's way of saying the feature
 * flag is off, distinct from a real "not found" on a single run.
 */
export async function listRuns(limit = 100): Promise<OrchestratorRun[]> {
  const res = await apiFetch(`/api/v1/dev/runs?limit=${limit}`);
  if (res.status === 404) throw new DevRunsDisabledError();
  if (!res.ok) {
    throw new Error(`Failed to fetch runs: HTTP ${res.status}`);
  }
  const data = (await res.json()) as ListRunsResponse;
  return data.runs;
}

/**
 * Fetch a single run by id. Returns ``null`` for missing or
 * cross-tenant ids (the backend treats both as 404 to avoid leaking
 * existence). Throws ``DevRunsDisabledError`` when the feature flag
 * is off — the route's 404 in that case applies to the whole prefix,
 * which we surface differently than a per-row 404.
 */
export async function getRun(runId: string): Promise<OrchestratorRun | null> {
  const res = await apiFetch(`/api/v1/dev/runs/${runId}`);
  if (res.status === 404) {
    // Ambiguous: either the flag is off OR the row doesn't exist.
    // Caller decides — DevRunsDetail surfaces a single "not found"
    // message either way since the user-visible distinction doesn't
    // matter for a debugging tool.
    return null;
  }
  if (!res.ok) {
    throw new Error(`Failed to fetch run: HTTP ${res.status}`);
  }
  return (await res.json()) as OrchestratorRun;
}
