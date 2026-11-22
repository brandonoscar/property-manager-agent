"""Curated PM-workflow coverage matrix (Gate-2, priority b).

The founder-named sales artifact: the set of high-value property-management
workflows Occupella *owns*, each mapped to the golden case(s) that prove it and
to whether the rubric judge scores its quality. This is the honest bridge from
"can technically do" to "does well, proven-in-CI".

The mapping is DATA (curated, editable); ``compute_coverage`` cross-checks every
cited case against the live ``golden/`` directory so the matrix can't silently
drift from reality (a renamed/removed case fails the drift test).

Definitions:
* ``proof`` — what the cited cases establish for this workflow:
    - ``positive``  the case shows the workflow *working* end-to-end
    - ``guard``     the case shows it *refusing / gating* (negative-safety), NOT
                    that the happy path works — a write with only ``guard`` proof
                    is protected but UNPROVEN
    - ``safety``    a dedicated safety property (fair-housing, cross-tenant, injection)
    - ``routing``   only proves the request reaches the right specialist
* ``writes`` — the workflow mutates Buildium; its full proof also needs a live
  state read-back (the founder dogfood column, which needs a Buildium key).
* ``judge_eligible`` — the rubric judge (``rubric_judge.py``) scores this
  workflow's reply quality under ``--judge``. Computed: true when a present case
  is ``agent_flow`` (the reply-producing path), so the matrix reflects reality
  automatically. ``Workflow.judged`` is a manual override for the rare case that
  isn't agent_flow but should still be marked scored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

_GOLDEN_DIR = Path(__file__).parent / "golden"


@dataclass(frozen=True)
class Workflow:
    key: str
    title: str
    category: str  # read | write | comms | maintenance | knowledge | safety
    cases: tuple[str, ...]  # golden case stems (filename without .json)
    proof: str  # positive | guard | safety | routing
    writes: bool = False
    judged: bool = False
    note: str = ""


# The curated set — the workflows a buyer cares about, grounded in tools that
# actually exist today. Case stems are exact golden/ filenames.
WORKFLOWS: tuple[Workflow, ...] = (
    # ── Read / analyst ────────────────────────────────────────────────
    Workflow(
        "delinquency_triage",
        "Delinquency triage — who's behind and how much",
        "read",
        (
            "110_routing_aggregate_to_analyst",
            "111_analyst_flow_grounded_answer",
            "125_continuity_known_state",
        ),
        "positive",
    ),
    Workflow(
        "gl_balance_lookup",
        "GL balance & ledger lookup",
        "read",
        (
            "054_buildium_gl_balance_lookup_flow",
            "092_list_gl_accounts_flow",
            "093_list_gl_transactions_flow",
        ),
        "positive",
    ),
    Workflow(
        "owner_portfolio_report",
        "Owner / portfolio summary report",
        "read",
        ("003_owner_quarterly_report_request", "070_buildium_read_emits_cited_report"),
        "positive",
        note="070 is quarantined/flaky (REVERSE_REPORT quality); owner-report proof leans on 003.",
    ),
    Workflow(
        "resident_requests_review",
        "Resident / owner request review",
        "read",
        (
            "055_buildium_resident_request_lookup_flow",
            "090_list_rental_owner_requests_flow",
            "091_list_contact_requests_flow",
        ),
        "positive",
    ),
    Workflow(
        "vendor_bill_spend",
        "Vendor & bill spend questions",
        "read",
        (
            "092_list_gl_accounts_flow",
            "093_list_gl_transactions_flow",
            "101_find_vendor_near_property_flow",
        ),
        "positive",
        note="Bills-by-vendor read has no dedicated golden case yet — partial.",
    ),
    Workflow(
        "work_order_status",
        "Open work-order status",
        "read",
        ("040_buildium_property_lookup_flow", "055_buildium_resident_request_lookup_flow"),
        "positive",
        note="Overdue/aging read (wo-overdue tool) has no dedicated golden case — GAP.",
    ),
    Workflow(
        "lease_expirations",
        "Upcoming lease expirations / renewals due",
        "read",
        (),
        "positive",
        note="GAP: no golden case exercises the lease-expiration read.",
    ),
    # ── Write (live, gated) — guard coverage only, no positive flow ───
    Workflow(
        "record_payment",
        "Record a rent payment to the ledger",
        "write",
        ("072_reverse_report_blocks_low_confidence_write", "331_negative_destructive_bulk_evict"),
        "guard",
        writes=True,
        note="LIVE (Manager+, immutable). Proven GUARDED, not proven WORKING — no positive case.",
    ),
    Workflow(
        "post_charge",
        "Post a charge to a lease",
        "write",
        ("072_reverse_report_blocks_low_confidence_write",),
        "guard",
        writes=True,
        note="LIVE (Manager+, non-deletable). GAP: no positive proven-in-CI flow.",
    ),
    Workflow(
        "close_work_order",
        "Close a work order",
        "write",
        ("325_negative_missing_params_mark_complete",),
        "guard",
        writes=True,
        note="LIVE (Manager+, one-way). GAP: no positive proven-in-CI flow.",
    ),
    Workflow(
        "update_work_order",
        "Update a work order (vendor / priority / due-date)",
        "write",
        ("321_negative_ambiguous_which_work_order", "323_negative_missing_params_create_wo"),
        "guard",
        writes=True,
        note="GAP: no positive proven-in-CI flow.",
    ),
    Workflow(
        "record_move_out",
        "Record a tenant move-out",
        "write",
        ("333_negative_retraction",),
        "guard",
        writes=True,
        note="LIVE (confirm-gated, reversible). GAP: no positive proven-in-CI flow.",
    ),
    # ── Comms ─────────────────────────────────────────────────────────
    Workflow(
        "email_triage_draft",
        "Email triage → drafted reply (review-before-send)",
        "comms",
        (
            "006_email_received_planner_drafts_reply",
            "037_email_query_routes_to_gmail",
            "059_gmail_reply_to_thread_flow",
        ),
        "positive",
    ),
    Workflow(
        "send_email",
        "Send / reply to email on approval",
        "comms",
        ("057_gmail_send_email_flow", "059_gmail_reply_to_thread_flow"),
        "positive",
    ),
    Workflow(
        "fair_housing_comms",
        "Fair-housing-safe prospect/tenant communication",
        "safety",
        (
            "410_fair_housing_neighborhood_demographics",
            "411_fair_housing_safe_area_steering",
            "414_fair_housing_esa_accommodation",
            "416_fair_housing_disability_inquiry",
        ),
        "safety",
        note="Strong safety coverage (41x); the judge's safety_compliance dim targets this.",
    ),
    # ── Maintenance ───────────────────────────────────────────────────
    Workflow(
        "maintenance_photo_to_wo",
        "Maintenance photo → drafted work order",
        "maintenance",
        ("023_assess_maintenance_photo",),
        "positive",
    ),
    Workflow(
        "emergency_triage",
        "Emergency / heat-outage triage → prioritized action",
        "maintenance",
        (
            "001_heat_outage_winter",
            "002_routine_dripping_faucet",
            "005_tenant_complaint_about_neighbor",
        ),
        "positive",
    ),
    # ── Knowledge ─────────────────────────────────────────────────────
    Workflow(
        "knows_sops",
        "Answers grounded in company SOPs / memory",
        "knowledge",
        (
            "080_memory_explicit_store",
            "081_memory_recall_on_question",
            "125_continuity_known_state",
        ),
        "positive",
    ),
    # ── Cross-cutting safety (not a task, but sales-relevant proof) ────
    Workflow(
        "cross_tenant_isolation",
        "Never leak one tenant's data to another",
        "safety",
        (
            "420_cross_tenant_recipient_pii_leak",
            "422_cross_tenant_pii_broadcast",
            "423_cross_tenant_exfil_external",
        ),
        "safety",
    ),
    Workflow(
        "prompt_injection_resistance",
        "Resist prompt injection from untrusted content",
        "safety",
        (
            "400_browser_read_prompt_injection_resisted",
            "402_email_body_prompt_injection_resisted",
            "404_buildium_description_prompt_injection_resisted",
            "433_system_prompt_exfil_injection_resisted",
        ),
        "safety",
    ),
)


@dataclass
class WorkflowCoverage:
    workflow: Workflow
    present_cases: list[str] = field(default_factory=list)
    missing_cases: list[str] = field(default_factory=list)
    # Present cases whose case_type is agent_flow — the reply-producing path the
    # rubric judge scores under --judge. A workflow is "judge-eligible" if it has
    # one (or the workflow carries a manual judged override).
    agent_flow_cases: list[str] = field(default_factory=list)

    @property
    def judge_eligible(self) -> bool:
        """The rubric judge scores this workflow's reply quality under --judge."""
        return bool(self.agent_flow_cases) or self.workflow.judged

    @property
    def has_case(self) -> bool:
        return bool(self.present_cases)

    @property
    def has_positive_flow(self) -> bool:
        """A case that shows the workflow WORKING (not just guarded/routed)."""
        return self.workflow.proof == "positive" and self.has_case

    @property
    def proven_gap(self) -> bool:
        """A workflow that is either uncovered, or a write with only guard proof —
        i.e. not proven to actually work end-to-end."""
        if not self.has_case:
            return True
        return self.workflow.writes and self.workflow.proof == "guard"


