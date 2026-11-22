"""Live release-gate eval suites (the sandbox half of the spec).

Six suites that CANNOT run in a deny-all CI/cloud env because they need a real
Buildium sandbox + seeded data + a live LLM + (for redaction) a real browser:

    2  cross-tenant leakage      suites.suite02_cross_tenant
    4  redaction (Presidio)      suites.suite04_redaction
    5  hallucinated success      suites.suite05_hallucinated_success
    8  multi-turn personas       suites.suite08_personas
    9  dirty-data edge cases     suites.suite09_dirty_data
   10  rate limit & recovery     suites.suite10_rate_limit

They share the harness in this package:

    config          capability/creds gating + stable A/B company ids + RC sha
    seeder          two-company sandbox seeder (idempotent, --reset)
    scoring         STATE-BASED write scoring — read the row back, never trust text
    passk           pass^k (k=4) — a safety scenario gates on ALL k passing
    driver          headless agent driver (run_agent_streaming, multi-turn)
    langfuse_live   log each run as a Langfuse dataset tagged with the RC sha
    report          render the eval-reports/{date}-{sha}.md section per suite

Design contract (release-gate discipline, see docs + CLAUDE.md §4):
  * SAFETY assertions (state-based + trail) gate HARD.
  * Reply WORDING is a quality signal — quarantined, never a hard gate.
  * No creds → the suite records SKIPPED, never a fake green
    (eval-reports/README.md: "a keyless SKIPPED placeholder is not evidence").
"""
