"""Render the eval-reports/{date}-{sha}.md section for a live-eval run.

Follows the eval-reports norm: dated, committable, and honest about SKIPs (a
keyless/credless SKIP is recorded as SKIPPED, never dressed up as a pass).
"""

from __future__ import annotations

from datetime import UTC, datetime

from helixis.evals.live.suites.base import Status, SuiteResult

_STATUS_EMOJI = {
    Status.PASS: "✅ PASS",
    Status.FAIL: "❌ FAIL",
    Status.SKIPPED: "⏭️ SKIPPED",
    Status.ERROR: "⚠️ ERROR",
}


def report_filename(rc_sha: str, *, date: str | None = None) -> str:
    day = date or datetime.now(UTC).strftime("%Y-%m-%d")
    return f"{day}-{rc_sha}.md"


def render_report(results: list[SuiteResult], *, rc_sha: str, langfuse_active: bool) -> str:
    day = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = []
    lines.append(f"# Live release-gate eval run — {rc_sha}")
    lines.append("")
    lines.append(f"_Generated {day}. Release candidate: `{rc_sha}`._")
    lines.append("")
    lines.append(_overall_banner(results))
    lines.append("")
    lines.append(
        "These are the **live** suites — they need a real Buildium sandbox, seeded "
        "helixis-test data, a live LLM, and (suite 4) a real browser, so they cannot "
        "run in a deny-all CI env. Safety assertions (state-based read-back + trail) "
        "gate HARD; reply wording is a non-gating quality signal (CLAUDE.md §4). "
        "A suite with missing creds is **SKIPPED**, not passed — a keyless SKIP is not "
        "evidence."
    )
    lines.append("")
    lines.append(f"- Langfuse dataset logging: {'active' if langfuse_active else 'inactive'}")
    lines.append("- Two-company model: Company A is Buildium-sandbox-backed; Company B is a")
    lines.append("  shadow tenant seeded into the helixis-test mirror (one sandbox account can")
    lines.append("  only map to one Buildium `AccountId`).")
    lines.append("")
    lines.append("| # | Suite | Result | Scenarios (safety-gated) |")
    lines.append("|---|-------|--------|--------------------------|")
    for r in results:
        gated = sum(1 for o in r.outcomes if o.gated_pass)
        total = len(r.outcomes)
        cell = f"{gated}/{total} pass" if total else "—"
        lines.append(f"| {r.number} | {r.name} | {_STATUS_EMOJI[r.status]} | {cell} |")
    lines.append("")

    for r in results:
        lines.extend(_render_suite(r))
    lines.extend(_render_footer())
    return "\n".join(lines) + "\n"


def _render_footer() -> list[str]:
    return [
        "## Running the full gate",
        "",
        "```bash",
        "# 1. seed the two companies + shadow/dirty/redaction data, then run all suites",
        "python -m helixis.evals.live.runner --seed --reset",
        "# run a single suite:  --suite 5   (repeatable)",
        "```",
        "",
        "**Preconditions to turn a SKIP into a gated result:**",
        "",
        "| Need | Unblocks |",
        "|------|----------|",
        "| `HELIXIS_LLM__GOOGLE_API_KEY` | agent-driven suites 2, 8, 9 (+ 5e honesty) |",
        "| `HELIXIS_SUPABASE__DB_URL` (helixis-test) | seeder + read-back — 2, 5, 8, 9, 10 |",
        "| `HELIXIS_BUILDIUM__CLIENT_ID/SECRET` **(sandbox)** + sandbox `base_url` "
        "| Company A live provisioning (synthetic_traffic + backfill) and suite 10 |",
        "| `HELIXIS_BROWSER__BROWSERBASE_*` | suite 4 leg 4c (page redaction) |",
        "| `[security]` extra (presidio) | suite 4 real redaction path (gotcha 7) |",
        "",
        "> ⚠ Suites 5 and 10 fault-inject Buildium via a **mock transport** through the "
        "real client + real idempotency store, so they never touch a live Buildium and "
        "are safe to run against helixis-test with any (even placeholder) sandbox creds. "
        "Suites 2/8/9 drive the real agent and 2's Company A is live-Buildium-backed, so "
        "they need the **sandbox** creds — never point them at production Buildium.",
        "",
    ]


def _overall_banner(results: list[SuiteResult]) -> str:
    failed = [r for r in results if r.status in (Status.FAIL, Status.ERROR)]
    skipped = [r for r in results if r.status is Status.SKIPPED]
    if failed:
        names = ", ".join(f"#{r.number}" for r in failed)
        return f"> ❌ **DO NOT RELEASE** — {len(failed)} suite(s) failed: {names}."
    if skipped and len(skipped) == len(results):
        return "> ⏭️ **INCONCLUSIVE** — every suite SKIPPED (missing creds). Not a pass."
    if skipped:
        names = ", ".join(f"#{r.number}" for r in skipped)
        return (
            f"> 🟡 **PARTIAL** — all run suites passed, but {len(skipped)} SKIPPED "
            f"({names}); provide their creds for a full gate."
        )
    return "> ✅ **ALL LIVE SUITES PASS** for this release candidate."


def _render_suite(r: SuiteResult) -> list[str]:
    out: list[str] = []
    out.append(f"## Suite {r.number} — {r.name}  ·  {_STATUS_EMOJI[r.status]}")
    out.append("")
    out.append(f"**Pass criterion.** {r.pass_criterion}")
    out.append("")
    if r.status is Status.SKIPPED:
        out.append(f"**Skipped:** {r.skip_reason}")
        out.append("")
        return out
    if r.outcomes:
        out.append("| Scenario | Gate | Safety | Quality | Notes |")
        out.append("|----------|------|--------|---------|-------|")
        for o in r.outcomes:
            gate = "✅" if o.gated_pass else "❌"
            safety = f"{o.safety_pass_count}/{o.k}"
            quality = f"{o.quality_pass_count}/{o.k}"
            note = "; ".join(o.safety_failures or o.quality_notes) or o.description
            out.append(f"| `{o.scenario_id}` | {gate} | {safety} | {quality} | {_esc(note)} |")
        out.append("")
    for note in r.notes:
        out.append(f"- _note:_ {note}")
    if r.notes:
        out.append("")
    if r.bugs:
        out.append("**Bugs filed:**")
        for bug in r.bugs:
            out.append(f"- `{bug.scenario_id}` — {bug.title}")
        out.append("")
    return out


def _esc(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")[:240]
