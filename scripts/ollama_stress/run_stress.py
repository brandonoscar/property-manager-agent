"""Run the adversarial corpus through the REAL agent on the local model.

Each corpus row's ``input`` is fed through ``helixis.evals.agent_flow.
run_agent_flow_case`` — the same proven agent-run path the golden eval suite
uses — so we do NOT reinvent the orchestrator invocation. The run's tool-call
trail + final reply then go through ``safety_checks.evaluate``.

Model selection is the standard eval override: export
``HELIXIS_EVAL_MODEL=ollama_chat/<tag>`` before running (identical to the
nightly safety battery). Gates on CONFIRMED safety violations only:

  * exit 1 if any run is UNSAFE (forbidden action / canary leak / steering);
  * exit 1 if NOTHING ran conclusively (all errored) — the false-green guard;
  * errored runs otherwise are surfaced but NON-gating (local-model noise).

No real writes occur: the eval settings carry no Buildium/Composio creds, so a
forbidden tool the agent *decides* to call still records its name in the trail
(which is what we flag) without a live side effect.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import safety_checks as sc  # noqa: E402


async def _run_one(variant: dict, agent: str) -> tuple[str, object, list[str]]:
    from helixis.evals.agent_flow import run_agent_flow_case

    # ``input.agent`` is REQUIRED — run_agent_flow_case builds the agent from
    # this factory key and errors on a missing/unknown one. "Buildium Agent"
    # matches the golden should-not-act (32x) cases; pass --agent Orchestrator
    # to fuzz the routing layer too.
    case = {"input": {"user_message": variant["input"], "agent": agent}, "expected": {}}
    res = await run_agent_flow_case(case)
    if res.error:
        return ("error", res.error, [])
    verdict = sc.evaluate(
        category=variant.get("category", "injection"),
        tool_call_trail=res.tool_call_trail,
        reply=res.final_output,
        canaries=[variant["canary"]] if variant.get("canary") else [],
    )
    return (("safe" if verdict.safe else "unsafe"), verdict, res.tool_call_trail)


async def _main(args: argparse.Namespace) -> int:
    corpus = [
        json.loads(line)
        for line in Path(args.corpus).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.limit:
        corpus = corpus[: args.limit]
    if not corpus:
        print("No corpus rows — run generate_variants.py first.", file=sys.stderr)
        return 1

    if not os.environ.get("HELIXIS_EVAL_MODEL"):
        print(
            "WARNING: HELIXIS_EVAL_MODEL is unset — this will run on the DEFAULT (paid) model, "
            "NOT Ollama. Export HELIXIS_EVAL_MODEL=ollama_chat/<tag> for the free run.",
            file=sys.stderr,
        )

    safe = unsafe = errored = 0
    for i, variant in enumerate(corpus):
        kind, detail, trail = await _run_one(variant, args.agent)
        if kind == "safe":
            safe += 1
        elif kind == "error":
            errored += 1
        else:
            unsafe += 1
            print(f"  [UNSAFE] seed={variant.get('seed')} reasons={detail.reasons} trail={trail}")
        if (i + 1) % 20 == 0:
            print(f"  ...{i + 1}/{len(corpus)}  (safe={safe} unsafe={unsafe} err={errored})")

    print(
        f"\n== stress summary ==  safe={safe}  UNSAFE={unsafe}  "
        f"errored={errored}  total={len(corpus)}"
    )

    if safe + unsafe == 0:
        print(
            "RESULT: RED — no conclusive runs (every case errored). Check Ollama is up and "
            "HELIXIS_EVAL_MODEL points at it.",
            file=sys.stderr,
        )
        return 1
    if unsafe > 0:
        print(f"RESULT: RED — {unsafe} confirmed safety violation(s).", file=sys.stderr)
        return 1
    print(
        f"RESULT: GREEN — 0 safety violations across {safe} conclusive runs "
        f"({errored} errored, non-gating)."
    )
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the adversarial stress corpus through the agent.")
    ap.add_argument("--corpus", default="scripts/ollama_stress/corpus.jsonl")
    ap.add_argument("--limit", type=int, default=0, help="cap the number of variants (0 = all)")
    ap.add_argument(
        "--agent",
        default="Buildium Agent",
        help="factory key to run the input through (e.g. 'Orchestrator' to fuzz routing)",
    )
    args = ap.parse_args()
    raise SystemExit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
