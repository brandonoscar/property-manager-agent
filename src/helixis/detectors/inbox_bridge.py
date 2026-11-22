"""Detector findings → Inbox bridge (``Risk.FlagRaised`` events).

Detector findings used to live ONLY in ``helixis_risk_flags`` and render
as a thin digest (the Today strip) — they never entered the enrichment →
planner pipeline, so they never got the full Inbox message format
(summary + "What Occupella noticed" + suggested actions with drafts).
This module closes that gap the same way email ingest and teammate tasks
did: project each finding group onto the EXISTING ``buildium_events``
queue as a ``Risk.FlagRaised`` row with a task-shaped enrichment and a
deterministic floor surface, and let the worker's planner/summarizer
passes produce the designed card. No parallel "risk brain".

Grouping: ONE event per ``rule_key`` per bridge pass (the product call —
"Rent charged below the lease — 2 units" is one card covering both
units, not two cards). The grouped findings ride the enrichment's
``task.raw.findings`` so the planner can propose per-finding actions.

Idempotency, two layers:
- ``helixis_risk_flags.bridged_at`` (migration 0071) stamps each flag
  once bridged; the scan selects ``bridged_at IS NULL`` only. A flag row
  is bridged exactly ONCE per lifetime — a resolved→new reactivation
  does not re-bridge (deliberate: no Inbox re-spam).
- The payload's ``MessageId`` is derived from the sorted flag ids, so if
  the ``bridged_at`` stamp ever fails after enqueue, the next pass hits
  the queue's dedup key instead of double-carding.
"""

from __future__ import annotations

import hashlib
import logging
import zlib
from datetime import UTC, datetime
from typing import Any

import asyncpg

from helixis.webhooks.queue import enqueue, store_enrichment

logger = logging.getLogger(__name__)

RISK_EVENT_NAME = "Risk.FlagRaised"

# Same convention as email ingest / team tasks: no Buildium account is
# involved, so the envelope carries the sentinel and ``company_id`` is
# passed explicitly.
_RISK_ACCOUNT_SENTINEL = 0

RISK_SURFACE_SCHEMA_VERSION = 1

# Human labels for the known rule namespaces — used for grouped titles.
# A rule_key missing here degrades to a de-underscored fallback, so new
# detectors surface without touching this map (just less polished).
RULE_LABELS: dict[str, str] = {
    "rent_under_charged": "Rent charged below the lease",
    "duplicate_vendor_bill": "Possible duplicate vendor bill",
    "rent_not_collected": "No rent collected this month",
    "chronic_late_resident": "Seriously past-due balance",
    "promise_to_pay_broken": "Promise-to-pay came due",
    # The nightly LLM analyst writes source='analyst' rows under this one
    # rule_key — live prod carries them today, so the grouped card needs a
    # real name ("Analyst — 18 findings" is not one).
    "analyst": "Hidden risks in the books",
}


def rule_label(rule_key: str) -> str:
    """Human label for a rule_key ("rent_under_charged" → "Rent charged
    below the lease"; unknown keys degrade to de-underscored words)."""
    label = RULE_LABELS.get(rule_key)
    if label:
        return label
    words = rule_key.replace("_", " ").strip()
    return (words[:1].upper() + words[1:]) if words else "Risk finding"


_UNBRIDGED_SQL = """
SELECT id, rule_key, severity, title, detail, amount,
       property_id, entity_type, entity_id
  FROM helixis_risk_flags
 WHERE company_id = $1::uuid
   AND status IN ('new', 'confirmed')
   AND bridged_at IS NULL
 ORDER BY rule_key, id
"""

_MARK_BRIDGED_SQL = """
UPDATE helixis_risk_flags
   SET bridged_at = now()
 WHERE id = ANY($1::uuid[])
"""


