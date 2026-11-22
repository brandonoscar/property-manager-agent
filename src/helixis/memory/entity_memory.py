"""Entity-anchored memory (#534, Stage A).

The base memory store (:mod:`helixis.memory.manager`) learns facts from CHAT and
recalls them by fuzzy semantic search. This layer adds the two things #534 needs
on top of it, WITHOUT touching the live enrichment pipeline yet:

1. **Entity-anchored write** — store a fact tagged with the Buildium entity it's
   about (tenant / unit / owner / vendor / lease / property), so "Unit 4B always
   pays late" is attached to Unit 4B, not just floating in the company pool.
2. **Deterministic per-entity read** — pull EVERYTHING known about one entity by
   its id, not by hoping a semantic search surfaces it.

Plus **pure, deterministic fact derivers** that turn normalized ledger summaries
into plain-English behavioral facts ("paid late 4 of the last 6 months"). They
take a NORMALIZED input on purpose: Stage B populates it from the real mirror
(`helixis_lease_transactions` / `helixis_lease_balances`), grounded in the
Buildium contract — this module never guesses a Buildium field shape, and stays
a set of pure functions + thin wrappers over the manager, so it's unit-testable
with a fake manager and no DB.

Discipline (inherits the manager's + #534's guardrails): facts are **advisory**
(surfaced, never auto-acted); PII is stripped by ``manager.add``; every write is
company-scoped and audited by the manager. Derived facts are stored under the
existing ``MEMORY_CATEGORIES`` so recall/prompting treat them uniformly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)


EntityType = Literal["tenant", "unit", "owner", "vendor", "lease", "property"]

# Metadata keys the entity tag rides on — one source of truth so the write side
# (here) and the future injection side (Stage B) agree. Kept distinct from the
# manager's own metadata keys (``category`` / ``stored_by`` / ``stored_at``).
ENTITY_TYPE_KEY = "entity_type"
ENTITY_ID_KEY = "entity_id"
ENTITY_LABEL_KEY = "entity_label"
SOURCE_KEY = "source"
# Marks a fact derived from operational data (ledger/webhook) rather than chat,
# so Stage B / audits can tell learned-from-behavior apart from stated-in-chat.
SOURCE_OPERATIONAL = "operational"


@dataclass(frozen=True)
class EntityRef:
    """The Buildium entity a fact is about. ``entity_id`` is the Buildium id as
    a string (metadata values are compared as strings); ``label`` is the human
    name/number used both in prompts and as a prefix on the stored fact so
    semantic recall still works even before the metadata filter is consulted."""

    entity_type: EntityType
    entity_id: str
    label: str = ""

    def __post_init__(self) -> None:
        # Normalize the id to a trimmed string so int/str callers match on read.
        object.__setattr__(self, "entity_id", str(self.entity_id).strip())


class _ManagerLike(Protocol):
    """The slice of MemoryManager this module uses — typed as a Protocol so
    tests can pass a fake without importing the real (mem0-heavy) manager."""

    @property
    def available(self) -> bool: ...

    async def add(
        self,
        content: str,
        company_id: str = "",
        *,
        stored_by: str = "",
        category: str = "",
        extra_metadata: dict[str, Any] | None = None,
    ) -> str | None: ...

    async def get_all(self, company_id: str = "") -> list[dict[str, Any]]: ...


# ── entity-anchored write / read ─────────────────────────────────


async def store_entity_fact(
    manager: _ManagerLike,
    *,
    company_id: str,
    entity: EntityRef,
    fact: str,
    category: str = "",
    stored_by: str = "system",
    source: str = SOURCE_OPERATIONAL,
) -> str | None:
    """Store ``fact`` tagged with ``entity`` (company-scoped, PII-stripped +
    audited by the manager). The entity label is prefixed onto the stored text
    ("Unit 4B: paid late 4 of the last 6 months") so the fact reads on its own
    AND surfaces on a semantic search for the unit, not only via the metadata
    filter. Returns the memory id, or ``None`` when memory is unavailable / the
    fact is blank (best-effort — never raises)."""
    fact = (fact or "").strip()
    if not fact or not manager.available:
        return None
    text = f"{entity.label}: {fact}" if entity.label else fact
    return await manager.add(
        text,
        company_id=company_id,
        stored_by=stored_by,
        category=category,
        extra_metadata={
            ENTITY_TYPE_KEY: entity.entity_type,
            ENTITY_ID_KEY: entity.entity_id,
            ENTITY_LABEL_KEY: entity.label,
            SOURCE_KEY: source,
        },
    )


def _mem_matches_entity(mem: dict[str, Any], entity: EntityRef) -> bool:
    meta = mem.get("metadata") or {}
    return (
        str(meta.get(ENTITY_TYPE_KEY, "")) == entity.entity_type
        and str(meta.get(ENTITY_ID_KEY, "")) == entity.entity_id
    )


async def get_entity_memories(
    manager: _ManagerLike,
    *,
    company_id: str,
    entity: EntityRef,
) -> list[dict[str, Any]]:
    """Deterministically load every memory tagged with ``entity`` for the
    company. Filters the company pool by the entity metadata in Python — robust
    against mem0's arbitrary-metadata filter semantics (Stage B can push the
    filter down to mem0 if it proves reliable). Returns ``[]`` when memory is
    unavailable; never raises."""
    if not manager.available:
        return []
    all_mems = await manager.get_all(company_id=company_id)
    return [m for m in all_mems if _mem_matches_entity(m, entity)]


def format_entity_memories(memories: list[dict[str, Any]]) -> str:
    """Render entity memories as bullet lines for prompt/enrichment injection
    (Stage B). Empty string when there are none."""
    lines: list[str] = []
    for mem in memories:
        text = mem.get("memory", mem.get("content", ""))
        if not text:
            continue
        meta = mem.get("metadata") or {}
        cat = meta.get("category", "")
        prefix = f"[{cat}] " if cat else ""
        lines.append(f"- {prefix}{text}")
    return "\n".join(lines)


# ── pure, deterministic fact derivers ────────────────────────────
#
# Normalized inputs on purpose — Stage B fills these from the mirror after
# grounding the Buildium ledger shape. No Buildium field names appear here.


@dataclass(frozen=True)
class LatePaymentSummary:
    """Normalized rent-payment punctuality over a recent window. Stage B derives
    this from ``helixis_lease_transactions`` (charge→payment pairing)."""

    periods_considered: int
    late_count: int
    latest_days_late: int | None = None


@dataclass(frozen=True)
class BalanceSummary:
    """Normalized current outstanding balance. Stage B reads this from
    ``helixis_lease_balances`` (the delinquency snapshot)."""

    total_balance: float
    as_of: date


@dataclass(frozen=True)
class AgingSummary:
    """Normalized aged-receivables snapshot — Buildium's OWN computation of how
    overdue a lease's balance is, read from ``helixis_lease_balances`` aging
    buckets.

    This is the RELIABLE lateness signal (grounded against the live sandbox
    2026-07-11): ``helixis_lease_transactions`` has no due-date column, so
    charge→payment pairing would require GUESSING the due date — a wrong flag is
    harmful — whereas the aging buckets are Buildium's authoritative aged
    receivables. There is one balance snapshot per lease (no month-by-month
    history), so we derive current delinquency severity, not a "K of N months"
    frequency."""

    overdue_31_60: float
    overdue_61_90: float
    overdue_over_90: float
    as_of: date

    @property
    def overdue_30_plus(self) -> float:
        return self.overdue_31_60 + self.overdue_61_90 + self.overdue_over_90


# A tenant is flagged a chronic late-payer at or above this share of the window
# (and at least 2 late periods, so a single blip never earns the label).
_LATE_PAYER_FRACTION = 0.5
_LATE_PAYER_MIN_COUNT = 2


def derive_payment_behavior_fact(summary: LatePaymentSummary) -> str | None:
    """ "Paid late K of the last N months" when the record clears the chronic-
    late-payer bar; ``None`` otherwise (no fact = no flag, so a good payer never
    gets a memory). Deterministic — same input always yields the same fact."""
    n = summary.periods_considered
    k = summary.late_count
    if n <= 0 or k < _LATE_PAYER_MIN_COUNT:
        return None
    if k / n < _LATE_PAYER_FRACTION:
        return None
    tail = ""
    if summary.latest_days_late is not None and summary.latest_days_late > 0:
        tail = f" (most recently {summary.latest_days_late} days late)"
    return f"chronic late-payer — paid rent late {k} of the last {n} months{tail}"


def derive_balance_fact(summary: BalanceSummary) -> str | None:
    """ "Carries an outstanding balance of $X as of <date>" when the balance is
    positive; ``None`` for a zero/credit balance (nothing worth remembering)."""
    if summary.total_balance <= 0:
        return None
    return (
        f"carries an outstanding balance of ${summary.total_balance:,.2f} "
        f"as of {summary.as_of.isoformat()}"
    )


def derive_delinquency_fact(aging: AgingSummary) -> str | None:
    """Grounded late-payer signal from the aged-receivables buckets, or ``None``
    when nothing is 30+ days overdue (current-or-paid = no flag, so a
    good/current payer never earns a memory).

    "Chronic late-payer" is reserved for a balance carrying money **90+ days
    overdue** — a lease that has now missed roughly three rent cycles — and is
    called out distinctly from a lease that is only recently behind. Uses
    Buildium's own aging (see :class:`AgingSummary`), never a guessed due date,
    so the flag is defensible. Deterministic — same buckets, same fact."""
    overdue = aging.overdue_30_plus
    if overdue <= 0:
        return None
    if aging.overdue_over_90 > 0:
        return (
            f"chronic late-payer — ${overdue:,.2f} is 30+ days overdue, "
            f"including ${aging.overdue_over_90:,.2f} over 90 days "
            f"(as of {aging.as_of.isoformat()})"
        )
    return f"behind on rent — ${overdue:,.2f} is 30+ days overdue (as of {aging.as_of.isoformat()})"


def delinquency_severity(aging: AgingSummary) -> str:
    """Advisory severity for a delinquency signal: ``critical`` once any money
    is 90+ days overdue, ``warning`` for 30+ days overdue, else ``info`` (no
    signal is stored in the ``info`` case — see :func:`derive_delinquency_fact`)."""
    if aging.overdue_over_90 > 0:
        return "critical"
    if aging.overdue_30_plus > 0:
        return "warning"
    return "info"


__all__ = [
    "AgingSummary",
    "BalanceSummary",
    "ENTITY_ID_KEY",
    "ENTITY_LABEL_KEY",
    "ENTITY_TYPE_KEY",
    "EntityRef",
    "EntityType",
    "LatePaymentSummary",
    "SOURCE_KEY",
    "SOURCE_OPERATIONAL",
    "delinquency_severity",
    "derive_balance_fact",
    "derive_delinquency_fact",
    "derive_payment_behavior_fact",
    "format_entity_memories",
    "get_entity_memories",
    "store_entity_fact",
]
