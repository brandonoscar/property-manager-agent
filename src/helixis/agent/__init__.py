"""Phase A.1 — Agent enrichment + planner subsystem.

Two services live here. Both run synchronously inside the Buildium
webhook receiver after the inbound row is persisted to
``buildium_events``:

* :mod:`helixis.agent.enrichment` — fan-out Buildium reads that turn
  the bare webhook envelope into a rich ``EnrichedEvent`` (the task
  itself, the unit, the active lease, the tenants on it, 90 days of
  task history at the unit and at the property).
* :mod:`helixis.agent.planner` — feeds the enriched event into the
  reasoning LLM with a curated tool catalogue and asks for an
  ``ActionSurface`` (a short plain-English summary plus 1-4 concrete
  human-confirmable action buttons).

Read-side only: this PR never writes back to Buildium. Action
execution is Phase A.2.
"""
