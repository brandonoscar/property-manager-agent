"""The PR cost gate must know which cases actually spend money.

The leak this pins, found 2026-08-26 while diagnosing a red backend job on a
FRONTEND-ONLY pull request:

``_case_needs_gemini`` decided whether a case was free by looking for one
hardcoded constant — ``HELIXIS_LLM__GOOGLE_API_KEY``. The 2026-07-21 Anthropic
migration (gotcha 18) rewrote every golden file to declare
``HELIXIS_LLM__ANTHROPIC_API_KEY`` instead, and from that day the check matched
nothing. Every ``tool_invocation`` case silently reclassified itself as
"deterministic — free, always run", so the four MULTIMODAL document cases — a
lease PDF, an invoice, an illegible scan, a maintenance photo, the most
token-heavy calls in the suite — ran live against Anthropic on EVERY pull
request for five weeks, including docs-only and frontend-only ones that cannot
possibly regress them. Each then retried up to three times.

Nothing failed. The gate simply stopped gating, and a cost control that has
silently stopped working looks exactly like one that is working.

⚠ These read BOTH sides (gotcha 28): the classifier AND the golden corpus it
classifies. A test asserting only that the function behaves correctly on
hand-written inputs would have passed happily throughout the leak — the
function was self-consistent; it was pointed at a key that no longer existed.
"""

from __future__ import annotations

import json
import pathlib

from helixis.evals.runner import (
    _LLM_ENV_PATTERN,
    case_makes_billed_call,
    select_pr_gate_cases,
)

GOLDEN = pathlib.Path("src/helixis/evals/golden")

MODEL_DRIVEN = {"agent_flow", "orchestrator_routing", "planner_summarizer"}


def _cases() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(GOLDEN.glob("*.json"))]


# ── the corpus, read directly ───────────────────────────────────────────────


def test_every_provider_key_in_the_corpus_is_recognised() -> None:
    """⚠ The assertion that would have caught the leak on the day it opened.

    Whatever key the golden files actually declare, the gate must recognise it
    as billed. Swap providers again and this fails immediately instead of
    quietly waving 139 cases through as free.
    """
    declared: set[str] = set()
    for case in _cases():
        declared.update(
            e for e in (case.get("requires_env") or []) if e.startswith("HELIXIS_LLM__")
        )

    assert declared, "no golden case declares a provider key — has requires_env moved?"
    unrecognised = {e for e in declared if not _LLM_ENV_PATTERN.match(e)}
    assert not unrecognised, (
        f"the cost gate does not recognise these provider keys as billed: {unrecognised}. "
        "Every case declaring one runs live on every PR while counting as free."
    )


def test_no_case_that_calls_a_model_is_classed_free() -> None:
    """The leak, stated as the property it violated."""
    misclassified = [
        case.get("id")
        for case in _cases()
        if not case_makes_billed_call(case)
        and (
            any(e.startswith("HELIXIS_LLM__") for e in (case.get("requires_env") or []))
            or case.get("case_type", "planner_summarizer") in MODEL_DRIVEN
        )
    ]
    assert not misclassified, (
        f"these cases spend provider money but the gate calls them free: {misclassified}"
    )


def test_the_multimodal_document_cases_are_billed() -> None:
    """Named explicitly because they are the expensive ones and they are the
    four that leaked. A PDF or an image is the largest input in the suite."""
    by_id = {c.get("id"): c for c in _cases()}
    for cid in (
        "020_extract_lease",
        "021_extract_invoice",
        "022_extract_illegible",
        "023_assess_maintenance_photo",
    ):
        assert cid in by_id, f"{cid} vanished from the corpus"
        assert case_makes_billed_call(by_id[cid]), f"{cid} is billed but classed free"


# ── the gate's behaviour ────────────────────────────────────────────────────


def test_a_frontend_only_pr_runs_no_billed_case() -> None:
    """The concrete waste. A PR touching only `frontend/` cannot regress a
    document-extraction eval, so it must not pay to run one."""
    selected = select_pr_gate_cases(_cases(), set(), eval_code_changed=False)
    billed = [c.get("id") for c in selected if case_makes_billed_call(c)]
    assert not billed, f"a docs/frontend-only PR would still run live cases: {billed}"


def test_a_changed_golden_case_still_runs() -> None:
    """The gate must stay a cost control, not a coverage cut."""
    target = "020_extract_lease"
    selected = select_pr_gate_cases(_cases(), {target}, eval_code_changed=False)
    assert target in {c.get("id") for c in selected}


def test_agent_code_changes_still_run_the_safety_battery() -> None:
    """The whole point of the gate: a code edit CAN regress a safety bound, so
    it still pays for those. Cutting this would be a concession, not a saving."""
    selected = select_pr_gate_cases(_cases(), set(), eval_code_changed=True)
    assert len(selected) > len(select_pr_gate_cases(_cases(), set(), eval_code_changed=False))


def test_deterministic_cases_always_run() -> None:
    """They are free, so there is never a reason to defer them."""
    deterministic = [c for c in _cases() if not case_makes_billed_call(c)]
    assert deterministic, "no free cases at all — the classifier is over-matching"
    selected = {c.get("id") for c in select_pr_gate_cases(_cases(), set(), eval_code_changed=False)}
    for case in deterministic:
        assert case.get("id") in selected


# ── the pattern itself ──────────────────────────────────────────────────────


def test_the_pattern_matches_providers_and_spares_everything_else() -> None:
    """Both directions (gotcha 38): what it must catch and what it must spare."""
    for billed in (
        "HELIXIS_LLM__ANTHROPIC_API_KEY",
        "HELIXIS_LLM__GOOGLE_API_KEY",
        "HELIXIS_LLM__VOYAGE_API_KEY",
        "HELIXIS_LLM__OPENAI_API_KEY",
    ):
        assert _LLM_ENV_PATTERN.match(billed), f"{billed} costs money and is not matched"

    for free in (
        "HELIXIS_SUPABASE__DB_URL",
        "HELIXIS_BUILDIUM__CLIENT_ID",
        "HELIXIS_COMPOSIO__API_KEY",  # a connector, not a model — no per-token cost
        "HELIXIS_TAVILY__API_KEY",
    ):
        assert not _LLM_ENV_PATTERN.match(free), f"{free} is not a model provider"
