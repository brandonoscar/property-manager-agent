"""Phase 2 — the background analyst (roadmap §7 rank 4).

The autonomous half of reconciliation: once a day per company, assemble
a compact **dossier** of the mirrored books (leases with balances,
charges and renewal windows; open work orders with ages; what the
deterministic rules already flagged) and have the reasoning model do
what a human analyst does — connect dots ACROSS records that no single
rule looks at ("three HVAC tickets + a climbing balance + a lease
ending in 60 days on the same unit").

The trust contract that makes autonomous flagging safe:

- **The dossier is the whole world.** The model sees ONLY mirror data;
  the prompt forbids outside knowledge or arithmetic beyond comparing
  the numbers given.
- **Citations or it didn't happen.** Every risk must anchor to an
  entity id present in the dossier and cite the dossier rows it used.
  The validator drops (and logs) any risk whose anchor or citations
  don't exist, and any risk whose quoted dollar amounts aren't literal
  dossier values — a REVERSE_REPORT-style gate, enforced in code.
- **Read-only, budgeted, capped.** Flags only (same ledger as Phase 1,
  ``source='analyst'``); the run is skipped when the company's daily
  token budget (0050) is exhausted; at most ``analyst_max_flags``
  survive per run.
- **Dark by default.** ``HELIXIS_RECON__ANALYST_ENABLED`` gates the
  whole cycle — it runs on our own books before any customer's.

Dedup + lifecycle: fingerprint = ``{kind}:{entity_type}:{entity_id}``,
so re-detecting the same risk on the same entity refreshes one row and
a dismissal keeps suppressing it. Analyst flags are non-deterministic
run to run, so instead of Phase 1's same-sweep auto-resolve they AGE
OUT: an open analyst flag not re-detected for 7 days resolves.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import asyncpg
import litellm

from helixis.buildium.maintenance_status import open_status_sql
from helixis.common.json_extract import coerce_json
from helixis.db.pms_coverage import coverage_for
from helixis.llm import spend
from helixis.llm.router import _api_key_for_model
from helixis.llm.sampling import sampling_kwargs
from helixis.observability.context import trace_context
from helixis.settings import Settings

logger = logging.getLogger(__name__)

_LLM_TIMEOUT_SECONDS = 45.0

# Dossier caps — a 500-door book fits whole; a 5,000-door book sends its
# most interesting slice. Bounded input = bounded cost + bounded prompt.
_MAX_LEASES = 60
_MAX_WORK_ORDERS = 40

_STALE_ANALYST_DAYS = 7

KINDS = (
    "churn_risk",
    "delinquency_pattern",
    "maintenance_pattern",
    "expense_anomaly",
    "data_quality",
    "other",
)

_SEVERITIES = ("info", "warning", "critical")

_SYSTEM_PROMPT = f"""You are Occupella's risk analyst for a property management company.
You receive a DOSSIER of the company's mirrored Buildium data. It is your ONLY
source of truth.

Find up to {{max_flags}} cross-record risks a busy property manager would miss —
patterns that emerge only when you connect multiple records (e.g. repeat
maintenance on a unit whose lease is ending while its balance climbs).

HARD RULES — violations are discarded by a validator, so follow them exactly:
1. Use ONLY the dossier. No outside knowledge, no assumptions, no arithmetic
   beyond comparing numbers that appear in the dossier.
2. Every dollar figure you write in `detail` must be a number that appears
   literally in the dossier.
3. Every risk must anchor to a real record: `entity_type` is "lease" or
   "work_order", and `entity_id` must be an id present in the dossier.
4. `citations` lists every dossier record you used, as
   {{{{"section": "leases"|"work_orders", "id": <id>}}}} — at least one.
5. Do NOT restate anything already listed under `open_rule_flags`.
6. If nothing is genuinely noteworthy, return {{{{"risks": []}}}} — silence is a
   valid, good answer. Never invent urgency.