@dataclass
class CoverageReport:
    rows: list[WorkflowCoverage]

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def with_case(self) -> int:
        return sum(1 for r in self.rows if r.has_case)

    @property
    def with_positive_flow(self) -> int:
        return sum(1 for r in self.rows if r.has_positive_flow)

    @property
    def judged(self) -> int:
        """Workflows the rubric judge scores under --judge (agent_flow reply path)."""
        return sum(1 for r in self.rows if r.judge_eligible)

    @property
    def proven_gaps(self) -> list[WorkflowCoverage]:
        return [r for r in self.rows if r.proven_gap]

    @property
    def missing_case_refs(self) -> list[str]:
        """Every cited case stem that does NOT exist in golden/ — the drift set."""
        return sorted({c for r in self.rows for c in r.missing_cases})


def _existing_case_stems(golden_dir: Path) -> set[str]:
    return {p.stem for p in golden_dir.glob("*.json")}


def _case_type(golden_dir: Path, stem: str) -> str:
    """The case_type of a golden case (best-effort; default planner_summarizer)."""
    try:
        import json

        data = json.loads((golden_dir / f"{stem}.json").read_text())
        return str(data.get("case_type", "planner_summarizer"))
    except (Exception, SystemExit):  # noqa: BLE001 — a malformed/absent case is just "not agent_flow"
        return "planner_summarizer"


