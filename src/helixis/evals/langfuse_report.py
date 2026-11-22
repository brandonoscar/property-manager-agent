"""Optional Langfuse **Dataset + Score** reporting for the eval harness.

Opt-in and best-effort. It is OFF unless the runner is invoked with
``--langfuse`` (or ``HELIXIS_EVAL_LANGFUSE`` is set) AND Langfuse keys are
configured. It never raises into the eval run — same contract as
:mod:`helixis.observability.tracing`: *observability must never break the
product* (here, must never fail a run or change its exit code).

When active, per invocation it:

* **Upserts a Langfuse Dataset** (``helixis-golden-evals``) with one item per
  golden case, keyed by case id — so the golden set is versioned and browsable
  in Langfuse, and re-syncing updates in place rather than duplicating.
* **Creates one trace per case, links it to the dataset item as a Dataset
  RUN, and attaches Scores**:
    - ``eval_pass``          NUMERIC 1.0 / 0.0  — the quality-gate outcome
    - ``eval_latency_ms``    NUMERIC            — wall-clock per case
    - ``eval_failure_kind``  CATEGORICAL        — ``safety`` / ``quality`` (only on a failure)

  This turns the local-only pass/fail into a **tracked-over-time quality +
  latency signal per case** — the backbone of the planned quality/latency/cost
  eval routines (see docs/audits/langfuse-render-audit.md §1.4).

**Not yet captured: per-case token COST.** Cost lives on the LiteLLM
*generation* observations, which the eval process does not currently emit (it
never calls ``configure_langfuse``), and each case's generations would have to
be pinned to the eval trace id — the planner and orchestrator open their own
``trace_context`` mid-run, so correlating them is a deliberate follow-up, not a
silent omission. Quality + latency ship now; cost is the next increment.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from helixis.settings import Settings

if TYPE_CHECKING:
    from helixis.evals.runner import CaseResult

logger = logging.getLogger(__name__)

DATASET_NAME = "helixis-golden-evals"


def _build_client(settings: Settings) -> Any | None:
    """Return a Langfuse client, or ``None`` when disabled / keys blank /
    import fails. Mirrors ``tracing.configure_tracer``'s gating exactly."""
    cfg = settings.langfuse
    if not cfg.enabled or not cfg.public_key or not cfg.secret_key:
        return None
    try:
        from langfuse import Langfuse

        return Langfuse(
            public_key=cfg.public_key,
            secret_key=cfg.secret_key,
            host=cfg.host,
        )
    except Exception:  # noqa: BLE001 — never let observability break the eval
        logger.debug("Langfuse eval client init failed (non-fatal)", exc_info=True)
        return None


def _run_name() -> str:
    """A stable-ish name for this eval run's Dataset run. Prefers an
    explicit override or the CI commit SHA; falls back to a timestamp
    (``datetime`` is fine here — this is a plain script, not a resumable
    workflow)."""
    explicit = os.environ.get("HELIXIS_EVAL_RUN_NAME")
    if explicit:
        return explicit
    sha = os.environ.get("GITHUB_SHA", "")
    prefix = f"ci-{sha[:7]}" if sha else "local"
    return f"{prefix}-{datetime.now(UTC):%Y%m%d-%H%M%S}"


def _sync_dataset(client: Any, cases: list[dict[str, Any]]) -> None:
    """Upsert the dataset + one item per case (keyed by case id). Each call
    is individually guarded so one bad case can't abort the sync."""
    try:
        client.create_dataset(
            name=DATASET_NAME,
            description="Helixis golden eval cases (planner/summarizer, tool, routing, flow).",
        )
    except Exception:  # noqa: BLE001 — create is idempotent server-side; ignore conflicts
        logger.debug("create_dataset non-fatal", exc_info=True)

    for case in cases:
        case_id = case.get("id")
        if not case_id:
            continue
        try:
            client.create_dataset_item(
                dataset_name=DATASET_NAME,
                id=case_id,  # upsert-by-id: re-syncing updates in place
                input=case.get("input"),
                expected_output=case.get("expected"),
                metadata={
                    "case_type": case.get("case_type", "planner_summarizer"),
                    "tool": case.get("tool"),
                    "requires_env": case.get("requires_env") or [],
                    "single_run_gate": case.get("single_run_gate"),
                },
            )
        except Exception:  # noqa: BLE001
            logger.debug("create_dataset_item failed for %s (non-fatal)", case_id, exc_info=True)


