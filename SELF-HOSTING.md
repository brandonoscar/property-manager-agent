# Self-hosting Occupella

You run three pieces, all on your own accounts:

| Piece | Where | What it needs from you |
|---|---|---|
| Database and sign-in | a Supabase project | the project's URL, keys and database URL |
| API and webhook worker | Render (the `render.yaml` Blueprint), or any Docker host | your API keys as environment variables |
| Web app | Vercel, or any static host | your Supabase URL and anon key, and your API's URL |

## The keys

**Required:**

| Variable | From | Used for |
|---|---|---|
| `HELIXIS_LLM__ANTHROPIC_API_KEY` | console.anthropic.com | every chat turn (the default model is Anthropic's) |
| `HELIXIS_SUPABASE__URL`, `__ANON_KEY`, `__SERVICE_KEY` | Supabase → Project Settings → API | sign-in and the backend's admin calls |
| `HELIXIS_SUPABASE__DB_URL` | Supabase → Connect → connection string | the backend's Postgres pool |
| `HELIXIS_DATA_KEY` | you generate it (below) | encrypting each company's PMS keys |

Generate the data key once and store it somewhere safe. There is no
re-encrypt path: if it changes, every saved PMS key becomes unreadable and each
company has to paste its keys again.

```bash
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

**Optional.** Leave a key blank and that feature stays off:

| Variable | Turns on |
|---|---|
| `HELIXIS_LLM__VOYAGE_API_KEY` | long-term memory (embeddings). Or set `HELIXIS_MEMORY__ENABLED=false` |
| `HELIXIS_COMPOSIO__API_KEY` | Gmail, Google Calendar and Google Drive (each user connects their own account) |
| `HELIXIS_BROWSER__BROWSERBASE_API_KEY`, `__PROJECT_ID` | the browser agent |
| `HELIXIS_TWILIO__*` | SMS and calls |
| `HELIXIS_BILLING__*` | Stripe billing (`HELIXIS_BILLING__ENFORCE=false` leaves it off) |
| `HELIXIS_LANGFUSE__*`, `HELIXIS_POSTHOG__*`, `HELIXIS_SENTRY__*` | tracing, product analytics, error reports |
| `HELIXIS_LLM__OPENAI_API_KEY` | only if you point a model at `openai/...` |
| `HELIXIS_RENTVINE__ENABLED` and the other connector switches | the Rentvine, Rent Manager, Propertyware and DoorLoop connectors |

`.env.example` lists every variable with a comment.

Property management keys are not deployment variables. Each company signs in
and pastes its own Buildium, Rentvine, Rent Manager, Propertyware or DoorLoop
keys on the Connectors page. Buildium's base URL defaults to its sandbox. Set
`HELIXIS_BUILDIUM__BASE_URL=https://api.buildium.com` for real accounts.

## 1. Create the Supabase project

1. Create a project at supabase.com.
2. Authentication → URL Configuration: set the Site URL to your web app's
   address and add it to the redirect URLs.
3. Authentication → Email Templates: paste in the branded templates from
   `docs/deployment/auth-emails/` (its README maps each file to a template).
   The sign-up screen asks for the 6-digit code these templates carry. The
   stock Supabase emails don't include it.
4. Copy the URL, the anon key, the service role key and the database
   connection string. The service role key and the database URL are secrets.

## 2. Build the schema

From a checkout with the Python dependencies installed (`make install`):

```bash
export HELIXIS_SUPABASE__DB_URL='postgresql://...'   # your project's connection string
python scripts/apply_schema.py --dry-run              # shows the order
python scripts/apply_schema.py                        # applies it
```

The script applies every migration in numeric order and skips the one
historical file. It also places `src/helixis/db/schema/helixis_entities.sql`
where it belongs. Each file runs as one transaction, and the script stops at
the first error. It refuses a database that already has tables. Run
`python scripts/rls_guard.py` afterwards. It fails if any table has
row-level security off.

The memory library creates its own tables the first time it stores something.
After your first conversation with memory on, run
`python scripts/apply_schema.py --only 0025` so those tables are locked down
too, then run `rls_guard.py` again.

## 3. Deploy the API and the worker on Render

1. Fork or push this repository to your own GitHub account.
2. In Render: New → Blueprint, and pick the repository. `render.yaml` creates
   the API (`occupella-api`), the webhook worker and a manual backfill job.
3. Render asks for every variable marked `sync: false`. Fill in the required
   keys above for both the API and the worker, and leave optional ones blank.
4. Before you apply the Blueprint, change these three values in `render.yaml`
   (or on the service afterwards) to your own addresses:
   - `HELIXIS_API__PUBLIC_APP_URL`: your web app's URL
   - `HELIXIS_API__CORS_ORIGINS`: a JSON list of your web app's origins
   - `HELIXIS_API__CORS_ORIGIN_REGEX`: your Vercel preview pattern, or delete it
5. Check `https://<your-api>.onrender.com/api/v1/health` once it is live.

Not on Render? The `Dockerfile` runs the API (`uvicorn helixis.main:app`, port
8000). Run the worker from the same image with
`python -m helixis.webhooks.worker` and give both the same environment.

## 4. Deploy the web app

1. In `frontend/vercel.json`, change the `/api/(.*)` rewrite's destination to
   your API (`https://<your-api>.onrender.com/api/$1`).
2. Create a Vercel project with `frontend/` as the root directory.
3. Set `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY` and
   `VITE_RENDER_BACKEND_URL` (your API's URL; webhook addresses shown in the
   app are built from it). The PostHog and Sentry variables are optional.
4. Deploy, then sign up. The first account gets its own company.

Another static host works too. Run `npm run build` in `frontend/`, serve
`dist/`, and proxy `/api/*` to your API.

## 5. Connect a property management system

Sign in, open Connectors and paste the PMS keys. Webhooks (Buildium, Rentvine)
point at your API: the app shows the exact address to paste into the vendor's
settings.

Read the vendor's API terms before you connect. They bind you, not this
project.

## Rebranding

The app ships with Occupella's name, support address (`team@occupella.com`,
in `src/helixis/common/support.py` and `frontend/src/lib/support.ts`) and
footer. Change them for your own deployment. A test checks that the two
support addresses match.
