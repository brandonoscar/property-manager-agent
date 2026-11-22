"""Real-services smoke test for Helixis.

`python -m helixis.smoke` hits every external service the backend
depends on (Buildium, Composio toolkits, Supabase Postgres, LiteLLM,
Langfuse) in parallel and exits 0/1 for CI gating.

All checks are read-only EXCEPT the Langfuse check, which emits a
single idempotent test trace tagged ``smoke_test`` — that's an
observability write and is intentional.

See CLAUDE.md §4 "Real-services smoke test" for invocation patterns.
"""
