# Live PM playground

Drive the **deployed** Helixis web app in a real, visible browser as a property
manager: it logs in (you do the actual sign-in), types real chat prompts, walks
every surface (chat / inbox / activity / properties / CRM / search / settings /
⌘K palette), and screenshots each step into `./shots/`.

Runs on **your** machine (this is why it's here and not in the sandbox — the
sandbox can't reach the deployed app). Windows/PowerShell first-class; the Node
script runs anywhere.

## Safe by design

- **Never approves a write.** If the agent surfaces a confirm gate (a Buildium
  write / outbound message), the script screenshots it and clicks **Cancel** —
  nothing is filed or sent.
- **Read-mostly.** It only types chat prompts and navigates. It changes no
  settings and connects no accounts.

## Run it (PowerShell)

```powershell
cd e2e/live-playground

# Recommended: you log in yourself in the browser (password OR Google, 2FA ok)
./run.ps1

# Or auto-login if your account uses email + password
./run.ps1 -Email you@company.com -Password 's3cret'

# Point at a local dev build instead of prod
./run.ps1 -Url http://localhost:5173
```

First run installs Playwright + the Chromium binary automatically. A browser
window opens; if you didn't pass credentials it pauses with
`👉 LOG IN NOW …` — sign in, and it continues the moment the chat loads.

## Run it (plain Node, any OS)

```bash
cd e2e/live-playground
npm install && npx playwright install chromium
HELIXIS_URL=https://your-app.vercel.app node playground.mjs
```

## Options (env vars, all optional)

| Var | Default | Meaning |
|---|---|---|
| `HELIXIS_URL` | `https://your-app.vercel.app` | app to drive |
| `E2E_EMAIL` / `E2E_PASSWORD` | — | set both for automatic password login; else manual |
| `HEADLESS` | `false` | `true` hides the browser |
| `SLOWMO` | `250` | ms between actions (turn up to watch, down to hurry) |
| `SHOT_DIR` | `./shots` | where screenshots + `summary.md` land |
| `LOGIN_TIMEOUT_MS` | `300000` | how long it waits for you to finish manual login |

## Want the inbox/CRM populated first?

Empty Inbox/CRM is expected on a fresh account. Seed realistic events through the
real webhook pipeline before running (needs Buildium sandbox creds + a Gemini key
in env — see `CLAUDE.md` §4):

```powershell
python scripts/synthetic_traffic.py --count 5 --kind mix
```

Wait ~30–60s, then run `./run.ps1`.

## Output

`./shots/NN-*.png` screenshots + `./shots/summary.md` (per-step ✅/⚠️). Send me
`summary.md` and I'll turn any ⚠️ into fix PRs.

---

# Full E2E battery (`battery.mjs`)

The playground above is a quick walk. The **battery** runs ~34 realistic
property-management workflows through the live chat as a fictional company —
maintenance, leases, owner reports, finance, vendors, tenant comms, calendar,
documents, web, browser, memory, and safety — screenshots each, and writes a
pass/warn/fail report. Same safety guarantees: read/draft-only, and **every
confirm gate is auto-cancelled** (a "write" scenario that produces NO gate is a
hard safety FAIL — the F1 regression check).

```powershell
cd e2e/live-playground
./run-battery.ps1                          # full battery, manual login (20-40 min)
./run-battery.ps1 -Only "Maintenance,Web"  # just some categories
./run-battery.ps1 -Email you@co.com -Password 's3cret'
```

Or plain node: `SCENARIO_ONLY="Safety" node battery.mjs`.

**Scenarios** live in `scenarios.mjs` — a flat, easy-to-extend catalog. Add a
line to test more (`{ id, category, prompt, write?, expectAny?, route? }`); set
`write: true` for any ask that should surface a confirm gate.

**Output:** `./shots-battery/NNN-*.png` (one per scenario, plus `-GATE` shots for
gated writes) + `./shots-battery/battery-report.md` — grouped by category with a
✅/⚠️/❌ per scenario, the reply snippet, and a top-line **write-gate safety**
verdict. Runs all scenarios in one continuous thread (a realistic "all-day" use
that also exercises long-conversation history + cross-turn memory recall).

Send me `battery-report.md` + any ❌/⚠️ screenshots and I'll turn the real bugs
into fix PRs.

## Config (battery, env vars)

Same as the playground, plus:

| Var | Default | Meaning |
|---|---|---|
| `SCENARIO_ONLY` | (all) | comma-separated category names to run a subset |
| `REPLY_TIMEOUT_MS` | `150000` | max wait for each scenario's reply (browser/web are slow) |
| `SHOT_DIR` | `./shots-battery` | where screenshots + `battery-report.md` land |

## Heads-up

A "draft" scenario may create a real **Gmail draft** (not a send) if Gmail is
connected — harmless, but you'll see them in your Drafts. Buildium reads hit
your sandbox. Nothing is ever **sent, filed, assigned, or moved** — those all
gate, and the battery cancels every gate.
