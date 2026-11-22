"""Ambient detector framework — the ONE object the ambient detectors share.

A **Detector** reads mirrored state (or, later, comms / history) and emits
**Finding**s. A Finding is the typed, idempotent, entity-linked,
evidence-carrying record persisted to ``helixis_risk_flags`` — the unified
finding store. The same dedup key (``company_id, rule_key, fingerprint``)
means the same condition detected on twelve consecutive nights is ONE row
with a refreshed ``last_seen_at``, never twelve.

This generalises the original reconciliation engine (rules 1-2) into a
registry so every future detector — state-rule, comms-extraction,
aggregate, on-demand — shares the same store, dedup, auto-resolve,
dismissal-suppression, per-detector crash isolation, and observability.
Adding a detector is one subclass + one ``register()`` call; the framework
is untouched.

Finding == the old ``reconciliation.engine.RiskFlag`` (identical fields), so
the existing ``helixis_risk_flags`` table and its PM-facing routes carry
every detector's output with no schema change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import asyncpg

# ── detector_class taxonomy (the plan's three classes + the on-demand one) ──
STATE_RULE = "state_rule"  # pure logic over current mirrored state
COMMS = "comms"  # LLM classification/extraction over inbound messages
AGGREGATE = "aggregate"  # computed over history, not current state
ON_DEMAND = "on_demand"  # parameterized sweep, not scheduled

# helixis_risk_flags.source values (the store's provenance discriminator).
SOURCE_RULE = "rule"
SOURCE_ANALYST = "analyst"


@dataclass
class Finding:
    """One detected condition. Persisted to ``helixis_risk_flags``.

    ``rule_key`` + ``fingerprint`` is the dedup identity within a company;
    ``evidence`` is the citation (the exact numbers/ids the finding was
    computed from) so "this looks wrong" is never shippable without proof.
    """

    rule_key: str
    fingerprint: str
    severity: str  # 'info' | 'warning' | 'critical'
    title: str
    detail: str
    amount: float | None = None
    property_id: int | None = None
    entity_type: str | None = None
    entity_id: int | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


class Detector(ABC):
    """One ambient detector. Read-only against external systems — it
    proposes Findings; it never acts.

    Subclasses set the class attributes and implement :meth:`scan`. The
    framework handles persistence, dedup, auto-resolve, suppression, and
    crash isolation — a detector only computes Findings.
    """

    #: Stable id, unique across the registry. Also the log/observability tag.
    detector_id: str
    #: One of STATE_RULE / COMMS / AGGREGATE / ON_DEMAND.
    detector_class: str = STATE_RULE
    #: ``helixis_risk_flags.source`` for rows this detector writes.
    source: str = SOURCE_RULE
    #: Whether the scheduled cycle runs this detector by default. New
    #: detectors ship dark (False) so merging can't flood the findings
    #: list before the attention-budget layer lands; enable per-id via the
    #: ``HELIXIS_DETECTORS__ENABLED_IDS`` env allowlist. Direct callers
    #: (``run_detectors_for_company``) run whatever they are handed.
    default_enabled: bool = True
    #: The ``rule_key`` namespace(s) this detector owns. Stale findings under
    #: these keys that the current scan no longer emits are auto-resolved, so
    #: a detector MUST declare every rule_key its Findings can carry.
    rule_keys: tuple[str, ...] = ()
    #: Every ``helixis_*`` table :meth:`scan` reads. The cycle uses it to skip
    #: a detector for a company whose PMS does not fill one of them
    #: (``db/pms_coverage.py``): an empty table there means "never sent", not
    #: "none exist", and a money rule reading it reports every lease as a
    #: problem. ``None`` is refused at registration. A detector that forgot
    #: to declare would otherwise read as depending on nothing and always run,
    #: which is the direction that makes false claims.
    #: ``test_pms_coverage.py`` checks each declaration against the SQL the
    #: detector actually executes.
    reads_tables: frozenset[str] | None = None

    @abstractmethod
    async def scan(self, conn: asyncpg.Connection, company_id: str) -> list[Finding]:
        """Compute the Findings currently true for ``company_id``.

        Pure read. Raising is safe — the runner isolates each detector so
        one failure never takes down the run.
        """
        raise NotImplementedError