def _score(
    client: Any,
    *,
    trace_id: str,
    name: str,
    value: float | str,
    data_type: str,
    comment: str | None = None,
) -> None:
    try:
        client.score(
            name=name, value=value, data_type=data_type, trace_id=trace_id, comment=comment
        )
    except Exception:  # noqa: BLE001
        logger.debug("score %s failed (non-fatal)", name, exc_info=True)


def _report_run(
    client: Any, cases: list[dict[str, Any]], results: list[CaseResult], run_name: str
) -> int:
    """Create a trace + scores per non-skipped case and link it to the dataset
    run. Returns the number of cases scored."""
    try:
        dataset = client.get_dataset(DATASET_NAME)
        items_by_id = {getattr(it, "id", None): it for it in dataset.items}
    except Exception:  # noqa: BLE001
        logger.debug("get_dataset failed — scoring without dataset-run link", exc_info=True)
        items_by_id = {}

    case_type_by_id = {c.get("id"): c.get("case_type", "planner_summarizer") for c in cases}
    scored = 0
    for result in results:
        if result.skipped:  # a skip is not a quality signal — don't score it
            continue
        case_id = result.case_id
        trace_id = str(uuid.uuid4())
        try:
            trace = client.trace(
                id=trace_id,
                name=f"eval.{case_id}",
                input={"case_id": case_id, "case_type": case_type_by_id.get(case_id)},
                output={
                    "passed": result.passed,
                    "failure_kind": result.failure_kind,
                    "failed_assertions": [a.name for a in result.assertions if not a.passed],
                    "error": result.error,
                },
                tags=["eval", case_type_by_id.get(case_id) or "unknown", run_name],
            )
        except Exception:  # noqa: BLE001
            logger.debug("trace create failed for %s (non-fatal)", case_id, exc_info=True)
            trace = None

        _score(
            client,
            trace_id=trace_id,
            name="eval_pass",
            value=1.0 if result.passed else 0.0,
            data_type="NUMERIC",
        )
        if result.latency_ms is not None:
            _score(
                client,
                trace_id=trace_id,
                name="eval_latency_ms",
                value=float(result.latency_ms),
                data_type="NUMERIC",
            )
        if not result.passed and result.failure_kind:
            _score(
                client,
                trace_id=trace_id,
                name="eval_failure_kind",
                value=result.failure_kind,
                data_type="CATEGORICAL",
                comment="; ".join(a.name for a in result.assertions if not a.passed)
                or result.error,
            )

        item = items_by_id.get(case_id)
        if item is not None and trace is not None:
            try:
                item.link(trace, run_name)
            except Exception:  # noqa: BLE001
                logger.debug("dataset item link failed for %s (non-fatal)", case_id, exc_info=True)
        scored += 1
    return scored


def report(
    cases: list[dict[str, Any]], results: list[CaseResult], *, settings: Settings | None = None
) -> None:
    """Entry point: sync the dataset, score this run, flush. Fully best-effort —
    prints one status line either way and never raises."""
    settings = settings or Settings()
    client = _build_client(settings)
    if client is None:
        print("[langfuse] eval reporting requested but Langfuse keys not configured — skipping")
        return
    run_name = _run_name()
    try:
        _sync_dataset(client, cases)
        scored = _report_run(client, cases, results, run_name)
        client.flush()
        print(
            f"[langfuse] eval reporting → dataset {DATASET_NAME!r} run {run_name!r} "
            f"({scored} case(s) scored) @ {settings.langfuse.host}"
        )
    except Exception:  # noqa: BLE001 — a reporting failure must never fail the eval
        logger.debug("Langfuse eval reporting failed (non-fatal)", exc_info=True)
        print("[langfuse] eval reporting hit an error — continuing (run unaffected)")


__all__ = ["DATASET_NAME", "report"]
