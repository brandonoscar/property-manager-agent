.PHONY: install dev test lint format typecheck frontend backend remote-ui remote-ui-down seed smoke smoke-real conformance langfuse-smoke create-user ui-fuzz viewports

install:
	pip install -e ".[dev]"
	cd frontend && npm install

dev: backend

backend:
	uvicorn helixis.main:app --reload --host 0.0.0.0 --port 8000

frontend:
	cd frontend && npm run dev

test:
	pytest tests/ -v

lint:
	ruff check src/ tests/

format:
	ruff format src/ tests/

typecheck:
	mypy src/helixis/

# Seed demo data for the demo-company. Requires HELIXIS_SUPABASE__DB_URL
# and HELIXIS_SEED=1 (production safety guard).
seed:
	HELIXIS_SEED=1 python3 scripts/seed.py

# Portable off-Supabase DB backup — verified pg_dump into ./backups. Run from a
# machine/CI that can reach HELIXIS_SUPABASE__DB_URL. Restore + the full DR
# procedure live in docs/deployment/disaster-recovery.md.
backup:
	python3 scripts/backup.py

# End-to-end smoke test against the running backend. Requires
# HELIXIS_SMOKE_TOKEN; HELIXIS_SMOKE_BASE_URL defaults to localhost:8000.
smoke:
	python3 scripts/smoke.py

# Real-services smoke test — hits Buildium, Composio (Gmail/Calendar/Drive),
# Supabase Postgres, LiteLLM, and Langfuse directly via the in-process
# clients. Read-only except one idempotent Langfuse trace. Used by the
# daily GitHub Actions health-check job. See CLAUDE.md §4.
smoke-real:
	python -m helixis.smoke

# ADR 0003 contract conformance — probes the DEPLOYED backend's API surface
# (CORS for the wizard origin + unauthenticated endpoint surface). Read-only.
# Override HELIXIS_CONFORMANCE_BASE_URL for staging; set
# HELIXIS_CONFORMANCE_TOKEN to also run the authenticated bootstrap probe.
# See docs/operations/conformance-check.md.
conformance:
	python -m helixis.conformance

# UI-fuzz sweep (keyless). Boots the frontend in demo mode, injects varied
# edge-case inbox rows via ?fuzz=<seed>, and Playwright-walks every page +
# card looking for render breakage (thrown errors, missing cards, empty
# lists). No backend, no Buildium, no LLM key. Exits non-zero on a real
# render failure; the last line is `[ui-fuzz] RESULT=PASS|FAIL ...`.
# Screenshots for any failing seed land in frontend/fuzz/shots/.
# Orchestration (boot server, run, tear down) lives in the script so the
# server is always killed even when the sweep fails.
ui-fuzz:
	bash frontend/fuzz/sweep.sh

# Viewport audit (keyless). Boots the frontend in demo mode and measures, in a
# real browser at phone / landscape / tablet / desktop, two things a user
# feels: does the document scroll sideways, and is any on-screen control under
# the 24x24 WCAG 2.2 AA target minimum. jsdom cannot answer either — it has no
# layout engine, so every element measures zero and passes. Exit 1 on any
# finding; the last line is `[viewports] RESULT=PASS|FAIL ...`.
viewports:
	bash frontend/fuzz/viewport-sweep.sh

# Langfuse + LiteLLM round-trip smoke. Requires HELIXIS_LANGFUSE__*
# env vars (and an LLM provider key like HELIXIS_LLM__GOOGLE_API_KEY).
# Issues one cheap LLM call, flushes, and prints the dashboard URL so
# you can click through and confirm the trace landed.
langfuse-smoke:
	python3 -m pytest tests/integration/test_langfuse_smoke.py -v -m integration -s

# Create a real Helixis user account on the production Supabase project.
# Idempotent — safe to re-run with the same inputs. Requires
# HELIXIS_SUPABASE__{URL,SERVICE_KEY,DB_URL} in env.
#
# Pass args after `--`, e.g.:
#   make create-user ARGS="--email you@x.com --password 'pw' \\
#                          --company-id acme --company-name 'Acme'"
create-user:
	python3 scripts/create_user.py $(ARGS)

# Open the UI remotely via browser-based VNC (noVNC) at http://<host>:6080
# Password: helixis (set in docker-compose.yml)
remote-ui:
	docker compose --profile vnc up -d
	@echo "UI available at: http://localhost:6080/  (password: helixis)"

remote-ui-down:
	docker compose --profile vnc down