def risk_group_message_id(rule_key: str, flag_ids: list[str]) -> str:
    """Deterministic per-group identity for the queue's dedup key.

    Derived from the SORTED flag ids so the same unbridged set always
    maps to the same event — if the ``bridged_at`` stamp fails after an
    enqueue, the retry dedups instead of double-carding. A later pass
    with NEW flags in the group yields a different id (a genuinely new
    event).
    """
    digest = hashlib.sha256("|".join(sorted(flag_ids)).encode("utf-8")).hexdigest()[:16]
    return f"risk:{rule_key}:{digest}"


def risk_synthetic_task_id(message_id: str) -> int:
    """Stable positive int surrogate for ``EnrichedTask.id`` (typed int,
    required) — same crc32 derivation as ``email_synthetic_task_id``;
    the real identity (flag ids) is preserved on ``task.raw``."""
    return zlib.crc32(message_id.encode("utf-8")) & 0x7FFFFFFF


def group_title(rule_key: str, findings: list[dict[str, Any]]) -> str:
    """The card title. A single finding keeps its own detector-written
    title; a group gets the rule label + count ("Rent charged below the
    lease — 2 findings")."""
    if len(findings) == 1:
        title = str(findings[0].get("title") or "").strip()
        if title:
            return title
    return f"{rule_label(rule_key)} — {len(findings)} findings"


def _total_amount(findings: list[dict[str, Any]]) -> float:
    total = 0.0
    for f in findings:
        amount = f.get("amount")
        if isinstance(amount, (int, float)):
            total += float(amount)
    return round(total, 2)


def build_risk_event_payload(
    *,
    rule_key: str,
    findings: list[dict[str, Any]],
    event_time_iso: str,
) -> dict[str, Any]:
    """The ``buildium_events.payload`` envelope for one rule group.

    Shaped like a Buildium envelope (EventName / EventDateTime /
    AccountId) so it rides the same queue + dedup. Deliberately light —
    per-finding detail lives on the enrichment column; the payload keeps
    just enough (Title / RuleKey / Count / TotalAmount) for the fallback
    enricher to degrade gracefully if that column is ever lost.
    """
    flag_ids = [str(f["id"]) for f in findings]
    return {
        "EventName": RISK_EVENT_NAME,
        "EventDateTime": event_time_iso,
        "AccountId": _RISK_ACCOUNT_SENTINEL,
        "MessageId": risk_group_message_id(rule_key, flag_ids),
        "RuleKey": rule_key,
        "Title": group_title(rule_key, findings),
        "FlagIds": flag_ids,
        "Count": len(findings),
        "TotalAmount": _total_amount(findings),
    }


def build_risk_enrichment(
    *,
    company_id: str,
    rule_key: str,
    findings: list[dict[str, Any]],
    event_time_iso: str,
) -> dict[str, Any]:
    """Project a finding group onto the ``EnrichedEvent`` task shape so
    the existing planner/summarizer pipeline consumes it natively (the
    email mapping's twin): grouped title → ``task.title``, the joined
    per-finding details → ``task.description``, category ``"RiskFlag"``,
    the structured findings on ``task.raw`` so the planner can ground
    per-finding actions in real ids and amounts.
    """
    message_id = risk_group_message_id(rule_key, [str(f["id"]) for f in findings])
    lines: list[str] = []
    for f in findings:
        title = str(f.get("title") or "").strip()
        detail = str(f.get("detail") or "").strip()
        lines.append(f"- {title}: {detail}" if detail else f"- {title}")
    any_critical = any(f.get("severity") == "critical" for f in findings)
    return {
        "schema_version": 1,
        "event_name": RISK_EVENT_NAME,
        "company_id": company_id,
        "task": {
            "id": risk_synthetic_task_id(message_id),
            "title": group_title(rule_key, findings),
            "description": "\n".join(lines),
            "status": None,
            "category": "RiskFlag",
            "priority": "High" if any_critical else "Normal",
            "requested_by_first_name": None,
            "requested_by_last_name": None,
            "raw": {
                "source": "risk_detector",
                "rule_key": rule_key,
                "detected_at": event_time_iso,
                "findings": [
                    {
                        "flag_id": str(f["id"]),
                        "severity": f.get("severity"),
                        "title": f.get("title"),
                        "detail": f.get("detail"),
                        "amount": f.get("amount"),
                        "property_id": f.get("property_id"),
                        "entity_type": f.get("entity_type"),
                        "entity_id": f.get("entity_id"),
                    }
                    for f in findings
                ],
            },
        },
    }


