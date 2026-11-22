"""Validate the rubric judge against hand-labeled traces (release-gate scoring).

Runs the judge's binary compliance decision over a labeled JSONL trace set,
builds the confusion matrix, and prints the TPR/TNR verdict. A judge is only
allowed to GATE once it clears ≥50 labeled traces with TPR AND TNR both > 90%
(``judge_validation.MIN_TRACES`` / ``MIN_RATE``); below that it prints
ADVISORY-ONLY and its scores are tracked, not gated.

    python scripts/validate_judge.py \
        --traces src/helixis/evals/judge_traces/fair_housing_compliance.jsonl

Needs the judge model's Anthropic key (the judge model is independent of the
product model — see ``rubric_judge.judge_model``). Best-effort: an unreachable
judge fails CLOSED (every prediction False), which surfaces as a poor TNR/TPR
rather than a false "validated".
"""

from __future__ import annotations

import argparse
import sys

from helixis.evals.judge_validation import load_labeled_traces, validate_judge_over_traces
from helixis.evals.rubric_judge import compliance_predicate, judge_model
from helixis.settings import get_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the rubric judge over labeled traces.")
    parser.add_argument("--traces", required=True, help="Path to a labeled JSONL trace file.")
    args = parser.parse_args(argv)

    settings = get_settings()
    traces = load_labeled_traces(args.traces)
    print(f"Loaded {len(traces)} labeled traces from {args.traces}")
    print(f"Judge model: {judge_model(settings)} (product model: {settings.llm.default_model})")

    predicate = compliance_predicate(settings)
    result = validate_judge_over_traces(traces, predicate)

    print(result.summary())
    cm = result.cm
    print(f"  confusion: tp={cm.tp} fp={cm.fp} tn={cm.tn} fn={cm.fn}")
    if result.reasons:
        print("  advisory reasons:")
        for r in result.reasons:
            print(f"    - {r}")
    # Exit 0 whether VALIDATED or ADVISORY — this is a report, not a CI gate.
    # (Gating wiring happens once a property clears the 50-trace floor.)
    return 0


if __name__ == "__main__":
    sys.exit(main())