Respond with ONLY this JSON shape:
{{{{"risks": [{{{{"kind": one of {list(KINDS)},
             "severity": "info"|"warning"|"critical",
             "title": "<= 90 chars, plain English",
             "detail": "<= 350 chars — the connected dots, with the numbers",
             "entity_type": "lease"|"work_order",
             "entity_id": <int>,
             "property_id": <int or null>,
             "citations": [{{{{"section": "...", "id": <int>}}}}]}}}}]}}}}"""


# ── Dossier ──────────────────────────────────────────────────────────

_DOSSIER_LEASES_SQL = f"""
SELECT l.buildium_id                             AS lease_id,
       l.property_id,
       u.unit_number,
       l.rent::float                             AS rent,
       COALESCE(b.total_balance, 0)::float       AS balance,
       l.lease_to_date::date::text               AS lease_end,
       GREATEST(0, (l.lease_to_date::date - current_date))::int AS days_to_end,
       COALESCE(c.charges_3mo, 0)::float         AS charges_3mo,
       COALESCE(p.payments_3mo, 0)::float        AS payments_3mo
  FROM helixis_leases l
  LEFT JOIN helixis_units u
    ON u.buildium_id = l.unit_id AND u.company_id = l.company_id
  LEFT JOIN helixis_lease_balances b
    ON b.lease_id = l.buildium_id AND b.company_id = l.company_id
  LEFT JOIN (
      SELECT lease_id, SUM(total_amount) AS charges_3mo
        FROM helixis_lease_transactions
       WHERE company_id = $1::uuid AND transaction_type = 'Charge'
         AND transaction_date >= now() - interval '3 months'
       GROUP BY lease_id
  ) c ON c.lease_id = l.buildium_id
  LEFT JOIN (
      -- ⚠ NEGATED, and this is a PRESENTATION fix, not a storage one. Buildium
      -- signs a ledger row by its effect on the balance: a Payment REDUCES it
      -- and arrives negative. Live-verified across all three companies —
      -- 144 of 144 Payment rows negative, 619 of 619 Charge rows positive,
      -- Credit and Applied Deposit negative, Refund positive. The mirror
      -- stores that faithfully and MUST keep doing so; flipping the sign at
      -- write time would corrupt the ledger and contradict gotcha 21.
      --
      -- What was wrong is what the analyst was handed: a field named
      -- ``payments_3mo`` whose value was -820, next to ``charges_3mo`` at
      -- 3000. Nothing in the dossier says the two use opposite conventions,
      -- so the model either narrates a negative payment or compares them
      -- directly and gets the arrears backwards. The sanitizer already
      -- de-jargonises the NAME ("payments_3mo" -> "payments 3mo") and has
      -- never touched the sign — its own comment quotes the leak verbatim.
      --
      -- Negating (rather than ABS) keeps a reversal honest: a positive
      -- Payment row means money went back out, and it should read negative
      -- here. Charges need no adjustment; the other types are not summed.
      SELECT lease_id, -SUM(total_amount) AS payments_3mo
        FROM helixis_lease_transactions
       WHERE company_id = $1::uuid AND transaction_type = 'Payment'
         AND transaction_date >= now() - interval '3 months'
       GROUP BY lease_id
  ) p ON p.lease_id = l.buildium_id
 WHERE l.company_id = $1::uuid AND l.status = 'Active'
 ORDER BY COALESCE(b.total_balance, 0) DESC, l.lease_to_date ASC NULLS LAST
 LIMIT {_MAX_LEASES}