def build_risk_action_surface(
    *,
    rule_key: str,
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    """The deterministic floor surface: even before the planner's pass
    lands, the Inbox shows what was found — the grouped summary plus one
    "noticed" signal per finding. The planner's enriched surface (with
    real suggested actions + drafts) replaces this once its pass
    completes (same lifecycle as the email floor card)."""
    title = group_title(rule_key, findings)
    total = _total_amount(findings)
    summary = title if total <= 0 else f"{title} (${total:,.0f} involved)"
    signals: list[str] = []
    for f in findings[:6]:
        text = str(f.get("title") or "").strip()
        if text:
            signals.append(text)
    return {
        "schema_version": RISK_SURFACE_SCHEMA_VERSION,
        "kind": "risk",
        "summary": summary,
        "risk": {
            "rule_key": rule_key,
            "count": len(findings),
            "total_amount": total,
        },
        "signals": signals,
        "actions": [],
        "freeform_placeholder": "or tell me how you want this handled…",
    }


async def bridge_new_findings(pool: asyncpg.Pool, company_id: str) -> int:
    """Project every unbridged open finding for one company into the
    Inbox pipeline — one ``Risk.FlagRaised`` event per rule_key group.

    Returns the number of events created. Per-group failures are
    contained (logged; the group's flags stay unbridged for the next
    pass). Flags are stamped ``bridged_at`` even when the enqueue dedups
    (the event already exists — re-carding would be the bug).
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(_UNBRIDGED_SQL, company_id)
    if not rows:
        return 0

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        finding = dict(row)
        # numeric(12,2) arrives as Decimal — normalize so the enrichment
        # dict is json.dumps-able and the amount math stays float.
        if finding.get("amount") is not None:
            finding["amount"] = float(finding["amount"])
        groups.setdefault(str(finding["rule_key"]), []).append(finding)

    created = 0
    for rule_key, findings in groups.items():
        try:
            now = datetime.now(UTC)
            event_time_iso = now.isoformat()
            payload = build_risk_event_payload(
                rule_key=rule_key, findings=findings, event_time_iso=event_time_iso
            )
            result = await enqueue(
                pool,
                event_name=RISK_EVENT_NAME,
                event_time=now,
                account_id=_RISK_ACCOUNT_SENTINEL,
                entity_ids={},
                payload=payload,
                company_id=company_id,
            )
            if not result.duplicate:
                await store_enrichment(
                    pool,
                    result.id,
                    enrichment=build_risk_enrichment(
                        company_id=company_id,
                        rule_key=rule_key,
                        findings=findings,
                        event_time_iso=event_time_iso,
                    ),
                    action_surface=build_risk_action_surface(rule_key=rule_key, findings=findings),
                )
                created += 1
            # Stamp AFTER the event exists (fresh or dedup) — a crash
            # before this line leaves the flags unbridged and the
            # MessageId dedup absorbs the retry.
            flag_ids = [f["id"] for f in findings]
            async with pool.acquire() as conn:
                await conn.execute(_MARK_BRIDGED_SQL, flag_ids)
        except Exception:  # noqa: BLE001 — isolate each rule group
            logger.exception(
                "risk bridge: group %s failed for company %s — will retry next pass",
                rule_key,
                company_id,
            )
    if created:
        logger.info("risk bridge: company=%s events=%d", company_id, created)
    return created


__all__ = [
    "RISK_EVENT_NAME",
    "RULE_LABELS",
    "bridge_new_findings",
    "build_risk_action_surface",
    "build_risk_enrichment",
    "build_risk_event_payload",
    "group_title",
    "risk_group_message_id",
    "risk_synthetic_task_id",
    "rule_label",
]
