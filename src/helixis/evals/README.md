# Helixis evals — planner + summarizer

A minimal eval harness that gates planner + summarizer behavior with
property-based assertions. The runner calls the production
`ActionPlanner.plan` and `Summarizer.summarize` entry points directly
so the eval reflects what users actually get; the only deviation from
production is `temperature=0` (forced via a `completion_fn` wrapper)
to reduce flake.

## Running

```bash
# All cases — requires HELIXIS_LLM__GOOGLE_API_KEY (or GOOGLE_API_KEY)
python -m helixis.evals.runner

# A single case (substring filter on case id)
python -m helixis.evals.runner --case 001
python -m helixis.evals.runner --case heat_outage

# Via pytest (CI entry point) — skips if no Gemini key is configured
pytest tests/test_evals.py -v
```

Exit code: `0` if every case passes, `1` if any assertion fails, `2`
if no key is configured or the filter matched nothing.

## Case types

The runner dispatches on each case's `case_type` field. Five are
supported today:

| `case_type`              | Calls into                                                 | When to use                                                                                                                                                                            |
| ------------------------ | ---------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `planner_summarizer`     | `ActionPlanner.plan` + `Summarizer.summarize`              | Default (back-compat with cases 001-005). Webhook-driven planner/summarizer behavior.                                                                                                  |
| `tool_invocation`        | A registered tool's `_impl` coroutine                      | Capability tools like `web_search` — exercises a single tool with structured-result assertions.                                                                                        |
| `orchestrator_routing`   | `agents.Runner.run(orchestrator, …)` with real LLM         | Whole-orchestrator routing decisions. Real Gemini at every agent turn (temperature=0 via `RunConfig`); per-case `tool_mocks` substitute each tool's `*_impl` so no live calls fire.    |
| `agent_flow`             | `agents.Runner.run(<specialist>, …)` with real LLM         | Multi-tool agent flows within a single specialist (e.g. browser_agent navigates → encounters login wall → escalates). Per-tool `tool_mocks` are SEQUENCED queues (each call consumes one return); `repeat` short-hand for "next N calls return this." |
| `reverse_report_enforcement` | `reverse_report.evaluate_write` + citation-graph (pure)  | DETERMINISTIC #271 enforcement checks — feeds a fixed REVERSE_REPORT + tool-call trail straight into `evaluate_write` / `ungrounded_observations` and asserts the verdict. No model → no flake, so these gate fully (critical=True). Prefer this over an `agent_flow` reverse-report case whenever the guarantee can be expressed as a decision over a fixed report (the gotcha-10 lesson: live Gemini's REVERSE_REPORT JSON flakes run-to-run). |

When `case_type` is omitted, `planner_summarizer` is assumed. The
runner's tool registry lives in `runner.py:_register_default_tools`
— adding a new capability tool to evals means registering its impl
there before authoring cases.

## How to extend this harness (add routing cases for the next specialist)

When a new specialist ships (e.g. `browser_agent` after Prompt 3),
drop a new `orchestrator_routing` case under `golden/NNN_*.json`
following this shape:

```json
{
  "id": "NNN_short_name",
  "case_type": "orchestrator_routing",
  "requires_env": ["HELIXIS_LLM__GOOGLE_API_KEY"],
  "description": "One sentence about what this case tests.",
  "input": {
    "user_message": "The exact user message the orchestrator sees.",
    "tool_mocks": {
      "<tool_name>": <mock_spec>
    }
  },
  "expected": {
    "must_route_to": "browser_agent",
    "must_not_route_to": ["web_agent", "buildium_agent"],
    "first_handoff_must_be": "browser_agent",
    "reply_must_contain": ["substring", "substring"],
    "reply_must_not_contain": ["Task #", "Tenant #"],
    "reply_must_match_regex": ["pattern"],
    "reply_must_not_match_regex": ["pattern"]
  }
}
```

For cases where multiple terminal agents are acceptable (e.g. an
ambiguous query the orchestrator may either clarify on or route
directly), use `must_route_to_one_of` INSTEAD of `must_route_to`:

```json
  "expected": {
    "must_route_to_one_of": ["Helixis", "buildium_agent"],
    "must_not_route_to": ["web_agent"]
  }
```

