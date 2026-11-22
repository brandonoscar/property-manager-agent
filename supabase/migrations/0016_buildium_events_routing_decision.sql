-- 0016_buildium_events_routing_decision.sql
--
-- PR #82 — persist the deterministic ``RoutingDecision`` produced by
-- ``helixis.webhooks.routing.route_event`` alongside the existing
-- enrichment + action_surface columns. The Inbox card reads
-- ``routing_decision.assigned_buildium_user_id`` to render the
-- assignee; the historical-row backfill endpoint uses the column to
-- record decisions for events that landed before PR #82's receiver
-- wiring.
--
-- Idempotent (``add column if not exists`` / ``create index if not
-- exists``) so re-applying on an already-patched environment is a
-- no-op. No RLS changes — ``buildium_events`` inherits per-company
-- scoping from the existing policies added in 0007.

alter table public.buildium_events
    add column if not exists routing_decision jsonb;

-- Partial functional index on the assignee — supports the inbox query
-- ``WHERE company_id = $1 AND routing_decision->>'assigned_buildium_user_id' = $2``
-- without scanning the JSONB blob on every row.
create index if not exists buildium_events_routing_assignee_idx
    on public.buildium_events ((routing_decision->>'assigned_buildium_user_id'))
    where routing_decision is not null;
