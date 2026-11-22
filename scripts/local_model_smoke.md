# Local-model machinery smoke (free, plumbing-only)

Run the agent MACHINERY against a free local Ollama model instead of paying
Anthropic tokens — to iterate on the *wiring* (does fan-out fire in
parallel? does the loop finish? does the deadline salvage deliver a partial
instead of an empty give-up? does a trivial question stay single-call?).

> ⚠️ **PLUMBING ONLY.** A local model's *answer quality* says nothing about
> production Sonnet. This is for "does the machinery behave," never "is the
> answer good." The dev override is HARD-GATED to non-production and is
> ignored (with a warning) if `HELIXIS_ENV=production`.

## One-time setup

1. Install Ollama: https://ollama.com/download
2. Pull a **tool-calling-capable** model (a weak one will flub the tool
   calls and the run breaks for model reasons, not Helixis bugs):
   ```
   ollama pull llama3.3        # or: qwen2.5, mistral-nemo
   ```
3. Ollama serves on `http://localhost:11434` by default — LiteLLM's
   `ollama/` provider talks to it with no API key.

## Run the backend on the local model

```bash
export HELIXIS_ENV=development           # MUST NOT be production
export HELIXIS_DEV__LOCAL_MODEL=ollama/llama3.3
export HELIXIS_AGENT__REASONING_LOOP=true   # so fan-out is exercised
# (Buildium mirror / Supabase envs as usual for a local backend)
make backend
```

On boot you'll see a loud warning confirming the override is active — that's
expected. If you instead see "IGNORING it … ENV is production," your env is
production; fix `HELIXIS_ENV`.

## Fire the machinery checks (each is a claim about the WIRING)

Point these at your local backend's `/api/v1/agent/run` (a fresh
`session_id` each, wait for `done`):

| Question | What the PLUMBING should do |
|---|---|
| "compare my two delinquent tenants" | 2–3 specialists run **in parallel** in the trail; ends with the Sources/Confidence/Unverified footer |
| "what units are at Riverside?" | **one** mirror call, no fan-out |
| "what's the average rent across my portfolio?" | **single** analyst call (over-fan guard) |
| a deliberately huge multi-part question | deadline hit → a **partial** answer with the "ran long" note, NOT an empty give-up |
| any question | first activity appears fast (memory search is timeout-bounded) |

You're grading *shape*, not correctness: which tools fire, in what order,
in parallel or serial, and whether the turn completes. If the local model
gives a dumb answer but the machinery behaved, that's a **pass** — quality
is the Sonnet Layer-4 pass, done separately and paid, once.

## When you're done

Unset `HELIXIS_DEV__LOCAL_MODEL` (or just don't export it) to return to the
Anthropic default. Nothing about this touches prod — the override is
non-production-only by construction.
