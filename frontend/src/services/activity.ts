/**
 * Team Activity data layer (PR #70 rewrite).
 *
 * Source of truth: ``GET /api/v1/activity`` — every row in
 * ``helixis_activity_log`` for the user's company, joined to
 * ``auth.users`` for the actor's display name. No demo seed; demo
 * mode renders an explicit "connect Buildium" empty state.
 */

import { apiFetch } from './api';

export type ActivityScope = 'team' | 'mine';

/** Action bucket for the category filter. Mirrors the backend
 *  ``ActivityCategory`` allow-list. ``all`` applies no filter. */
export type ActivityCategory =
  | 'all'
  | 'crm'
  | 'buildium'
  | 'assistant'
  | 'team'
  | 'scheduled';

/** Actor block on each row — the user that produced the action. */
export interface ActivityActor {
  id: string;
  name: string | null;
  email: string | null;
  /** True when this row was produced by the currently authenticated
   *  user. Frontend uses this to prefix "You" instead of the name. */
  is_self: boolean;
}

/** One row in the feed. Mirrors the backend ``ActivityRow``. */
export interface ActivityRow {
  id: string;
  actor: ActivityActor;
  source: string;
  source_label: string | null;
  action_type: string;
  description: string;
  property: string | null;
  tenant: string | null;
  /** ISO8601 timestamp. */
  created_at: string;
}

export interface ActivityPage {
  rows: ActivityRow[];
  /** ISO timestamp of the last row on this page, or ``null`` when no
   *  further pages remain. Caller passes this as ``cursor`` on the
   *  next request. */
  next_cursor: string | null;
}

// ── Legacy types ────────────────────────────────────────────────
//
// PR #70 rewrote the Activity feed but the Chat surface (out of scope
// per the brief) still imports ``ActivityEntry`` / ``ActivitySource``
// from this module via ``stores/activityStore.ts``. We keep the names
// alive as deprecated aliases so ChatThread compiles. The store no
// longer receives writes from anywhere — ChatThread renders an empty
// activity list until PR #71 rewires it.

export type ActivitySource = 'webhook' | 'chat' | 'scheduled' | 'manual';

export interface ActivityUser {
  id: string;
  name: string;
  email: string;
}

/** @deprecated Use ``ActivityRow`` for new code. Kept for the legacy
 *  ``activityStore`` consumed by ChatThread, which will be rewritten
 *  in PR #71. */
export interface ActivityEntry {
  id: string;
  user: ActivityUser;
  actionType: string;
  description: string;
  source: ActivitySource;
  sourceLabel: string;
  property?: string;
  tenant?: string;
  timestamp: number;
}

const DEFAULT_LIMIT = 50;

/** Filters passed to the feed. ``actor`` is a teammate ``user_id`` (the
 *  person filter); omit for everyone. ``category`` narrows the action
 *  bucket. */
export interface ActivityFilters {
  scope?: ActivityScope;
  category?: ActivityCategory;
  actor?: string | null;
}

/**
 * Fetch one page of activity. ``cursor`` paginates older rows; pass
 * ``null`` for the first page.
 */
export async function fetchActivity(
  filters: ActivityFilters = {},
  cursor: string | null = null,
  limit: number = DEFAULT_LIMIT,
): Promise<ActivityPage> {
  const params = new URLSearchParams();
  params.set('scope', filters.scope ?? 'team');
  if (filters.category && filters.category !== 'all') {
    params.set('category', filters.category);
  }
  if (filters.actor) params.set('actor', filters.actor);
  params.set('limit', String(limit));
  if (cursor) params.set('cursor', cursor);
  const res = await apiFetch(`/api/v1/activity?${params.toString()}`);
  return (await res.json()) as ActivityPage;
}

/**
 * The distinct people who have activity in the company — powers the
 * person filter dropdown. Returns actors sorted by display name with the
 * requester flagged ``is_self``.
 */
export async function fetchActivityActors(): Promise<ActivityActor[]> {
  const res = await apiFetch('/api/v1/activity/actors');
  return (await res.json()) as ActivityActor[];
}