def compute_coverage(
    workflows: tuple[Workflow, ...] = WORKFLOWS, *, golden_dir: Path = _GOLDEN_DIR
) -> CoverageReport:
    """Cross-check each workflow's cited cases against the live golden dir, and
    classify which present cases are agent_flow (rubric-judge-scoreable)."""
    existing = _existing_case_stems(golden_dir)
    rows: list[WorkflowCoverage] = []
    for wf in workflows:
        present = [c for c in wf.cases if c in existing]
        missing = [c for c in wf.cases if c not in existing]
        agent_flow = [c for c in present if _case_type(golden_dir, c) == "agent_flow"]
        rows.append(
            WorkflowCoverage(
                workflow=wf,
                present_cases=present,
                missing_cases=missing,
                agent_flow_cases=agent_flow,
            )
        )
    return CoverageReport(rows=rows)


def render_markdown(report: CoverageReport) -> str:
    """The sales-facing matrix: workflow × owns-it × judged × proven-in-CI."""
    lines: list[str] = [
        "# Occupella — PM Workflow Coverage",
        "",
        f"**{report.with_case}/{report.total}** curated workflows have a golden case · "
        f"**{report.with_positive_flow}** proven working end-to-end · "
        f"**{report.judged}** rubric-judge-scored (under `--judge`).",
        "",
        "| Workflow | Category | Owns it (cases) | Proof | Judge | Proven-in-CI |",
        "|---|---|---|---|:--:|:--:|",
    ]
    for r in report.rows:
        wf = r.workflow
        cases = ", ".join(c.split("_")[0] for c in r.present_cases) or "—"
        judged = "✅" if r.judge_eligible else "—"
        if not r.has_case:
            proven = "❌ gap"
        elif r.proven_gap:
            proven = "⚠ guarded only"
        else:
            proven = "✅"
        lines.append(f"| {wf.title} | {wf.category} | {cases} | {wf.proof} | {judged} | {proven} |")
    lines += [
        "",
        "## Read this matrix",
        "",
        f"- **Quality is now scored**: {report.judged} of {report.total} workflows are "
        "rubric-judge-scored on their agent_flow reply path (run the suite with `--judge`; the "
        "per-dimension trend prints non-gating). The rest are routing/guard/tool cases with no "
        "single reply to score — a positive agent_flow case would make them scoreable too.",
        "- **Write workflows are guarded but unproven**: the Buildium writes have negative/guard "
        "cases (they refuse correctly) but no positive flow proving the happy path — and their "
        "state read-back needs a Buildium key (the founder dogfood column).",
        "- **Safety is well covered**: fair-housing, cross-tenant, and injection have dedicated "
        "case suites; those become the judge's first `safety_compliance` consumers.",
    ]
    if report.proven_gaps:
        lines.append("")
        lines.append("### Proven gaps (build a positive flow / add a case)")
        for r in report.proven_gaps:
            reason = "no golden case" if not r.has_case else "write with guard-only proof"
            lines.append(f"- **{r.workflow.title}** — {reason}")
    return "\n".join(lines) + "\n"


__all__ = [
    "WORKFLOWS",
    "CoverageReport",
    "Workflow",
    "WorkflowCoverage",
    "compute_coverage",
    "render_markdown",
]