"""

# ⚠ ``age_days`` WAS HERE AND HAS BEEN DELETED — it was
# ``(current_date - wo.updated_at::date)`` and ``updated_at`` is when WE last
# wrote the mirror row, not when anything happened in Buildium. Live-measured
# 2026-08-28 on Blue Ridge: all 60 work orders share ONE distinct
# ``updated_at`` date (the last sync), so every open work order reported an
# age of 0 — the analyst was handed a column that says "brand new" about a
# backlog, in the dossier it grounds its numbers on.
#
# It is NOT fixable by pointing at a better column, which is why the number is
# gone rather than corrected: there is no Buildium date on this record to point
# at. ``raw->>'CreatedDateTime'`` and ``UpdatedDateTime`` are absent on 60 of 60
# rows, and ``schemas.WorkOrder`` already records the probe that found the same
# thing — "/workorders exposes DueDate … there is NO created/opened/updated date
# on this endpoint". A derived figure with no source is worse than a missing
# column: the analyst cites it, and a cited zero reads as measured.
#
# ``due_date`` IS available on the endpoint, so "overdue" remains answerable and
# is the honest question to build on if age is wanted back. The ORDER BY keeps
# ``updated_at`` deliberately — as a stable tiebreak it is fine; it was only
# ever wrong when RENDERED as a fact about the work order.
_DOSSIER_WOS_SQL = f"""
SELECT wo.buildium_id                             AS wo_id,
       COALESCE(wo.property_id, wu.property_id)   AS property_id,
       wo.unit_id,
       wo.title,
       wo.status
  FROM helixis_work_orders wo
  LEFT JOIN helixis_units wu
    ON wu.buildium_id = wo.unit_id AND wu.company_id = wo.company_id
 WHERE wo.company_id = $1::uuid AND {open_status_sql("wo.status")}
 ORDER BY wo.updated_at ASC
 LIMIT {_MAX_WORK_ORDERS}
"""

_DOSSIER_RULE_FLAGS_SQL = """
SELECT rule_key, title, entity_type, entity_id
  FROM helixis_risk_flags
 WHERE company_id = $1::uuid AND source = 'rule' AND status IN ('new', 'confirmed')
 LIMIT 50
"""


async def build_dossier(pool: asyncpg.Pool, company_id: str) -> dict[str, Any]:
    async with pool.acquire() as conn:
        leases = [dict(r) for r in await conn.fetch(_DOSSIER_LEASES_SQL, company_id)]
        work_orders = [dict(r) for r in await conn.fetch(_DOSSIER_WOS_SQL, company_id)]
        rule_flags = [dict(r) for r in await conn.fetch(_DOSSIER_RULE_FLAGS_SQL, company_id)]
    return {"leases": leases, "work_orders": work_orders, "open_rule_flags": rule_flags}


# ── Validation (the grounding gate) ──────────────────────────────────

_MONEY_RE = re.compile(r"\$([\d,]+(?:\.\d{1,2})?)")


def _dossier_numbers(dossier: dict[str, Any]) -> set[float]:
    """Every numeric value in the dossier — the only dollars the analyst
    is allowed to quote."""
    numbers: set[float] = set()
    for section in ("leases", "work_orders"):
        for row in dossier.get(section, []):
            for v in row.values():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    numbers.add(round(float(v), 2))
    return numbers


def validate_risks(
    raw: Any, dossier: dict[str, Any], max_flags: int
) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep only risks fully grounded in the dossier. Returns (kept, drop_reasons)."""
    lease_ids = {r["lease_id"] for r in dossier.get("leases", [])}
    wo_ids = {r["wo_id"] for r in dossier.get("work_orders", [])}
    allowed = _dossier_numbers(dossier)

    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    risks = raw.get("risks", []) if isinstance(raw, dict) else []
    if not isinstance(risks, list):
        return [], ["risks is not a list"]

    for i, r in enumerate(risks):
        if len(kept) >= max_flags:
            dropped.append(f"risk[{i}]: over max_flags cap")
            continue
        if not isinstance(r, dict):
            dropped.append(f"risk[{i}]: not an object")
            continue
        if r.get("kind") not in KINDS:
            dropped.append(f"risk[{i}]: unknown kind {r.get('kind')!r}")
            continue
        if r.get("severity") not in _SEVERITIES:
            dropped.append(f"risk[{i}]: unknown severity {r.get('severity')!r}")
            continue
        title, detail = str(r.get("title", "")).strip(), str(r.get("detail", "")).strip()
        if not title or not detail:
            dropped.append(f"risk[{i}]: empty title/detail")
            continue

        # Anchor must exist in the dossier.
        etype, eid = r.get("entity_type"), r.get("entity_id")
        anchor_ok = (etype == "lease" and eid in lease_ids) or (
            etype == "work_order" and eid in wo_ids
        )
        if not anchor_ok:
            dropped.append(f"risk[{i}]: anchor {etype}:{eid} not in dossier")
            continue

        # Every citation must exist.
        citations = r.get("citations") or []
        if not isinstance(citations, list) or not citations:
            dropped.append(f"risk[{i}]: no citations")
            continue
        cite_ok = all(
            isinstance(c, dict)
            and (
                (c.get("section") == "leases" and c.get("id") in lease_ids)
                or (c.get("section") == "work_orders" and c.get("id") in wo_ids)
            )
            for c in citations
        )
        if not cite_ok:
            dropped.append(f"risk[{i}]: citation not in dossier")
            continue

        # Every quoted dollar must be a literal dossier value.
        quoted = [float(m.replace(",", "")) for m in _MONEY_RE.findall(detail)]
        if any(round(q, 2) not in allowed for q in quoted):
            dropped.append(f"risk[{i}]: quotes ${quoted} not all in dossier")
            continue

        kept.append(
            {
                "kind": r["kind"],
                "severity": r["severity"],
                "title": title[:90],
                "detail": detail[:350],
                "entity_type": etype,
                "entity_id": eid,
                "property_id": r.get("property_id"),
                "citations": citations,
            }
        )
    return kept, dropped