`Helixis` in the list means "the orchestrator stayed on itself"
(typical when its prompt's clarifying-question rule fires). A case
that declares both `must_route_to` and `must_route_to_one_of` is
rejected as a malformed case — pick exactly one.

Rules:

1. **`requires_env` is mandatory.** Every `orchestrator_routing`
   case must list `HELIXIS_LLM__GOOGLE_API_KEY` (plus any
   tool-specific env). Without this, CI without the secret cannot
   SKIP the case and the build breaks.
2. **Exactly one of `must_route_to` or `must_route_to_one_of` is
   mandatory.** Asserts the terminal agent. Use `must_route_to` for
   single-correct-answer cases; use `must_route_to_one_of` when the
   prompt legitimately allows multiple outcomes (e.g. clarify vs.
   route on ambiguous input). Add `must_not_route_to` as a
   negative-space guard regardless.
3. **`tool_mocks` keys must be in `_MOCK_TARGETS`** in
   `src/helixis/evals/orchestrator_routing.py`. To mock a new tool,
   add an entry there mapping `tool_name → (patch_target, factory)`.
   Factories take the JSON spec and return the impl's typed return
   value.
4. **Other assertions are optional.** Mix and match the include /
   exclude / regex assertions per case.
5. **Identity rule is in scope.** Add `["Task #", "Tenant #",
   "Property #"]` to `reply_must_not_contain` on every case that
   surfaces tool snippets to the user — defense-in-depth on the
   agent's prompt rule.

Authoring discipline: when a case is flaky 2+ runs in a row at
temperature=0, the routing prompt is ambiguous. Tighten the prompt;
don't tighten the assertion.

## How to extend with `agent_flow` cases

`agent_flow` is the case_type for "what does specialist X do given
specific tool returns?" Use when:

* The scenario requires the agent to call multiple tools in sequence
  (e.g. navigate → read_page → escalate_to_user).
* The assertion is on the agent's behavior chain, not just the
  orchestrator's routing choice.
* The tool returns need to be SCRIPTED across multiple calls (not
  one static value per tool, which is the `tool_invocation` shape).

Case shape:

```json
{
  "id": "NNN_short_name",
  "case_type": "agent_flow",
  "requires_env": ["HELIXIS_LLM__GOOGLE_API_KEY"],
  "input": {
    "user_message": "...",
    "agent": "Browser Agent",
    "tool_mocks": {
      "<tool_name>": [
        {"return": {<typed result fields>}, "repeat": 1},
        {"return": {<different result>}}
      ]
    }
  },
  "expected": {
    "tool_call_trail_must_contain": ["navigate", "escalate_to_user"],
    "tool_call_trail_must_not_contain": ["click"],
    "tool_call_count_min": {"read_page": 1},
    "tool_call_count_max": {"read_page": 15, "click": 0},
    "reply_must_contain": ["substring"],
    "reply_must_not_contain": ["Task #"],
    "reply_must_match_regex": ["pattern"]
  }
}
```

Rules:

1. `requires_env: ["HELIXIS_LLM__GOOGLE_API_KEY"]` — every
   agent_flow case drives real Gemini, so it MUST declare the
   Gemini key as a requirement (cleanly SKIPs in CI without it).
2. `input.agent` must reference a name registered in
   `_AGENT_FACTORIES` in `src/helixis/evals/agent_flow.py`. New
   specialists need a factory entry there.
3. Each tool listed in `input.tool_mocks` must appear in
   `_TOOL_MOCK_TARGETS`. To mock a new tool, add an entry mapping
   `tool_name → (patch_target, return_factory)` — the factory
   turns the JSON spec into the impl's typed return value.
4. Tool mock entries are **sequenced queues**. Each call consumes
   one entry. `"repeat": N` short-hands "this entry covers the
   next N calls" (useful for budget-exhaustion tests). When the
   queue is exhausted, the harness raises a clear error so the
   case author knows to extend it.
5. Tools NOT in `input.tool_mocks` use their real impl — which
   for browser_tools means returning `browser_unavailable` because
   Browserbase credentials are intentionally blank in the eval
   path. The agent's prompt teaches it to escalate on
   `browser_unavailable`, so unmocked tools naturally terminate
   the flow.
6. **Buildium read cases: mock the mirror tools empty.** The
   Buildium agent prefers the mirror aggregates (`property_snapshot`,
   `portfolio_overview`, `bank_balances`, `open_tasks`, …) for
   "state of the world" questions. In eval there is no memory
   context, so an UNMOCKED mirror tool returns a "which company?"
   dead-end instead of the empty-mirror hint, and the documented
   mirror-empty→live fallback never fires — the agent stalls. So a
   case that pins a LIVE `buildium_*` read must mock **every** mirror
   tool (all nine are in `_TOOL_MOCK_TARGETS`) with the empty-mirror
   message, and anchor the ask on the specific record type. Even so,
   gemini-2.5-flash can return an empty completion under the agent's
   57-tool schema on weakly-anchored asks; such cases stay
   `single_run_gate:false` (see 092/094-098).

## Adding a `planner_summarizer` golden case

1. Pull a real row from `buildium_events` in a dev or staging
   environment. The fields you need are `payload`, `enrichment`, and
   `event_time`.
2. Anonymize before committing: change tenant names, email addresses,
   phone numbers, and street addresses. Property and unit identifiers
   are not PII but the human-readable address is — swap it for a
   fictional one. Do **not** commit anything that could identify a
   real tenant.
3. Write the `expected` block as property-based assertions (see
   below). Substring + structural checks only, never exact-string
   match.
4. Drop the file under `src/helixis/evals/golden/NNN_short_name.json`
   following the existing numbering.
5. Run `python -m helixis.evals.runner --case NNN` to verify the
   case parses and runs. Commit.

### Case file shape

```json
{
  "id": "NNN_short_name",
  "description": "One sentence about what this case is testing.",
  "input": {
    "event_time_iso": "ISO-8601 timestamp the production event_time would carry",
    "payload": { "EventName": "Task.Created", ... raw webhook ... },
    "enrichment": { ... a full EnrichedEvent serialised as dict ... }
  },
  "expected": {
    "summary_must_mention": ["substring", "substring"],
    "summary_must_not_mention": ["forbidden", "forbidden"],
    "summary_max_words": 100,
    "action_surface_must_contain_action_type": ["substring"],
    "action_surface_must_contain_action_type_one_of": ["substring", "substring"],
    "action_surface_must_not_suggest": ["substring"],
    "signals_must_reference": []
  }
}
```

The `enrichment` blob must validate against
`helixis.agent.enrichment.models.EnrichedEvent` — that's the shape
the production planner sees. If a case fails to parse there, the
runner reports `ERROR: enrichment did not parse as EnrichedEvent`
and the case fails as a whole.

## Adding a `tool_invocation` golden case

```json
{
  "id": "NNN_short_name",
  "case_type": "tool_invocation",
  "tool": "web_search",
  "description": "One sentence about what this case is testing.",
  "requires_env": ["HELIXIS_WEB__TAVILY_API_KEY"],
  "input": {
    "kwargs": {"query": "...", "max_results": 5, "search_depth": "basic"},
    "env": {"HELIXIS_WEB__TAVILY_API_KEY": "override or empty-string-to-clear"}
  },
  "expected": {
    "result_must_contain": ["substring"],
    "result_must_not_contain": ["forbidden"],
    "warnings_must_include": ["search_unavailable"],
    "warnings_must_not_include": ["search_unauthorized"],
    "sources_min_count": 0,
    "sources_max_count": 10
  }
}
```

Optional fields:
- `requires_env`: list of env var names that must be set for the case
  to run. Missing env → case is SKIPPED (not FAILED) so CI without
  secrets stays green.
- `input.env`: env overrides applied for the duration of the call and
  restored after. Use `""` to unset an env var.

Each new `tool_invocation` case must reference a tool registered in
`runner.py:_register_default_tools` — adding a tool here also means
adding it there.

## Assertion semantics

Every assertion is a **substring + structural check**. Phrasing drift
must not break the test; behavior regressions must.

### `planner_summarizer` cases

| Key                                          | Applies to                          | Semantics                                                                                |
| -------------------------------------------- | ----------------------------------- | ---------------------------------------------------------------------------------------- |
| `summary_must_mention`                       | summarizer output text              | Each string must appear (case-insensitive substring).                                    |
| `summary_must_not_mention`                   | summarizer output text              | Each string must NOT appear. Carry the identity-rule guards on every case (see below).   |
| `summary_max_words`                          | summarizer output text              | Whitespace-split word count must not exceed this.                                        |
| `action_surface_must_contain_action_type`    | serialised planner ActionSurface    | Each string must appear somewhere in the JSON-serialised surface (action types, tool ids, intents, primary_action kinds, etc.). |
| `action_surface_must_contain_action_type_one_of` | serialised planner ActionSurface | Passes if AT LEAST ONE of the listed strings appears in the serialised surface. Use when several action types are all acceptable for the scenario (e.g. a tenant-on-tenant complaint → `send_tenant_message` OR `add_internal_note` OR `create_followup_task`), so the planner's run-to-run choice among them isn't a failure. Mirrors `must_route_to_one_of`. |
| `action_surface_must_not_suggest`            | serialised planner ActionSurface    | Each string must NOT appear anywhere in the serialised surface.                          |
| `signals_must_reference`                     | planner `signals` list              | Each string must appear in the serialised `signals` array. Omit or leave empty when the case has no strong signal expectation — the V2 planner is allowed to emit no signals. |

### `tool_invocation` cases

| Key                          | Applies to                              | Semantics                                                              |
| ---------------------------- | --------------------------------------- | ---------------------------------------------------------------------- |
| `result_must_contain`        | serialised tool result (JSON)           | Each string must appear (case-insensitive substring).                  |
| `result_must_not_contain`    | serialised tool result (JSON)           | Each string must NOT appear.                                           |
| `warnings_must_include`      | result `warnings` list                  | Each string must be present as an exact list entry.                    |
| `warnings_must_not_include`  | result `warnings` list                  | Each string must NOT be present.                                       |
| `sources_min_count`          | result `sources` list length            | Length must be >= the integer.                                         |
| `sources_max_count`          | result `sources` list length            | Length must be <= the integer.                                         |

Every key is optional. Empty list or missing key = that family of
assertion is skipped for the case.

### Identity-rule baseline

Every case's `summary_must_not_mention` list MUST include the
identity guards as a regression net:

```
"Task #", "Tenant #", "Property ID", "Unit ID", "Property #", "Lease #"
```

The summarizer's `sanitizer` already rejects ID-shaped patterns at
the production layer (see `helixis/agent/summarizer/sanitizer.py`),
but mirroring the guards here means a future prompt or sanitizer
regression that leaks an ID surfaces directly in the eval output.

## Debugging a failure

```bash
python -m helixis.evals.runner --case <id>
```

prints the case's PASS/FAIL with each failing assertion name and a
human-readable reason. For the planner side, the assertion message
references the substring that wasn't present; for the summarizer
side, it references either the missing substring, the forbidden
substring that did appear, or the over-length word count.

If you suspect the failure is a flaky LLM response rather than a
real regression: the runner forces `temperature=0` already; rerun
once and if it still fails, treat it as a real signal.

## Discipline

* Every regression that hits a design partner becomes a new golden
  case in this directory. No exceptions.
* No prompt change ships without running `pytest tests/test_evals.py`
  locally with the Gemini key set.
* When an existing case starts failing on a prompt change you wanted
  to make, the case is the source of truth — either the prompt
  change is wrong, or the case is wrong (and updating the case is a
  separate explicit decision, not a quiet edit). Prefer the former.

## Escalation reply-quality judge (non-gating · #218)

The escalation cases (061, 062) gate only on the deterministic trail + a
non-empty reply (#213) — reply *wording* was dropped because it flakes. To
measure the quality that gating can't, an opt-in LLM-judge scores escalation
replies on three criteria (names the gate honestly / states what was tried +
remains / gives a concrete next step):

```
python -m helixis.evals.runner --judge            # judge opted-in cases
python -m helixis.evals.runner --case 061 --judge
```

A case opts in with a top-level `"escalation_judge": {"scenario": "..."}` block.
The score is **printed in a separate report, never asserted** — it is NOT part
of the §4 sweep merge gate, and `--judge` is off by default (zero impact on CI /
`test_evals_pass`). Best-effort: a judge failure is reported as absent.

## What this harness does NOT catch

* Phrasing quality (clumsy but technically grounded summaries pass).
* Multi-turn behavior (the follow-up click-chain has its own surface).
* Latency / cost regressions.
* Cross-tenant data leakage (RLS lives in the DB layer, not here).
* Flake from underlying model serving (temperature=0 reduces but
  does not eliminate variance).

Specialist agents (web_agent, document_agent, browser_agent) will
add their own golden cases under this directory following the same
shape — the runner picks them up automatically as long as their
expected blocks use the supported assertion keys.
