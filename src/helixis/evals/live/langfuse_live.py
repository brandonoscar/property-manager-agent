"""Log each live-eval run to Langfuse as a Dataset run tagged with the RC SHA.

Mirrors :mod:`helixis.evals.langfuse_report` exactly (same gating, same
best-effort contract: observability must NEVER fail or change a run's verdict),
but writes to a SEPARATE dataset (``helixis-live-evals``) and keys the Dataset
RUN on the release-candidate git SHA — so every gating run is browsable in
Langfuse, pinned to the exact tree it gated.

Scores per scenario:
  * ``live_safety_pass``   NUMERIC 1/0   — the HARD gate outcome (pass^k)
  * ``live_quality_pass``  NUMERIC 1/0   — wording/quality (non-gating signal)
  * ``live_k``             NUMERIC       — k runs performed
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from helixis.settings import Settings, get_settings

logger = logging.getLogger(__name__)

DATASET_NAME = "helixis-live-evals"


@dataclass
class ScenarioRecord:
    """One scored scenario, ready to ship to Langfuse."""

    suite: str
    scenario_id: str
    safety_passed: bool
    quality_passed: bool
    k: int
    input: dict[str, Any]
    detail: dict[str, Any]


class LiveEvalLangfuse:
    """Best-effort Langfuse sink. A no-op when keys/env are absent."""

    def __init__(self, settings: Settings | None = None):
        self._settings = settings or get_settings()
        self._client = self._build_client()

    @property
    def active(self) -> bool:
        return self._client is not None

    def _build_client(self) -> Any | None:
        cfg = self._settings.langfuse
        if not cfg.enabled or not cfg.public_key or not cfg.secret_key:
            return None
        try:
            from langfuse import Langfuse

            return Langfuse(public_key=cfg.public_key, secret_key=cfg.secret_key, host=cfg.host)
        except Exception:  # noqa: BLE001 — never let observability break a run
            logger.debug("Langfuse live client init failed (non-fatal)", exc_info=True)
            return None

    def log_run(self, rc_sha: str, records: list[ScenarioRecord]) -> int:
        """Upsert the dataset + one trace/score set per scenario, linked as a
        Dataset RUN named ``rc-<sha>``. Returns the number of scenarios scored.
        Every SDK call is individually guarded so one bad record can't abort the
        batch or the surrounding gate."""
        client = self._client
        if client is None or not records:
            return 0
        run_name = f"rc-{rc_sha}"
        self._sync_dataset(client, records)
        try:
            dataset = client.get_dataset(DATASET_NAME)
            items_by_id = {getattr(it, "id", None): it for it in dataset.items}
        except Exception:  # noqa: BLE001
            logger.debug("get_dataset failed — scoring without run link", exc_info=True)
            items_by_id = {}

        scored = 0
        for rec in records:
            trace_id = str(uuid.uuid4())
            trace = self._trace(client, trace_id, rec, run_name)
            self._score(client, trace_id, "live_safety_pass", 1.0 if rec.safety_passed else 0.0)
            self._score(client, trace_id, "live_quality_pass", 1.0 if rec.quality_passed else 0.0)
            self._score(client, trace_id, "live_k", float(rec.k))
            item = items_by_id.get(rec.scenario_id)
            if item is not None and trace is not None:
                try:
                    item.link(trace, run_name)
                except Exception:  # noqa: BLE001
                    logger.debug("dataset link failed for %s", rec.scenario_id, exc_info=True)
            scored += 1
        try:
            client.flush()
        except Exception:  # noqa: BLE001
            logger.debug("langfuse flush failed (non-fatal)", exc_info=True)
        return scored

    def _sync_dataset(self, client: Any, records: list[ScenarioRecord]) -> None:
        try:
            client.create_dataset(
                name=DATASET_NAME,
                description="Helixis LIVE release-gate eval scenarios (sandbox + seeded data).",
            )
        except Exception:  # noqa: BLE001 — create is idempotent server-side
            logger.debug("create_dataset non-fatal", exc_info=True)
        for rec in records:
            try:
                client.create_dataset_item(
                    dataset_name=DATASET_NAME,
                    id=rec.scenario_id,
                    input=rec.input,
                    metadata={"suite": rec.suite},
                )
            except Exception:  # noqa: BLE001
                logger.debug("dataset_item failed for %s", rec.scenario_id, exc_info=True)

    def _trace(self, client: Any, trace_id: str, rec: ScenarioRecord, run_name: str) -> Any | None:
        try:
            return client.trace(
                id=trace_id,
                name=f"live-eval.{rec.scenario_id}",
                input=rec.input,
                output={
                    "safety_passed": rec.safety_passed,
                    "quality_passed": rec.quality_passed,
                    "detail": rec.detail,
                },
                tags=["live-eval", rec.suite, run_name],
            )
        except Exception:  # noqa: BLE001
            logger.debug("trace create failed for %s", rec.scenario_id, exc_info=True)
            return None

    def _score(self, client: Any, trace_id: str, name: str, value: float) -> None:
        try:
            client.score(name=name, value=value, data_type="NUMERIC", trace_id=trace_id)
        except Exception:  # noqa: BLE001
            logger.debug("score %s failed (non-fatal)", name, exc_info=True)


def make_sink(settings: Settings | None = None) -> LiveEvalLangfuse:
    return LiveEvalLangfuse(settings or get_settings())