# ── Persistence ──────────────────────────────────────────────────────

_ANALYST_UPSERT_SQL = """
INSERT INTO helixis_risk_flags
    (company_id, source, rule_key, fingerprint, severity, title, detail,
     amount, property_id, entity_type, entity_id, evidence)
VALUES ($1::uuid, 'analyst', 'analyst', $2, $3, $4, $5, NULL, $6, $7, $8, $9::jsonb)
ON CONFLICT (company_id, rule_key, fingerprint) DO UPDATE SET
    severity     = EXCLUDED.severity,
    title        = EXCLUDED.title,
    detail       = EXCLUDED.detail,
    evidence     = EXCLUDED.evidence,
    last_seen_at = now(),
    status       = CASE
                     WHEN helixis_risk_flags.status = 'resolved' THEN 'new'
                     ELSE helixis_risk_flags.status
                   END,
    resolved_at  = CASE
                     WHEN helixis_risk_flags.status = 'resolved' THEN NULL
                     ELSE helixis_risk_flags.resolved_at
                   END
"""

# Analyst findings are non-deterministic run to run, so they age out
# instead of same-sweep resolving: open + not re-detected for 7 days.
_AGE_OUT_SQL = """
UPDATE helixis_risk_flags
   SET status = 'resolved', resolved_at = now()
 WHERE company_id = $1::uuid
   AND source = 'analyst'
   AND status IN ('new', 'confirmed')
   AND last_seen_at < now() - make_interval(days => $2)
"""


# ── The run ──────────────────────────────────────────────────────────


def _analyst_model(settings: Settings) -> str:
    if settings.recon.analyst_model:
        return settings.recon.analyst_model
    models = settings.llm.models or {}
    reasoning = models.get("reasoning") or {}
    return reasoning.get("model") or settings.llm.default_model


