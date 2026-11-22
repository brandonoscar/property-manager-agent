# Occupella

A chat assistant for property managers. One chat box routes each request to a
specialist agent that knows one system: Buildium, Rentvine, Rent Manager,
Propertyware, DoorLoop, Gmail, Google Calendar, Google Drive, documents, the
web, or a real browser. Anything that writes to a property management system
waits for a person to approve it.

This repository is the self-hosted edition. There is no shared backend and no
shared account: **you bring your own keys** (Anthropic, Supabase, and whichever
integrations you turn on), your own Supabase project, and your own Render (or
Docker) host. Each property management company then pastes its own PMS API
keys into the app, where they are stored encrypted.

The package is still called `helixis`, the product's first name. It appears in
imports, environment variables and table names. Code comments cite "gotcha N"
and `docs/` notes from the private development repository. Those notes are not
included here. The comment around each citation says what went wrong.

## How it fits together

```
 React 19 + Vite web app  ──/api──▶  FastAPI (Render web service)
        │                               │  orchestrator → specialist agents
        │ Supabase Auth (sign-in)       │  OpenAI Agents SDK + LiteLLM → Anthropic
        ▼                               ▼
   your Supabase project  ◀──────  Postgres: tenants, PMS mirror, chat, memory (pgvector)
                                        ▲
                         webhook worker (Render background worker):
                         PMS webhooks, scheduled syncs, detectors
```

- `src/helixis/agents/` the orchestrator and specialists
- `src/helixis/buildium/`, `rentvine/`, `rentmanager/`, `propertyware/`, `doorloop/` one connector each: client, mapper, sync
- `src/helixis/pms/` the one gate every PMS write goes through
- `src/helixis/api/` routes, auth, middleware
- `src/helixis/webhooks/` receiver and worker
- `src/helixis/evals/` the eval harness and golden cases
- `supabase/migrations/` schema and row-level security, applied in order
- `frontend/` the web app

## Things worth reading in the code

- **Writes fail closed.** A PMS write waits for a person to approve a confirm
  card. Then the record is re-read (and refused if it changed), sent once, and
  read back to check it landed (`src/helixis/pms/gated_write.py`).
- **Tenant isolation is tested two ways.** Every query on a `helixis_*` table
  carries `company_id`, Postgres row-level security backs it, and
  `scripts/rls_guard.py` fails CI when a table ships with row-level security
  off.
- **Credentials never reach a log.** PMS keys are encrypted at rest with a
  Fernet key, settings objects hide secrets from `repr`, and the Buildium
  client masks its own credentials in any error a server echoes back
  (`tests/unit/test_buildium_no_secrets_in_errors.py`).
- **Agent loops are bounded in code**, with tool caps and token budgets, not
  with prompt wording.
- **Evals measure what the user sees.** Golden cases score the reply after
  scrubbing, and keep safety, quality and infrastructure failures apart.
- **Guards read the AST.** Tests that enforce a rule walk the syntax tree
  instead of matching source text, so a comment that quotes the rule can't
  trip them.

## Run it locally

You need Python 3.11, Node 20, and a Supabase project of your own.

```bash
cp .env.example .env               # fill in your keys (see SELF-HOSTING.md)
cp frontend/.env.example frontend/.env
make install                       # Python and frontend dependencies
# apply the schema to your Supabase project: SELF-HOSTING.md, step 2
make backend                       # FastAPI on :8000
make frontend                      # Vite on :5173
```

`make seed` (with `HELIXIS_SEED=1`) loads a demo company with a small
portfolio so the chat has something to read.

## Deploy your own

[SELF-HOSTING.md](SELF-HOSTING.md) walks through it: a Supabase project, the
Render Blueprint in `render.yaml` (API plus webhook worker), and the frontend on
Vercel or any static host.

## Tests

```bash
ruff check src/ tests/ && ruff format --check src/ tests/
mypy src/helixis/
python -m pytest tests/
cd frontend && ./node_modules/.bin/tsc --noEmit && npx vitest run
```

The model-driven eval cases skip cleanly unless `HELIXIS_LLM__ANTHROPIC_API_KEY`
is set. Run one with `python -m helixis.evals.runner --case 062`. The connector
contract tests compare the code against vendor API specs that this repository
doesn't redistribute. Those tests report as skipped and name the file they
needed (`NOT_PUBLISHED` in `tests/conftest.py`).

## Property management APIs

Each connector calls the vendor's API with the keys a company gives it. Those
vendors' API terms bind whoever connects. Read them before you turn a connector
on. The deployment switches (`HELIXIS_RENTVINE__ENABLED` and the rest) are off
by default. Buildium is always available.

## License

[GNU AGPL-3.0](LICENSE). You can run, change and share it. If you run a
modified version as a service for other people, you must offer them its source
code. The Occupella name and logo are not licensed for use by other products.