async def analyze_company(
    pool: asyncpg.Pool,
    settings: Settings,
    company_id: str,
    *,
    completion_fn: Any = None,
) -> int:
    """One analyst pass for one company. Returns flags written (0 on any
    soft failure — the analyst is an enhancement, never load-bearing)."""
    complete = completion_fn or litellm.acompletion

    # ⚠ The dossier hands the model every lease's balance, charges and
    # payments. For a PMS that sends none of those (Rentvine), each one reads
    # as 0, and "rent charged $0 for three months" is precisely what the model
    # is asked to flag, so every lease would come back as a risk. Skip, don't
    # guess (gotcha 163).
    cov = await coverage_for(pool, company_id)
    blind = [t for t in ANALYST_READS if not cov.sees(t)]
    if blind:
        logger.info("analyst skipped (pms sends no %s): company=%s", sorted(blind), company_id)
        return 0

    dossier = await build_dossier(pool, company_id)
    if not dossier["leases"] and not dossier["work_orders"]:
        return 0

    model = _analyst_model(settings)
    max_flags = settings.recon.analyst_max_flags
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT.format(max_flags=max_flags)},
        {"role": "user", "content": "DOSSIER:\n" + json.dumps(dossier, default=str)},
    ]
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        **sampling_kwargs(model, 0.1),
        "timeout": _LLM_TIMEOUT_SECONDS,
    }
    api_key = _api_key_for_model(settings, model)
    if api_key:
        kwargs["api_key"] = api_key

    # trace_context so the 0050 spend ledger + Langfuse attribute the run.
    with trace_context(
        company_id=company_id,
        session_id=f"analyst:{company_id[:8]}",
        trace_name="helixis-recon-analyst",
    ):
        response = await complete(**kwargs)
    raw_text = response.choices[0].message.content or ""
    parsed, _ = coerce_json(raw_text)

    kept, dropped = validate_risks(parsed, dossier, max_flags)
    for reason in dropped:
        logger.info("analyst flag dropped (company=%s): %s", company_id, reason)

    async with pool.acquire() as conn:
        for r in kept:
            await conn.execute(
                _ANALYST_UPSERT_SQL,
                company_id,
                f"{r['kind']}:{r['entity_type']}:{r['entity_id']}",
                r["severity"],
                r["title"],
                r["detail"],
                r["property_id"],
                r["entity_type"],
                r["entity_id"],
                json.dumps({"citations": r["citations"], "kind": r["kind"]}),
            )
        await conn.execute(_AGE_OUT_SQL, company_id, _STALE_ANALYST_DAYS)
    return len(kept)


#: Every PMS-fed table the dossier reads. ``test_pms_coverage.py`` pins this
#: against the SQL in this module.
ANALYST_READS: frozenset[str] = frozenset(
    {
        "helixis_bills",
        "helixis_lease_balances",
        "helixis_lease_transactions",
        "helixis_leases",
        "helixis_units",
        "helixis_work_orders",
    }
)


_COMPANIES_SQL = """
SELECT DISTINCT company_id FROM helixis_leases
UNION
SELECT DISTINCT company_id FROM helixis_bills
"""


async def run_analyst_cycle(pool: asyncpg.Pool, settings: Settings) -> None:
    """The nightly cycle: every company with mirrored books, one analyst
    pass each — skipped entirely unless the flag is on, and per company
    when its daily token budget (0050) is spent."""
    if not settings.recon.analyst_enabled:
        return

    async with pool.acquire() as conn:
        rows = await conn.fetch(_COMPANIES_SQL)
    for row in rows:
        company_id = str(row["company_id"])
        try:
            if await spend.budget_exceeded(company_id, settings.llm.daily_token_budget):
                logger.info("analyst skipped (budget spent): company=%s", company_id)
                continue
            written = await analyze_company(pool, settings, company_id)
            if written:
                logger.info("analyst flags written: company=%s n=%d", company_id, written)
        except (Exception, SystemExit):  # noqa: BLE001 — one bad company never stops the cycle
            logger.exception("analyst pass failed for company %s", company_id)


__all__ = [
    "ANALYST_READS",
    "KINDS",
    "analyze_company",
    "build_dossier",
    "run_analyst_cycle",
    "validate_risks",
]
